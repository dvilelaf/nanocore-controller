"""Validation and byte-exact planning for safe NANOCORE BLE restores."""

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .ble_midi import BleMidiSession
from .config import DEFAULT_ADAPTER
from .errors import (
    PartialApplyError,
    ProtocolError,
    ValidationError,
    VerificationError,
)
from .nanocore_protocol import (
    AMP_SLOT_COMMAND,
    EFFECT_COUNT,
    IR_SLOT_COMMAND,
    MAX_ASSET_SLOT,
    MAX_EFFECT_PARAMS,
    MAX_PRESET_SLOT,
    MAX_PRESET_VOLUME,
    MAX_VARIANT,
    RUNTIME_SNAPSHOT_COMMAND,
    SET_LIVE_FIELD_COMMAND,
    amp_slot_payload,
    chain_order_payload,
    ir_slot_payload,
    live_enabled_payload,
    live_variant_payload,
    parse_asset_slot,
    parse_runtime_snapshot,
    preset_volume_payload,
)
from .session import SysexSession

# Live-edit fields address the eight runtime entries by their zero-based
# snapshot position.  The unrelated 1..8 ``effectId`` values used by exported
# preset files must never be sent here (the pedal rejects 8 with status 0x03).
RUNTIME_EFFECT_INDICES = tuple(range(8))


@dataclass(frozen=True)
class RestoreOperation:
    description: str
    command: int
    payload: bytes


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValidationError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _hex_bytes(value: Any, what: str) -> bytes:
    if not isinstance(value, str):
        raise ValidationError(f"backup is missing its {what}")
    try:
        return bytes.fromhex(value)
    except ValueError as exc:
        raise ValidationError(f"backup {what} is not valid hex") from exc


def validate_backup(document: Mapping[str, Any]) -> None:
    if not isinstance(document, Mapping):
        raise ValidationError("backup root must be an object")
    version = document.get("version")
    if document.get("format") != "nanocore-controller-backup" or type(version) is not int or version != 1:
        raise ValidationError("unsupported NANOCORE backup format or version")
    if not isinstance(document.get("device"), Mapping):
        raise ValidationError("backup device must be an object")
    preset = document.get("preset")
    if not isinstance(preset, Mapping):
        raise ValidationError("backup is missing its preset object")
    slot = _integer(preset.get("slot"), "preset slot", 0, MAX_PRESET_SLOT)
    volume = _integer(preset.get("preset_volume"), "preset volume", 0, MAX_PRESET_VOLUME)
    amp_slot = _integer(preset.get("amp_slot"), "amp slot", 0, MAX_ASSET_SLOT)
    ir_slot = _integer(preset.get("ir_slot"), "IR slot", 0, MAX_ASSET_SLOT)

    chain = preset.get("chain_order")
    if (
        not isinstance(chain, list)
        or any(type(entry) is not int for entry in chain)
        or sorted(chain) != list(range(EFFECT_COUNT))
    ):
        raise ValidationError("chain order must be a permutation of 0 through 7")

    effects = preset.get("effects")
    if not isinstance(effects, list) or len(effects) != EFFECT_COUNT:
        raise ValidationError("backup must contain exactly eight effects")
    for effect in effects:
        if not isinstance(effect, Mapping):
            raise ValidationError("every backup effect must be an object")
    if [effect.get("index") if type(effect.get("index")) is int else None for effect in effects] != list(
        range(EFFECT_COUNT)
    ):
        raise ValidationError("effect indices must be 0 through 7 in order")
    for index, effect in enumerate(effects):
        if not isinstance(effect.get("enabled"), bool):
            raise ValidationError(f"effect {index} enabled must be a boolean")
        _integer(effect.get("variant"), f"effect {index} variant", 0, MAX_VARIANT)
        params = effect.get("params")
        if not isinstance(params, list) or len(params) > MAX_EFFECT_PARAMS:
            raise ValidationError(
                f"effect {index} params must be a list with at most {MAX_EFFECT_PARAMS} values"
            )
        for value in params:
            if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
                raise ValidationError(f"effect {index} parameters must be finite numbers")
            if not 0.0 <= value <= 1.0:
                raise ValidationError(f"effect {index} parameters must be from 0.0 to 1.0")

    try:
        snapshot = parse_runtime_snapshot(_hex_bytes(document.get("raw_snapshot"), "raw snapshot"))
    except ProtocolError as exc:
        raise ValidationError(f"backup raw snapshot is invalid: {exc}") from exc
    expected_effects = [
        (effect["enabled"], effect["variant"], tuple(effect["params"])) for effect in effects
    ]
    actual_effects = [
        (effect.enabled, effect.variant, effect.params) for effect in snapshot.effects
    ]
    agrees = (
        snapshot.active_preset == slot
        and snapshot.preset_volume == volume
        and list(snapshot.chain_order) == chain
        and actual_effects == expected_effects
    )
    if not agrees:
        raise ValidationError("backup JSON does not agree with its raw snapshot")

    assets = document.get("assets")
    if not isinstance(assets, Mapping):
        raise ValidationError("backup assets must be an object")
    for key, target_slot in (("amp", amp_slot), ("ir", ir_slot)):
        metadata = assets.get(key)
        if not isinstance(metadata, Mapping):
            raise ValidationError(f"backup {key} metadata is missing")
        try:
            parsed = parse_asset_slot(_hex_bytes(metadata.get("raw"), f"{key} metadata"))
        except ProtocolError as exc:
            raise ValidationError(f"backup {key} metadata is invalid: {exc}") from exc
        if (
            type(metadata.get("slot")) is not int
            or metadata["slot"] != target_slot
            or parsed.slot != target_slot
            or not parsed.present
            or not parsed.active
        ):
            raise ValidationError(f"backup {key} slot does not agree with its raw metadata")


