"""Measure how the pedal's runtime snapshot relates to the documented MIDI controllers.

The runtime snapshot (command 0x63) lists eight effects with a ``variant`` and
normalized ``params``. The MIDI guide documents controller numbers per block.
This script sends each documented controller and reports which entry of the
snapshot changed, so the mapping is measured instead of guessed.

It changes only the LIVE state of the active preset and never stores anything:
it writes a backup first, restores the original state at the end (also after
an error) and verifies the restore. Nothing is sent unless ``--apply`` is given.

    uv run python scripts/calibrate_mapping.py --backup /tmp/before.json            # plan only
    uv run python scripts/calibrate_mapping.py --backup /tmp/before.json --apply
"""

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nanocore_controller.device import NanocoreDevice
from nanocore_controller.discovery import discover_nanocore_midi_port
from nanocore_controller.errors import NanocoreError
from nanocore_controller.midi import control_change
from nanocore_controller.nanocore_protocol import RuntimeSnapshot
from nanocore_controller.usb_midi import AmidiSession

# Controller ranges from the MIDI guide (docs/protocol.md). Tuner (80) and preset
# stepping (81, 82) are deliberately absent.
PROBE_RANGES: dict[str, tuple[int, ...]] = {
    "fx1": tuple(range(50, 57)),
    "fx2": tuple(range(57, 60)) + tuple(range(68, 74)),
    "amp": tuple(range(60, 65)),
    "cab": tuple(range(65, 68)),
    "mod": tuple(range(68, 74)),
    "del": tuple(range(74, 80)),
    "rev": tuple(range(85, 91)),
    "eq": (33, *range(91, 98)),
}
PROBE_VALUES = (25, 100)
# These two blocks ignore their type controller on real hardware (SysEx selects the model).
FIXED_TYPE_BLOCKS = frozenset({"amp", "cab"})
SETTLE_TIMEOUT = 0.4
SETTLE_STEP = 0.02


@dataclass(frozen=True)
class Change:
    effect: int
    field: str  # "enabled", "variant" or "param"
    index: int | None
    old: Any
    new: Any


def diff_snapshots(before: RuntimeSnapshot, after: RuntimeSnapshot) -> list[Change]:
    """Every difference between two runtime snapshots, effect by effect."""

    changes: list[Change] = []
    for number, (old, new) in enumerate(zip(before.effects, after.effects, strict=True)):
        if old.enabled != new.enabled:
            changes.append(Change(number, "enabled", None, old.enabled, new.enabled))
        if old.variant != new.variant:
            changes.append(Change(number, "variant", None, old.variant, new.variant))
        if len(old.params) != len(new.params):
            changes.append(Change(number, "param_count", None, len(old.params), len(new.params)))
            continue
        for index, (a, b) in enumerate(zip(old.params, new.params, strict=True)):
            if abs(a - b) > 1e-6:
                changes.append(Change(number, "param", index, a, b))
    return changes


def describe(changes: list[Change]) -> list[dict[str, Any]]:
    return [
        {"effect": c.effect, "field": c.field, "index": c.index, "old": c.old, "new": c.new}
        for c in changes
    ]


