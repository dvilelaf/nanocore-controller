"""Read-only high-level state and backup helpers for a NANOCORE pedal."""

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from .ble_midi import BleMidiSession
from .config import DEFAULT_ADAPTER
from .errors import DeviceStatusError, DeviceTimeout, ProtocolError, VerificationError
from .nanocore_protocol import (
    AMP_SLOT_COMMAND,
    IR_SLOT_COMMAND,
    MAX_ASSET_SLOT,
    PRESET_CATALOG_COMMAND,
    RUNTIME_SNAPSHOT_COMMAND,
    AssetSlot,
    PresetSummary,
    RuntimeSnapshot,
    parse_asset_slot,
    parse_preset_summaries,
    parse_runtime_snapshot,
)
from .session import SysexSession

log = logging.getLogger("nanocore.state")

ASSET_SCAN_PASSES = 2
ASSET_PROBE_TIMEOUT = 1.0
STATE_READ_ATTEMPTS = 3
# Firmware 1.04 reports this status for a slot that is momentarily unavailable
# right after a preset recall; it is treated like an empty slot.
SLOT_UNAVAILABLE_STATUS = 0x03
# Runtime effect index -> the effect id used in exported presets.
EFFECT_IDS = (7, 8, 1, 2, 5, 4, 3, 6)

__all__ = [
    "AMP_SLOT_COMMAND",
    "IR_SLOT_COMMAND",
    "PRESET_CATALOG_COMMAND",
    "RUNTIME_SNAPSHOT_COMMAND",
    "read_active_state",
    "read_active_state_from_session",
    "read_preset_catalog",
    "read_preset_catalog_from_session",
]


async def read_preset_catalog(
    address: str,
    *,
    adapter: str = DEFAULT_ADAPTER,
    session_factory: Callable[..., SysexSession] = BleMidiSession,
) -> list[dict[str, Any]]:
    """Read every available preset name without recalling any preset."""

    session = session_factory(address, adapter=adapter)
    await session.connect()
    try:
        return await read_preset_catalog_from_session(session)
    finally:
        await session.close()


async def read_preset_catalog_from_session(
    session: SysexSession,
) -> list[dict[str, Any]]:
    """Read every preset name using an already connected session."""

    entries: list[PresetSummary] = []
    for start in range(0, 128, 6):
        for attempt in range(2):
            try:
                response = await session.query(PRESET_CATALOG_COMMAND, bytes([start, 6]))
                break
            except TimeoutError:
                if attempt == 1:
                    raise
        page = parse_preset_summaries(response.payload)
        entries.extend(page.entries)
        if len(page.entries) < 6:
            break
    return [
        {
            "slot": entry.slot,
            "display_number": entry.slot + 1,
            "flags": entry.flags,
            "name": entry.name,
        }
        for entry in entries
    ]


async def _find_active_asset(
    session: SysexSession,
    command: int,
    *,
    hint: int | None = None,
) -> AssetSlot:
    """Find the active amplifier or cabinet/IR slot.

    The slot found last time is probed first, which makes the usual case a
    single query. Otherwise every slot is tried: the firmware leaves empty
    holes silent, so each costs a timeout.
    """

    slots = list(range(MAX_ASSET_SLOT + 1))
    if hint is not None and hint in slots:
        slots.remove(hint)
        slots.insert(0, hint)
    pending = slots
    for _ in range(ASSET_SCAN_PASSES):
        silent: list[int] = []
        for slot in pending:
            try:
                response = await session.query(command, bytes([slot]), timeout=ASSET_PROBE_TIMEOUT)
            except TimeoutError:
                silent.append(slot)
                continue
            except DeviceStatusError as exc:
                if exc.status != SLOT_UNAVAILABLE_STATUS:
                    raise
                continue
            asset = parse_asset_slot(response.payload)
            if asset.slot != slot:
                raise ProtocolError(
                    f"asked for asset slot {slot} with command 0x{command:02x}, "
                    f"got slot {asset.slot}"
                )
            if asset.active:
                return asset
        if len(silent) == len(slots):
            # Empty slots stay silent, but when every one does the pedal itself is not answering.
            raise DeviceTimeout(command, maybe_applied=False, timeout=ASSET_PROBE_TIMEOUT)
        if not silent:
            break
        # A reply can be lost on the link; only the slots that stayed silent are asked again.
        pending = silent
    raise ProtocolError(f"active asset slot not found for command 0x{command:02x}")