def build_restore_plan(document: Mapping[str, Any]) -> tuple[RestoreOperation, ...]:
    """Return a deterministic, non-executing restore plan from a validated backup."""

    validate_backup(document)
    preset = document["preset"]
    effects = preset["effects"]
    operations = [
        RestoreOperation("select amplifier slot", SET_LIVE_FIELD_COMMAND, amp_slot_payload(preset["amp_slot"])),
        RestoreOperation("select cabinet/IR slot", SET_LIVE_FIELD_COMMAND, ir_slot_payload(preset["ir_slot"])),
    ]
    for effect_index, effect in zip(RUNTIME_EFFECT_INDICES, effects, strict=True):
        operations.append(
            RestoreOperation(
                f"effect {effect_index}: variant {effect['variant']} and {len(effect['params'])} parameters",
                SET_LIVE_FIELD_COMMAND,
                live_variant_payload(effect_index, effect["variant"], effect["params"]),
            )
        )
    for effect_index, effect in zip(RUNTIME_EFFECT_INDICES, effects, strict=True):
        operations.append(
            RestoreOperation(
                f"effect {effect_index}: {'enable' if effect['enabled'] else 'disable'}",
                SET_LIVE_FIELD_COMMAND,
                live_enabled_payload(effect_index, effect["enabled"]),
            )
        )
    operations.extend(
        (
            RestoreOperation(
                "set preset volume",
                SET_LIVE_FIELD_COMMAND,
                preset_volume_payload(preset["preset_volume"]),
            ),
            RestoreOperation(
                "set effect chain order",
                SET_LIVE_FIELD_COMMAND,
                chain_order_payload(preset["chain_order"]),
            ),
        )
    )
    return tuple(operations)


def _runtime_agrees(document: Mapping[str, Any], raw: bytes) -> bool:
    target = parse_runtime_snapshot(bytes.fromhex(document["raw_snapshot"]))
    actual = parse_runtime_snapshot(raw)
    return (
        actual.active_preset == target.active_preset
        and actual.preset_volume == target.preset_volume
        and actual.effects == target.effects
        and actual.chain_order == target.chain_order
    )