class Calibration:
    def __init__(self, device: NanocoreDevice, slot: int, catalog: dict[str, Any]) -> None:
        self.device = device
        self.slot = slot
        self.catalog = catalog
        self.sent = 0
        self.lags: list[float] | None = []

    async def send(self, cc: int, value: int) -> None:
        self.sent += 1
        await self.device.send_midi(control_change(1, cc, value))

    async def read(self) -> RuntimeSnapshot:
        snapshot = await self.device.read_live()
        if snapshot.active_preset != self.slot:
            raise RuntimeError(
                f"the active preset changed from {self.slot + 1} to {snapshot.active_preset + 1}"
            )
        return snapshot

    async def send_and_observe(
        self, cc: int, value: int, before: RuntimeSnapshot
    ) -> tuple[RuntimeSnapshot, list[Change]]:
        """Send one controller and wait until the snapshot changed and then stopped changing.

        The pedal applies writes a moment after it acknowledges them, so a read
        taken at once can still show the old state. Returns the last snapshot and
        its differences from ``before``; no differences after the timeout means
        the controller had no effect.
        """

        await self.send(cc, value)
        return await self.settle(before)

    async def settle(self, before: RuntimeSnapshot) -> tuple[RuntimeSnapshot, list[Change]]:
        waited = 0.0
        previous: RuntimeSnapshot | None = None
        latest = before
        while waited < SETTLE_TIMEOUT:
            latest = await self.read()
            changed = bool(diff_snapshots(before, latest))
            if changed and previous is not None and not diff_snapshots(previous, latest):
                break
            previous = latest
            await asyncio.sleep(SETTLE_STEP)
            waited += SETTLE_STEP
        if self.lags is not None and diff_snapshots(before, latest):
            self.lags.append(waited)
        return latest, diff_snapshots(before, latest)

    async def find_block_index(self, block: dict[str, Any], current: RuntimeSnapshot) -> int | None:
        """Which snapshot entry the block's on/off controller toggles."""

        cc = block["onOffCC"]
        for value in (127, 0):
            after, changes = await self.send_and_observe(cc, value, current)
            flipped = [c.effect for c in changes if c.field == "enabled"]
            if flipped:
                await self.send(cc, 127 if current.effects[flipped[0]].enabled else 0)
                restored = await self.read()
                if diff_snapshots(current, restored):
                    raise RuntimeError(f"could not restore the on/off state of {block['id']}")
                return flipped[0]
        return None

    async def probe_type(self, block: dict[str, Any], type_entry: dict[str, Any], index: int, current: RuntimeSnapshot) -> tuple[RuntimeSnapshot, dict[str, Any]]:
        record: dict[str, Any] = {"name": type_entry["name"], "type_id": type_entry["id"]}
        if block["id"] not in FIXED_TYPE_BLOCKS and block["typeCC"] is not None:
            current, changes = await self.send_and_observe(block["typeCC"], type_entry["id"], current)
            record["variant_after_type_cc"] = current.effects[index].variant
        record["variant"] = current.effects[index].variant
        record["param_count"] = len(current.effects[index].params)
        cc_map: dict[str, Any] = {}
        for cc in PROBE_RANGES[block["id"]]:
            seen: list[dict[str, Any]] = []
            for value in PROBE_VALUES:
                current, changes = await self.send_and_observe(cc, value, current)
                seen.append({"value": value, "changes": describe(changes)})
            cc_map[str(cc)] = seen
        record["cc_map"] = cc_map
        return current, record

    async def check_chain_order(self, current: RuntimeSnapshot) -> dict[str, Any]:
        original = list(current.chain_order)
        swapped = original.copy()
        swapped[0], swapped[1] = swapped[1], swapped[0]
        await self.device.set_chain_order(swapped)
        after, _ = await self.settle(current)
        await self.device.set_chain_order(original)
        restored, _ = await self.settle(after)
        return {
            "original": original,
            "sent": swapped,
            "read_back": list(after.chain_order),
            "restored": list(restored.chain_order),
        }

    async def run(self) -> dict[str, Any]:
        current = await self.read()
        result: dict[str, Any] = {"slot": self.slot, "blocks": {}}
        result["chain_order"] = await self.check_chain_order(current)
        for block in self.catalog["blocks"]:
            print(f"  {block['id']} ...", flush=True)
            index = await self.find_block_index(block, current)
            entry: dict[str, Any] = {"runtime_index": index, "on_off_cc": block["onOffCC"], "types": {}}
            result["blocks"][block["id"]] = entry
            if index is None:
                entry["error"] = "the on/off controller changed no entry"
                continue
            types = block["types"][:1] if block["id"] in FIXED_TYPE_BLOCKS else block["types"]
            original_variant = current.effects[index].variant
            for type_entry in types:
                current, record = await self.probe_type(block, type_entry, index, current)
                entry["types"][str(type_entry["id"])] = record
            if block["id"] not in FIXED_TYPE_BLOCKS and block["typeCC"] is not None:
                # Controllers 68-73 serve the modulation or FX2 pitch/wah depending on the
                # FX2 type, so every block hands its original type back before the next one.
                back = [t["type_id"] for t in entry["types"].values() if t["variant"] == original_variant]
                if back:
                    current, _ = await self.send_and_observe(block["typeCC"], back[0], current)
        result["controllers_sent"] = self.sent
        if self.lags:
            result["settle_seconds"] = {"count": len(self.lags), "max": round(max(self.lags), 3), "mean": round(sum(self.lags) / len(self.lags), 3)}
        return result


async def calibrate(args: argparse.Namespace) -> int:
    port = args.port or discover_nanocore_midi_port()
    catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    backup = Path(args.backup)
    safety = Path(args.safety_backup) if args.safety_backup else backup.with_name(backup.stem + "-final.json")
    async with NanocoreDevice(
        AmidiSession(port), device_info={"transport": "usb", "port": port}
    ) as device:
        original = await device.backup_active(backup)
        slot = original["preset"]["slot"]
        name = original["preset"]["name"]
        print(f"active preset {slot + 1} ({name}); backup written to {backup}")
        total = sum(
            len(PROBE_RANGES[b["id"]]) * len(PROBE_VALUES) * (1 if b["id"] in FIXED_TYPE_BLOCKS else len(b["types"]))
            for b in catalog["blocks"]
        )
        print(f"plan: about {total} controller messages, live state only, nothing is stored")
        if not args.apply:
            print("dry run: add --apply to measure")
            return 0
        outcome: dict[str, Any] | None = None
        failure: BaseException | None = None
        try:
            outcome = await Calibration(device, slot, catalog).run()
        except (NanocoreError, RuntimeError, OSError) as exc:
            failure = exc
            print(f"measurement stopped: {exc}", file=sys.stderr)
        finally:
            print("restoring the original live state ...", flush=True)
            try:
                restored = await device.restore_active(original, safety)
                print(f"restored {restored['operations_applied']} operations, read-back verified")
            except Exception as exc:
                print(f"RESTORE FAILED: {exc}\nthe original state is in {backup}", file=sys.stderr)
                failure = failure or exc
        if outcome is not None:
            outcome.update(
                captured_at=datetime.now(UTC).isoformat(timespec="seconds"),
                preset_name=name,
            )
            Path(args.output).write_text(json.dumps(outcome, indent=1) + "\n", encoding="utf-8")
            print(f"measurements written to {args.output}")
        return 0 if failure is None else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", help="ALSA MIDI port, e.g. hw:6,0,0 (default: discovered)")
    parser.add_argument("--catalog", default="docs/calibration/editor-catalog.json")
    parser.add_argument("--output", default="docs/calibration/measurements.json")
    parser.add_argument("--backup", required=True, help="new file: the state before measuring")
    parser.add_argument("--safety-backup", help="new file written before the final restore")
    parser.add_argument("--apply", action="store_true", help="really send the controllers")
    return asyncio.run(calibrate(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