async def read_active_state(
    address: str,
    *,
    adapter: str = DEFAULT_ADAPTER,
    session_factory: Callable[..., SysexSession] = BleMidiSession,
) -> dict[str, Any]:
    """Read a complete active-preset snapshot without changing pedal state."""

    session = session_factory(address, adapter=adapter)
    await session.connect()
    try:
        return await read_active_state_from_session(session)
    finally:
        await session.close()


async def read_active_state_from_session(
    session: SysexSession,
    *,
    device: Mapping[str, Any] | None = None,
    amp_hint: int | None = None,
    ir_hint: int | None = None,
    consistent: bool = True,
) -> dict[str, Any]:
    """Read active state using an already connected session.

    ``device`` is the connection metadata stored in the backup (a Bluetooth
    address and adapter, or a USB port). When it is not given, the session's
    own ``address`` and ``adapter`` attributes are used if it has them.

    The state takes several queries, so a preset change or knob turn on the
    pedal in between would mix two states. With ``consistent`` the runtime
    snapshot is read again at the end and the whole read is repeated if it
    differs; ``VerificationError`` is raised if it never settles.
    """

    if device is None:
        device = {
            "address": getattr(session, "address", None),
            "adapter": getattr(session, "adapter", None),
        }
    for attempt in range(1, STATE_READ_ATTEMPTS + 1):
        response = await session.query(RUNTIME_SNAPSHOT_COMMAND)
        snapshot = parse_runtime_snapshot(response.payload)
        page_start = (snapshot.active_preset // 6) * 6
        page_response = await session.query(PRESET_CATALOG_COMMAND, bytes([page_start, 6]))
        page = parse_preset_summaries(page_response.payload)
        summary = next(
            (entry for entry in page.entries if entry.slot == snapshot.active_preset), None
        )
        amp = await _find_active_asset(session, AMP_SLOT_COMMAND, hint=amp_hint)
        ir = await _find_active_asset(session, IR_SLOT_COMMAND, hint=ir_hint)
        if not consistent:
            break
        if (await session.query(RUNTIME_SNAPSHOT_COMMAND)).payload == response.payload:
            break
        log.info("pedal state changed while it was being read (attempt %d)", attempt)
    else:
        raise VerificationError("the pedal state kept changing while it was being read")
    return _state_document(device, snapshot, summary, amp, ir, response.payload)


def _state_document(
    device: Mapping[str, Any],
    snapshot: RuntimeSnapshot,
    summary: PresetSummary | None,
    amp: AssetSlot,
    ir: AssetSlot,
    raw_snapshot: bytes,
) -> dict[str, Any]:
    return {
        "format": "nanocore-controller-backup",
        "version": 1,
        "captured_at": datetime.now(UTC).isoformat(),
        "device": dict(device),
        "preset": {
            "slot": snapshot.active_preset,
            "display_number": snapshot.active_preset + 1,
            "name": summary.name if summary else None,
            "preset_volume": snapshot.preset_volume,
            "amp_slot": amp.slot,
            "ir_slot": ir.slot,
            "chain_order": list(snapshot.chain_order),
            "effects": [
                {
                    "index": index,
                    "effect_id": EFFECT_IDS[index],
                    "enabled": effect.enabled,
                    "variant": effect.variant,
                    "params": list(effect.params),
                }
                for index, effect in enumerate(snapshot.effects)
            ],
        },
        "assets": {"amp": _asset_entry(amp), "ir": _asset_entry(ir)},
        "raw_snapshot": raw_snapshot.hex(" "),
    }


def _asset_entry(asset: AssetSlot) -> dict[str, Any]:
    return {
        "slot": asset.slot,
        "name": asset.name,
        "size": asset.size,
        "checksum": asset.checksum,
        "format_id": asset.format_id,
        "raw": asset.raw.hex(" "),
    }
