"""Transport-independent control of one NANOCORE.

``NanocoreDevice`` owns one ``SysexSession`` (BLE-MIDI or USB MIDI) and one
lock. Every operation holds the lock, so a long-lived caller such as a server
can share a single device between many requests without interleaving frames.
Operations that write to the pedal run as protected tasks: if the caller is
cancelled, the operation still finishes (and releases the lock) instead of
stopping halfway.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, TypeVar

from . import assets_protocol as assets
from .backup_io import load_backup, write_backup
from .ble_restore import (
    ASSET_SETTLE_POLLS,
    apply_restore_plan,
    build_restore_plan,
    verify_restore,
    write_plan,
)
from .ble_state import (
    read_active_state_from_session,
    read_preset_catalog_from_session,
)
from .ead_install import decrypt_ead
from .errors import (
    PartialApplyError,
    ProtocolError,
    SlotChangedError,
    ValidationError,
    VerificationError,
)
from .nanocore_protocol import (
    FIRMWARE_104_SELECT_PRESET_COMMAND,
    GET_SETTINGS_COMMAND,
    MAX_ASSET_SLOT,
    RUNTIME_SNAPSHOT_COMMAND,
    SAVE_PRESET_COMMAND,
    SET_LIVE_FIELD_COMMAND,
    SET_SETTINGS_COMMAND,
    AssetSlot,
    GlobalSettings,
    NanocoreResponse,
    RuntimeSnapshot,
    amp_slot_payload,
    chain_order_payload,
    global_settings_payload,
    ir_slot_payload,
    live_enabled_payload,
    live_param_payload,
    live_variant_payload,
    parse_global_settings,
    parse_runtime_snapshot,
    preset_name_payload,
    preset_volume_payload,
    save_preset_payload,
    select_preset_payload,
)
from .safe_files import write_new
from .session import SysexSession

if TYPE_CHECKING:
    from .ble_midi import MidiEvent

T = TypeVar("T")

log = logging.getLogger("nanocore.device")
audit = logging.getLogger("nanocore.audit")


class NanocoreDevice:
    """One NANOCORE reached through any ``SysexSession``."""

    def __init__(
        self,
        session: SysexSession,
        *,
        device_info: Mapping[str, Any] | None = None,
        preset_readback_delay: float = 0.05,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._session = session
        self._device_info = device_info
        self._preset_readback_delay = preset_readback_delay
        self._sleep = sleep
        self._lock = asyncio.Lock()
        # Last amplifier and cabinet/IR slots seen, probed first on the next read.
        self._amp_hint: int | None = None
        self._ir_hint: int | None = None

    @property
    def session(self) -> SysexSession:
        return self._session

    @property
    def connected(self) -> bool:
        return self._session.connected

    async def connect(self) -> None:
        await self._session.connect()

    async def close(self) -> None:
        await self._session.close()

    async def __aenter__(self) -> "NanocoreDevice":
        try:
            await self.connect()
        except BaseException:
            await self._close_quietly()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc is None:
            await self.close()
        else:
            await self._close_quietly()

    async def _close_quietly(self) -> None:
        # Cleanup after a failure must not hide the original error.
        try:
            await self.close()
        except Exception:
            log.warning("closing the session after a failure also failed", exc_info=True)

    async def _protected(self, operation: Awaitable[T]) -> T:
        """Run a write operation to completion even if the caller is cancelled."""

        task = asyncio.ensure_future(operation)
        return await asyncio.shield(task)

    def events(self) -> AsyncIterator["MidiEvent"]:
        """Changes made on the pedal itself (knobs, preset switch)."""

        return self._session.events()

    async def read_live(self) -> RuntimeSnapshot:
        """The working state of the pedal in one query: the fast path for a UI."""

        async with self._lock:
            return await self._read_runtime()

    async def read_global_settings(self) -> GlobalSettings:
        """The pedal-wide settings: Bluetooth, loopback, input gain, volumes, MIDI channel."""

        async with self._lock:
            response = await self._read_query(GET_SETTINGS_COMMAND, b"", 5.0)
            return parse_global_settings(response.payload)

    async def write_global_settings(
        self,
        *,
        wireless_enabled: bool | None = None,
        loopback_enabled: bool | None = None,
        input_gain_db: int | None = None,
        usb_volume: int | None = None,
        bt_volume: int | None = None,
        midi_channel: int | None = None,
    ) -> GlobalSettings:
        """Change some of the pedal-wide settings and return what the pedal reports afterwards.

        Only the fields that are given change. The reply is checked against the request, and
        ``VerificationError`` is raised if the pedal did not take a value.
        """

        fields: dict[str, Any] = {
            "wireless_enabled": wireless_enabled,
            "loopback_enabled": loopback_enabled,
            "input_gain_db": input_gain_db,
            "usb_volume": usb_volume,
            "bt_volume": bt_volume,
            "midi_channel": midi_channel,
        }
        payload = global_settings_payload(**fields)  # validates before any I/O

        async def operation() -> GlobalSettings:
            async with self._lock:
                self._audit("global settings", ", ".join(f"{k}={v}" for k, v in fields.items() if v is not None))
                response = await self._session.query(SET_SETTINGS_COMMAND, payload)
                settings = parse_global_settings(response.payload)
            wrong = [name for name, wanted in fields.items() if wanted is not None and getattr(settings, name) != wanted]
            if wrong:
                raise VerificationError(f"the pedal did not take: {', '.join(wrong)}")
            return settings

        return await self._protected(operation())

    async def asset_storage(self, kind: assets.AssetKind) -> assets.StorageInfo:
        """How many slots the amplifier or IR storage has, its size limit and the active slot."""

        async with self._lock:
            return await self._asset_storage_locked(kind)

    async def _read_query(self, command: int, payload: bytes, timeout: float) -> NanocoreResponse:
        """A read-only query, asked again when its reply is lost.

        Over Bluetooth now and then a reply never arrives (seen on a pedal: a different slot each time,
        about one query in thirty). Reads change nothing, so asking again is safe. Writes are never retried.
        """

        for attempt in range(READ_ATTEMPTS):
            try:
                return await self._session.query(command, payload, timeout=timeout)
            except TimeoutError:
                if attempt + 1 == READ_ATTEMPTS:
                    raise
        raise AssertionError("unreachable")

    async def _asset_storage_locked(self, kind: assets.AssetKind) -> assets.StorageInfo:
        response = await self._read_query(kind.info, b"", _timeouts(kind).info)
        return assets.parse_storage_info(response.payload)

    async def list_assets(self, kind: assets.AssetKind) -> list[AssetSlot]:
        """The slot information of every slot of the amplifier or IR storage, in slot order."""

        async with self._lock:
            count = (await self._asset_storage_locked(kind)).slot_count
            return [await self._asset_info_locked(kind, slot) for slot in range(count)]

    async def _asset_info_locked(self, kind: assets.AssetKind, slot: int) -> AssetSlot:
        response = await self._read_query(kind.slot_info, assets.slot_info_payload(slot), _timeouts(kind).info)
        return assets.parse_slot_info(response.payload, slot)

    async def read_asset(self, kind: assets.AssetKind, slot: int) -> tuple[AssetSlot, bytes]:
        """Read the data of one amplifier or IR slot and check its length and CRC-32 (read only)."""

        assets.slot_info_payload(slot)  # validates the number before any I/O
        async with self._lock:
            return await self._read_asset_locked(kind, slot)

    async def _read_asset_locked(self, kind: assets.AssetKind, slot: int) -> tuple[AssetSlot, bytes]:
        storage = await self._asset_storage_locked(kind)
        if slot >= storage.slot_count:
            raise ValidationError(f"the {kind.name} storage has {storage.slot_count} slots")
        info = await self._asset_info_locked(kind, slot)
        if not info.present:
            raise ProtocolError(f"{kind.name} slot {slot} is empty")
        timeout = _timeouts(kind).read
        data = bytearray()
        for offset, wanted in assets.read_windows(info.size):
            response = await self._read_query(kind.read, assets.read_payload(slot, offset, wanted), timeout)
            chunk = assets.parse_read_chunk(response.payload, slot, offset, info.size)
            if not chunk.data:
                break
            data += chunk.data
        return info, assets.verify_slot_data(info, bytes(data))

    async def _reselect_asset_locked(self, kind: assets.AssetKind, slot: int) -> None:
        """Select ``slot`` again for the preset in use and wait for the pedal to load it."""

        if kind.name == "amp":
            payload = amp_slot_payload(slot) if slot <= MAX_ASSET_SLOT else None
        else:
            payload = ir_slot_payload(slot)
        if payload is None:  # drive models above slot 29 cannot be selected as an amplifier
            return
        await self._session.query(SET_LIVE_FIELD_COMMAND, payload)
        for _ in range(ASSET_SETTLE_POLLS):
            if (await self._asset_storage_locked(kind)).active_slot == slot:
                return
            await self._sleep(0.1)
        raise VerificationError(f"the {kind.name} slot {slot} was not selected again after the write")

    async def install_ead(
        self,
        slot: int,
        ead: bytes,
        *,
        decryptor: Sequence[str],
        safety_backup: Path | str,
        name: str | None = None,
    ) -> AssetSlot:
        """Decrypt an ``.ead`` file with the user's decryptor and write the model into an amplifier slot.

        The file and the decryptor's answer are checked before the pedal is touched; the write is
        then that of ``write_asset`` (previous content kept, read back, previous selection kept).
        """

        model = await asyncio.to_thread(decrypt_ead, ead, decryptor)
        return await self.write_asset(assets.AMP, slot, model, safety_backup=safety_backup, name=name)

    async def write_asset(
        self,
        kind: assets.AssetKind,
        slot: int,
        data: bytes,
        *,
        safety_backup: Path | str,
        name: str | None = None,
        keep_selection: bool = True,
    ) -> AssetSlot:
        """Replace the content of one amplifier or IR slot, after keeping what it holds.

        The old content goes to ``safety_backup`` (a new file) before anything is written. The
        write is a session of acknowledged chunks that is aborted if any step fails, and the slot
        is read back and compared (length, CRC-32, name) before this returns.

        The pedal makes a written slot the active one for the preset in use. With ``keep_selection``
        (the default) the slot that was active before is selected again, so writing a slot does not
        change the sound of the preset being played.
        """

        assets.slot_info_payload(slot)  # validates the number before any I/O
        if kind.name == "ir":
            assets.validate_ir(data)
        else:
            assets.validate_stored_amp(data)
        if name is not None:
            assets.encode_asset_name(name)
        steps = assets.write_sequence(kind, slot, data, name=name)  # validates sizes and names
        backup = Path(safety_backup)
        if backup.exists():
            raise ValidationError(f"{backup} already exists; nothing was overwritten")

        async def operation() -> AssetSlot:
            async with self._lock:
                storage = await self._asset_storage_locked(kind)
                if slot >= storage.slot_count:
                    raise ValidationError(f"the {kind.name} storage has {storage.slot_count} slots")
                if len(data) > storage.max_bytes:
                    raise ValidationError(f"{kind.name} data is larger than the {storage.max_bytes} bytes a slot holds")
                _, old = await self._read_asset_locked(kind, slot)
                write_new(backup, old)
                self._audit("ASSET WRITE", f"{kind.name} slot={slot} bytes={len(data)} backup={backup}")
                try:
                    for step in steps:
                        await self._session.query(step.command, step.payload, timeout=step.timeout)
                except BaseException:
                    with contextlib.suppress(Exception):
                        await self._session.query(kind.abort, b"", timeout=_timeouts(kind).abort)
                    raise
                info, stored = await self._read_asset_locked(kind, slot)
                if stored != data or (name is not None and info.name != name.rstrip(" ")):
                    raise VerificationError(
                        f"{kind.name} slot {slot} does not hold what was written; the previous content is in {backup}"
                    )
                self._audit("ASSET WRITE verified", f"{kind.name} slot={slot}")
                previous = storage.active_slot
                if keep_selection and previous is not None and previous != slot:
                    await self._reselect_asset_locked(kind, previous)
                return info

        return await self._protected(operation())

    async def read_state(self) -> dict[str, Any]:
        """The full active-preset document, in the format of a backup file."""

        async with self._lock:
            return await self._read_state_locked()

    async def _read_state_locked(self) -> dict[str, Any]:
        document = await read_active_state_from_session(
            self._session,
            device=self._device_info,
            amp_hint=self._amp_hint,
            ir_hint=self._ir_hint,
        )
        self._amp_hint = document["preset"]["amp_slot"]
        self._ir_hint = document["preset"]["ir_slot"]
        return document

    async def catalog(self) -> list[dict[str, Any]]:
        async with self._lock:
            return await read_preset_catalog_from_session(self._session)

    async def send_midi(self, message: bytes) -> None:
        """Send one raw MIDI message (the documented control-change protocol)."""

        if not message:
            raise ValidationError("MIDI message must not be empty")
        async with self._lock:
            await self._session.send(message)

    async def select_preset(self, display_number: int) -> RuntimeSnapshot:
        slot = _display_number_to_slot(display_number)

        async def operation() -> RuntimeSnapshot:
            async with self._lock:
                self._audit("select preset", f"slot={slot}")
                await self._session.query(
                    FIRMWARE_104_SELECT_PRESET_COMMAND, select_preset_payload(slot)
                )
                snapshot: RuntimeSnapshot | None = None
                for attempt in range(3):
                    snapshot = await self._read_runtime()
                    if snapshot.active_preset == slot:
                        return snapshot
                    if attempt < 2:
                        await self._sleep(self._preset_readback_delay)
                assert snapshot is not None
                raise VerificationError(
                    f"preset {display_number} read-back mismatch after 3 read-back attempts: "
                    f"expected slot {slot}, got {snapshot.active_preset}"
                )

        return await self._protected(operation())

    async def set_param(self, effect_index: int, param_index: int, value: float) -> None:
        await self._write_live(
            live_param_payload(effect_index, param_index, value),
            f"param effect={effect_index} index={param_index} value={value}",
        )

    async def set_enabled(self, effect_index: int, enabled: bool) -> None:
        await self._write_live(
            live_enabled_payload(effect_index, enabled),
            f"enabled effect={effect_index} enabled={enabled}",
        )

    async def set_variant(
        self, effect_index: int, variant: int, params: Sequence[float]
    ) -> None:
        await self._write_live(
            live_variant_payload(effect_index, variant, tuple(params)),
            f"variant effect={effect_index} variant={variant} params={len(params)}",
        )

    async def set_volume(self, volume: int) -> None:
        await self._write_live(preset_volume_payload(volume), f"volume {volume}")

    async def set_chain_order(self, order: Sequence[int]) -> None:
        await self._write_live(chain_order_payload(tuple(order)), f"chain order {list(order)}")

    async def select_amp(self, slot: int) -> None:
        await self._write_live(amp_slot_payload(slot), f"amp slot {slot}")
        self._amp_hint = slot

    async def select_ir(self, slot: int) -> None:
        await self._write_live(ir_slot_payload(slot), f"IR slot {slot}")
        self._ir_hint = slot

    async def backup_active(self, path: Path | str) -> dict[str, Any]:
        """Write the full active-preset document to a new file and return it.

        The file is created atomically with mode 0600, never replaces an
        existing one, and is read back and validated before this returns.
        """

        async def operation() -> dict[str, Any]:
            async with self._lock:
                document = await self._read_state_locked()
                return await self._store_backup(Path(path), document)

        return await self._protected(operation())

    async def set_preset_name(self, name: str) -> str:
        """Change the name of the active preset in the pedal's RAM and check what it reports back.

        Nothing is stored until the preset is saved; ``rename_active`` does both.
        """

        payload = preset_name_payload(name)  # validates before any I/O
        wanted = payload[2:].decode("ascii")

        async def operation() -> str:
            async with self._lock:
                self._audit("preset name", wanted)
                response = await self._session.query(SET_LIVE_FIELD_COMMAND, payload)
            echoed = response.payload
            shown = echoed[2 : 2 + echoed[1]].decode("ascii", "replace").rstrip(" ") if len(echoed) >= 2 else None
            if echoed[:1] != bytes([8]) or shown != wanted:
                raise VerificationError(f"the pedal reported the name {shown!r} instead of {wanted!r}")
            return wanted

        return await self._protected(operation())

    async def rename_active(self, name: str, backup_path: Path | str) -> str:
        """Rename the active preset and store it, then read the stored name back from the catalog.

        The flash write is that of ``save_active`` (backup first, active preset re-checked), so a
        rename changes the whole live state of the preset to what is stored, as a save does.
        """

        preset_name_payload(name)  # validates before any I/O
        wanted = await self.set_preset_name(name)
        snapshot = await self.save_active(backup_path)
        slot = snapshot.active_preset
        entries = await self.catalog()
        stored = next((e["name"].rstrip(" ") for e in entries if e["slot"] == slot), None)
        if stored != wanted:
            raise VerificationError(
                f"preset {slot + 1} was saved but the catalog shows {stored!r} instead of {wanted!r}"
            )
        return wanted

    async def save_active(self, backup_path: Path | str) -> RuntimeSnapshot:
        """Store the live state of the active preset in the pedal's flash.

        ``backup_path`` receives the live state that is about to be written. It
        does not preserve what the slot held before: that is the job of the
        baseline history kept before the first edit of a session.

        Nothing reaches the flash unless, in this order: the full state was
        read consistently, its backup was written and validated, and the
        active preset was confirmed unchanged immediately before the write.
        The slot is never an argument, so only the active preset can be saved.
        """

        async def operation() -> RuntimeSnapshot:
            async with self._lock:
                slot = (await self._read_runtime()).active_preset
                document = await self._read_state_locked()
                if document["preset"]["slot"] != slot:
                    raise SlotChangedError(slot, document["preset"]["slot"])
                await self._store_backup(Path(backup_path), document)
                before = await self._read_runtime()
                if before.active_preset != slot:
                    raise SlotChangedError(slot, before.active_preset)
                self._audit("FLASH WRITE", f"slot={slot} backup={backup_path}")
                await self._session.query(SAVE_PRESET_COMMAND, save_preset_payload(slot))
                after = await self._read_runtime()
                if after.active_preset != slot:
                    raise SlotChangedError(slot, after.active_preset)
                if _live_state(after) != _live_state(before):
                    raise VerificationError(
                        f"the state of slot {slot + 1} changed while it was being saved; "
                        f"the stored copy may differ from the backup {backup_path}"
                    )
                self._audit("FLASH WRITE verified", f"slot={slot}")
                return after

        return await self._protected(operation())

    async def restore_active(
        self, document: Mapping[str, Any], safety_backup_path: Path | str
    ) -> dict[str, Any]:
        """Apply a backup to the live state of the active preset, then verify it.

        The current state is written to ``safety_backup_path`` first. A failure
        after the first write raises ``PartialApplyError`` and the previous
        state is put back on a best-effort basis. Live state only: nothing is
        stored permanently.
        """

        plan = build_restore_plan(document)  # validates before any I/O

        async def operation() -> dict[str, Any]:
            async with self._lock:
                target = document["preset"]["slot"]
                current = await self._read_state_locked()
                if current["preset"]["slot"] != target:
                    raise ValidationError(
                        f"backup targets preset slot {target + 1}, but that slot is not active"
                    )
                path = Path(safety_backup_path)
                await self._store_backup(path, current)
                active = (await self._read_runtime()).active_preset
                if active != target:
                    raise SlotChangedError(target, active)
                self._audit("restore", f"slot={target} operations={len(plan)} safety={path}")
                try:
                    result = await apply_restore_plan(
                        document, self._session, plan, sleep=self._sleep
                    )
                except (PartialApplyError, VerificationError) as exc:
                    restored, outcome = await self._rollback(path)
                    failure: VerificationError = exc
                    if isinstance(exc, PartialApplyError):
                        failure = PartialApplyError(exc.applied, exc.total, path, exc.detail)
                    failure.add_note(outcome)
                    failure.rolled_back = restored
                    raise failure from exc
                self._amp_hint = document["preset"]["amp_slot"]
                self._ir_hint = document["preset"]["ir_slot"]
                return result

        return await self._protected(operation())

    async def _store_backup(self, path: Path, document: dict[str, Any]) -> dict[str, Any]:
        await asyncio.to_thread(write_backup, path, document)
        stored = await asyncio.to_thread(load_backup, path)
        self._audit("backup written", str(path))
        return stored

    async def _rollback(self, safety_backup: Path) -> tuple[bool, str]:
        try:
            previous = await asyncio.to_thread(load_backup, safety_backup)
            await write_plan(self._session, build_restore_plan(previous), sleep=self._sleep)
            await verify_restore(previous, self._session)
        except Exception as exc:
            log.error("rollback from %s failed: %s", safety_backup, exc)
            return False, f"rolling back to {safety_backup} also failed: {exc}"
        self._audit("rollback", f"restored {safety_backup}")
        return True, f"the previous state was put back from {safety_backup}"

    async def _write_live(self, payload: bytes, description: str) -> None:
        # The payload was validated by the builder before any I/O happens.
        async def operation() -> None:
            async with self._lock:
                self._audit("live write", description)
                await self._session.query(SET_LIVE_FIELD_COMMAND, payload)

        await self._protected(operation())

    async def _read_runtime(self) -> RuntimeSnapshot:
        response = await self._session.query(RUNTIME_SNAPSHOT_COMMAND)
        return parse_runtime_snapshot(response.payload)

    def _audit(self, action: str, detail: str = "") -> None:
        audit.info("%s %s", action, detail)


READ_ATTEMPTS = 3


def _timeouts(kind: assets.AssetKind) -> assets.Timeouts:
    return assets.AMP_TIMEOUTS if kind.name == "amp" else assets.IR_TIMEOUTS


def _display_number_to_slot(display_number: object) -> int:
    if (
        isinstance(display_number, bool)
        or not isinstance(display_number, int)
        or not 1 <= display_number <= 128
    ):
        raise ValidationError("preset display number must be an integer from 1 to 128")
    return display_number - 1


def _live_state(snapshot: RuntimeSnapshot) -> tuple[object, ...]:
    return (snapshot.preset_volume, snapshot.effects, snapshot.chain_order)