RESTORE_ATTEMPTS = 3
RETRY_DELAY = 0.05
# Switching the amplifier or the IR takes the pedal about half a second to load (measured on hardware),
# so a slot is polled for up to three seconds before it is reported as not selected.
ASSET_SETTLE_POLLS = 30
ASSET_SETTLE_INTERVAL = 0.1


async def apply_restore(
    document: Mapping[str, Any],
    address: str,
    *,
    adapter: str = DEFAULT_ADAPTER,
    session_factory: Callable[..., SysexSession] = BleMidiSession,
) -> dict[str, Any]:
    """Apply a validated plan, then independently read back every restored field."""

    plan = build_restore_plan(document)
    session = session_factory(address, adapter=adapter)
    await session.connect()
    try:
        return await apply_restore_plan(document, session, plan)
    finally:
        await session.close()


async def apply_restore_with_session(
    document: Mapping[str, Any],
    session: SysexSession,
) -> dict[str, Any]:
    """Apply and verify a restore using an already connected session."""

    plan = build_restore_plan(document)
    return await apply_restore_plan(document, session, plan)


async def write_plan(
    session: SysexSession,
    plan: tuple[RestoreOperation, ...],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Send every operation of ``plan`` in order.

    Live-field writes are idempotent, so a timed-out operation is retried. When
    an operation cannot be delivered the device is left partly modified, which
    is reported as ``PartialApplyError`` with the number of operations applied.
    """

    for applied, operation in enumerate(plan):
        for attempt in range(RESTORE_ATTEMPTS):
            try:
                await session.query(operation.command, operation.payload)
                break
            except TimeoutError as exc:
                if attempt + 1 < RESTORE_ATTEMPTS:
                    await sleep(RETRY_DELAY)
                    continue
                raise _partial(applied, plan, operation, exc) from exc
            except Exception as exc:
                raise _partial(applied, plan, operation, exc) from exc


def _partial(
    applied: int,
    plan: tuple[RestoreOperation, ...],
    operation: RestoreOperation,
    cause: Exception,
) -> PartialApplyError:
    detail = str(cause) or type(cause).__name__
    return PartialApplyError(
        applied,
        len(plan),
        detail=f"operation {applied + 1} failed ({operation.description}): {detail}",
    )


async def _slot_became_active(
    session: SysexSession,
    command: int,
    slot: int,
    sleep: Callable[[float], Awaitable[None]],
) -> bool:
    for poll in range(ASSET_SETTLE_POLLS + 1):
        asset = parse_asset_slot((await session.query(command, bytes((slot,)))).payload)
        if asset.active and asset.slot == slot:
            return True
        if poll < ASSET_SETTLE_POLLS:
            await sleep(ASSET_SETTLE_INTERVAL)
    return False


async def verify_restore(
    document: Mapping[str, Any],
    session: SysexSession,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Read back the runtime state and both asset slots and compare with ``document``."""

    runtime = await session.query(RUNTIME_SNAPSHOT_COMMAND)
    if not _runtime_agrees(document, runtime.payload):
        raise VerificationError("NANOCORE read-back verification failed for runtime state")
    preset = document["preset"]
    if not await _slot_became_active(session, AMP_SLOT_COMMAND, preset["amp_slot"], sleep):
        raise VerificationError("NANOCORE read-back verification failed for amplifier slot")
    if not await _slot_became_active(session, IR_SLOT_COMMAND, preset["ir_slot"], sleep):
        raise VerificationError("NANOCORE read-back verification failed for cabinet/IR slot")


async def apply_restore_plan(
    document: Mapping[str, Any],
    session: SysexSession,
    plan: tuple[RestoreOperation, ...],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, Any]:
    await write_plan(session, plan, sleep=sleep)
    await verify_restore(document, session, sleep=sleep)
    return {"operations_applied": len(plan), "verified": True}
