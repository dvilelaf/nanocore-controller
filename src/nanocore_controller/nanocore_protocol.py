"""Undocumented NANOCORE SysEx request/response protocol."""

import math
import struct
import zlib
from dataclasses import dataclass

from .errors import ProtocolError, ValidationError

SYSEX_PREFIX = bytes.fromhex("f0 7d 4e 43")
HOST_TO_DEVICE = 0x70
DEVICE_TO_HOST = 0x71
SET_LIVE_FIELD_COMMAND = 0x6D
SELECT_PRESET_COMMAND = 0x76
SAVE_PRESET_COMMAND = 0x46
RUNTIME_SNAPSHOT_COMMAND = 0x63
PRESET_CATALOG_COMMAND = 0x40
AMP_SLOT_COMMAND = 0x36
IR_SLOT_COMMAND = 0x56
PRESET_CAPABILITIES_COMMAND = 0x45
GET_SETTINGS_COMMAND = 0x65  # global settings; empty payload
SET_SETTINGS_COMMAND = 0x66  # global settings; field mask + values; answers with the new settings
# Firmware 1.04 exposes preset selection as field 0x09 on the live-field
# command.  The newer 0x76 variant uses the identical two-byte payload.
FIRMWARE_104_SELECT_PRESET_COMMAND = SET_LIVE_FIELD_COMMAND

EFFECT_COUNT = 8
MAX_EFFECT_INDEX = EFFECT_COUNT - 1
MAX_EFFECT_PARAMS = 24
MAX_PARAM_INDEX = MAX_EFFECT_PARAMS - 1
MAX_VARIANT = 0xFF
MAX_ASSET_SLOT = 29
MAX_PRESET_SLOT = 127
MAX_PRESET_VOLUME = 100
MAX_SETTING_VOLUME = 100
MAX_MIDI_CHANNEL = 16  # 0 = omni
MAX_INPUT_GAIN_DB = 20
PRESET_NAME_FIELD = 8  # live field 0x08 of command 0x6d: the name of the active preset
MAX_PRESET_NAME_LENGTH = 8


def _integer(value: object, name: str, maximum: int, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValidationError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _normalized(value: float) -> float:
    if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
        raise ValidationError("parameter value must be a finite number")
    if not 0.0 <= value <= 1.0:
        raise ValidationError("parameter value must be from 0.0 to 1.0")
    return float(value)


def validate_display_number(value: int) -> int:
    """Check a one-based preset number as shown on the pedal (1 to 128)."""

    if type(value) is not int or not 1 <= value <= MAX_PRESET_SLOT + 1:
        raise ValidationError("preset display number must be an integer from 1 to 128")
    return value


def select_preset_payload(slot: int) -> bytes:
    """Build the NanoCore preset-recall payload for a zero-based slot."""

    return bytes((0x09, _integer(slot, "preset slot", MAX_PRESET_SLOT)))


def save_preset_payload(slot: int) -> bytes:
    """Build the persistent-save payload for a zero-based preset slot."""

    return bytes((_integer(slot, "preset slot", MAX_PRESET_SLOT),))


def live_param_payload(effect_index: int, param_index: int, value: float) -> bytes:
    """Build native field 1: one exact normalized live parameter."""

    return bytes(
        (
            1,
            _integer(effect_index, "effect index", MAX_EFFECT_INDEX),
            _integer(param_index, "parameter index", MAX_PARAM_INDEX),
        )
    ) + struct.pack("<f", _normalized(value))


def live_variant_payload(effect_index: int, variant: int, params: list[float] | tuple[float, ...]) -> bytes:
    """Build native field 2: effect variant plus its complete normalized parameter array."""

    if not isinstance(params, (list, tuple)) or len(params) > MAX_EFFECT_PARAMS:
        raise ValidationError(f"a live variant needs a list of at most {MAX_EFFECT_PARAMS} parameters")
    values = tuple(_normalized(value) for value in params)
    return bytes(
        (
            2,
            _integer(effect_index, "effect index", MAX_EFFECT_INDEX),
            _integer(variant, "variant", MAX_VARIANT),
            len(values),
        )
    ) + struct.pack(f"<{len(values)}f", *values)


def live_enabled_payload(effect_index: int, enabled: bool) -> bytes:
    if not isinstance(enabled, bool):
        raise ValidationError("enabled must be a boolean")
    return bytes((3, _integer(effect_index, "effect index", MAX_EFFECT_INDEX), int(enabled)))


def preset_volume_payload(volume: int) -> bytes:
    return bytes((4, _integer(volume, "preset volume", MAX_PRESET_VOLUME)))


def chain_order_payload(order: list[int] | tuple[int, ...]) -> bytes:
    if (
        not isinstance(order, (list, tuple))
        or any(type(entry) is not int for entry in order)
        or sorted(order) != list(range(EFFECT_COUNT))
    ):
        raise ValidationError("chain order must be a permutation of 0 through 7")
    return bytes((5, EFFECT_COUNT, *order))


def amp_slot_payload(slot: int) -> bytes:
    return bytes((6, _integer(slot, "amp slot", MAX_ASSET_SLOT)))


def ir_slot_payload(slot: int) -> bytes:
    return bytes((7, _integer(slot, "IR slot", MAX_ASSET_SLOT)))


@dataclass(frozen=True)
class NanocoreResponse:
    sequence: int
    command: int
    status: int
    payload: bytes


@dataclass(frozen=True)
class EffectSnapshot:
    enabled: bool
    variant: int
    params: tuple[float, ...]


@dataclass(frozen=True)
class RuntimeSnapshot:
    version: int
    active_preset: int
    preset_volume: int
    effects: tuple[EffectSnapshot, ...]
    chain_order: tuple[int, ...] = ()
    trailing_bytes: bytes = b""


@dataclass(frozen=True)
class PresetCapabilities:
    version: int
    record_size: int
    flags: int
    preset_count: int


@dataclass(frozen=True)
class PresetRecord:
    version: int
    flags: int
    present: bool
    order: tuple[int, ...]
    raw: bytes


@dataclass(frozen=True)
class PresetSummary:
    slot: int
    flags: int
    name: str


@dataclass(frozen=True)
class PresetSummaryPage:
    start: int
    entries: tuple[PresetSummary, ...]


@dataclass(frozen=True)
class AssetSlot:
    slot: int
    present: bool
    active: bool
    size: int
    checksum: int
    format_id: int
    name: str
    raw: bytes


class ResponseAssembler:
    """Reassemble protocol-level streamed responses (status 0x10/0x11)."""

    def __init__(self) -> None:
        self._streams: dict[tuple[int, int], tuple[int, bytearray, bytearray]] = {}

    def feed(self, response: NanocoreResponse) -> NanocoreResponse | None:
        if response.status not in (0x10, 0x11):
            return response
        key = (response.sequence, response.command)
        try:
            return self._feed_chunk(key, response)
        except ProtocolError:
            self._streams.pop(key, None)
            raise

    def _feed_chunk(self, key: tuple[int, int], response: NanocoreResponse) -> NanocoreResponse | None:
        if len(response.payload) < 4:
            raise ProtocolError("truncated streamed NANOCORE response")
        total = int.from_bytes(response.payload[:2], "little")
        offset = int.from_bytes(response.payload[2:4], "little")
        chunk = response.payload[4:]
        if total == 0:
            raise ProtocolError("streamed NANOCORE response declares no data")
        if offset + len(chunk) > total:
            raise ProtocolError("streamed NANOCORE response exceeds declared length")
        if key not in self._streams:
            self._streams[key] = (total, bytearray(total), bytearray(total))
        stream_total, data, received = self._streams[key]
        if stream_total != total:
            raise ProtocolError("streamed NANOCORE response length changed")
        end = offset + len(chunk)
        if any(received[offset:end]) and data[offset:end] != chunk:
            raise ProtocolError("streamed NANOCORE response chunks overlap with different data")
        data[offset:end] = chunk
        received[offset:end] = b"\1" * len(chunk)
        if response.status == 0x11:
            if not all(received):
                raise ProtocolError("final NANOCORE response chunk arrived before all data")
            del self._streams[key]
            return NanocoreResponse(response.sequence, response.command, 0, bytes(data))
        return None


def pack_7bit(data: bytes) -> bytes:
    """Encode arbitrary bytes as groups of one high-bit mask plus seven data bytes."""

    packed = bytearray()
    for offset in range(0, len(data), 7):
        group = data[offset : offset + 7]
        mask = sum(((byte >> 7) & 1) << index for index, byte in enumerate(group))
        packed.append(mask)
        packed.extend(byte & 0x7F for byte in group)
    return bytes(packed)


def unpack_7bit(data: bytes) -> bytes:
    unpacked = bytearray()
    offset = 0
    while offset < len(data):
        mask = data[offset]
        offset += 1
        group = data[offset : offset + 7]
        if not group:
            raise ProtocolError("truncated 7-bit group")
        if mask >= 0x80 or any(byte >= 0x80 for byte in group):
            raise ProtocolError("7-bit data contains a byte with the high bit set")
        if mask >> len(group):
            raise ProtocolError("7-bit group mask covers bytes that are missing")
        unpacked.extend(byte | (((mask >> index) & 1) << 7) for index, byte in enumerate(group))
        offset += len(group)
    return bytes(unpacked)


def encode_request(command: int, payload: bytes = b"", *, sequence: int) -> bytes:
    _integer(command, "command", 0xFFFF)
    _integer(sequence, "sequence", 0xFFFF)
    if not isinstance(payload, (bytes, bytearray)) or len(payload) > 0xFFFF:
        raise ValidationError("payload must be at most 65535 bytes")
    raw = struct.pack("<BHHH", 2, sequence, command, len(payload)) + payload
    return SYSEX_PREFIX + bytes([HOST_TO_DEVICE]) + pack_7bit(raw) + bytes([0xF7])


def decode_response(message: bytes) -> NanocoreResponse:
    expected_prefix = SYSEX_PREFIX + bytes([DEVICE_TO_HOST])
    if not message.startswith(expected_prefix) or not message.endswith(bytes([0xF7])):
        raise ProtocolError("not a NANOCORE response SysEx message")
    raw = unpack_7bit(message[len(expected_prefix) : -1])
    if len(raw) < 8 or raw[0] != 2:
        raise ProtocolError("invalid NANOCORE response header")
    sequence, command = struct.unpack_from("<HH", raw, 1)
    status = raw[5]
    payload_length = int.from_bytes(raw[6:8], "little")
    if len(raw) != 8 + payload_length:
        raise ProtocolError("NANOCORE response payload length mismatch")
    return NanocoreResponse(sequence, command, status, raw[8:])


def parse_runtime_snapshot(payload: bytes) -> RuntimeSnapshot:
    if len(payload) < 3 or payload[0] != 3:
        raise ProtocolError("unsupported runtime snapshot")
    if payload[1] > MAX_PRESET_SLOT:
        raise ProtocolError("runtime snapshot active preset is out of range")
    if payload[2] > MAX_PRESET_VOLUME:
        raise ProtocolError("runtime snapshot preset volume is out of range")
    cursor = 3
    effects = []
    for _ in range(EFFECT_COUNT):
        if cursor + 3 > len(payload):
            raise ProtocolError("truncated runtime snapshot effect")
        enabled, variant, param_count = payload[cursor : cursor + 3]
        if enabled not in (0, 1) or param_count > MAX_EFFECT_PARAMS:
            raise ProtocolError("invalid runtime snapshot effect")
        cursor += 3
        params_end = cursor + param_count * 4
        if params_end > len(payload):
            raise ProtocolError("truncated runtime snapshot parameters")
        params = struct.unpack_from(f"<{param_count}f", payload, cursor) if param_count else ()
        if not all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in params):
            raise ProtocolError("runtime snapshot parameter is not a finite value from 0.0 to 1.0")
        effects.append(EffectSnapshot(bool(enabled), variant, tuple(params)))
        cursor = params_end
    remainder = payload[cursor:]
    chain_order: tuple[int, ...] = ()
    if remainder:
        if remainder[0] != EFFECT_COUNT or len(remainder) < EFFECT_COUNT + 1:
            raise ProtocolError("runtime snapshot chain order must list eight effects")
        chain_order = tuple(remainder[1 : EFFECT_COUNT + 1])
        if sorted(chain_order) != list(range(EFFECT_COUNT)):
            raise ProtocolError("runtime snapshot chain order is not a permutation of 0 through 7")
        remainder = remainder[EFFECT_COUNT + 1 :]
    return RuntimeSnapshot(
        payload[0], payload[1], payload[2], tuple(effects), chain_order, remainder
    )


def parse_preset_summaries(payload: bytes) -> PresetSummaryPage:
    if len(payload) < 2:
        raise ProtocolError("truncated preset summary page")
    start, count = payload[:2]
    expected_length = 2 + count * 10
    if len(payload) != expected_length:
        raise ProtocolError("preset summary page length mismatch")
    entries = []
    for offset in range(2, expected_length, 10):
        slot, flags = payload[offset : offset + 2]
        name = payload[offset + 2 : offset + 10].decode("ascii", errors="replace").rstrip(" \0")
        entries.append(PresetSummary(slot, flags, name))
    return PresetSummaryPage(start, tuple(entries))


def parse_asset_slot(payload: bytes) -> AssetSlot:
    if len(payload) != 30:
        raise ProtocolError("asset slot metadata must be 30 bytes")
    slot, present, active = payload[:3]
    if present not in (0, 1) or active not in (0, 1):
        raise ProtocolError("invalid asset slot flags")
    name = payload[14:30].decode("ascii", errors="replace").rstrip(" \0")
    return AssetSlot(
        slot=slot,
        present=bool(present),
        active=bool(active),
        size=int.from_bytes(payload[3:7], "little"),
        checksum=int.from_bytes(payload[7:11], "little"),
        format_id=int.from_bytes(payload[11:13], "little"),
        name=name,
        raw=payload,
    )


def parse_preset_capabilities(payload: bytes) -> PresetCapabilities:
    if len(payload) != 10 or payload[:4] != b"RSP1":
        raise ProtocolError("invalid preset capabilities response")
    version, record_size = struct.unpack_from("<HH", payload, 4)
    if version != 2 or record_size != 128:
        raise ProtocolError("unsupported preset record format")
    return PresetCapabilities(version, record_size, payload[8], payload[9])


def parse_preset_record(record: bytes) -> PresetRecord:
    if len(record) != 128 or record[:4] != b"RSP1":
        raise ProtocolError("invalid RSP1 preset record")
    version, size = struct.unpack_from("<HH", record, 4)
    if version != 2 or size != len(record):
        raise ProtocolError("unsupported RSP1 preset record")
    expected_crc = int.from_bytes(record[8:12], "little")
    crc_input = bytearray(record)
    crc_input[8:12] = b"\0\0\0\0"
    actual_crc = zlib.crc32(crc_input) & 0xFFFFFFFF
    if actual_crc != expected_crc:
        raise ProtocolError(f"RSP1 CRC mismatch: expected {expected_crc:08x}, got {actual_crc:08x}")
    flags = int.from_bytes(record[12:16], "little")
    return PresetRecord(version, flags, bool(flags & 1), tuple(record[32:37]), record)


@dataclass(frozen=True)
class GlobalSettings:
    """The pedal-wide settings (not part of any preset), as answered to command 0x65."""

    version: int
    wireless_enabled: bool
    loopback_enabled: bool
    input_gain_db: int
    usb_volume: int
    bt_volume: int
    midi_channel: int
    # Only in version 2 and later of the layout.
    volume_floor: int | None = None
    volume_ceiling: int | None = None


def parse_global_settings(payload: bytes) -> GlobalSettings:
    """Decode the answer to command 0x65 or 0x66.

    Layout, read from the official app and checked on a pedal: version, Bluetooth on, loopback on,
    input gain in dB (signed), USB volume, Bluetooth volume, MIDI channel (0 = omni). Version 2 adds
    two volume limits. Volumes above 100 and channels above 16 are limited, as the app does.
    """

    if len(payload) < 7:
        raise ProtocolError("global settings reply is too short")
    version = payload[0]
    floor = ceiling = None
    if version >= 2:
        if len(payload) < 9:
            raise ProtocolError("global settings reply is missing its volume limits")
        floor, ceiling = min(payload[7], 100), min(payload[8], 100)
        if floor >= ceiling:
            raise ProtocolError("global settings volume limits are not in order")
    return GlobalSettings(
        version=version,
        wireless_enabled=payload[1] != 0,
        loopback_enabled=payload[2] != 0,
        input_gain_db=struct.unpack("b", payload[3:4])[0],
        usb_volume=min(payload[4], MAX_SETTING_VOLUME),
        bt_volume=min(payload[5], MAX_SETTING_VOLUME),
        midi_channel=min(payload[6], MAX_MIDI_CHANNEL),
        volume_floor=floor,
        volume_ceiling=ceiling,
    )


def _flag(value: object, name: str) -> int:
    if type(value) is not bool:
        raise ValidationError(f"{name} must be true or false")
    return int(value)


def global_settings_payload(
    *,
    wireless_enabled: bool | None = None,
    loopback_enabled: bool | None = None,
    input_gain_db: int | None = None,
    usb_volume: int | None = None,
    bt_volume: int | None = None,
    midi_channel: int | None = None,
) -> bytes:
    """The payload of command 0x66: a one-byte field mask followed by the value of each chosen field.

    The bits are, in this order: Bluetooth (1), loopback (2), input gain (4), USB volume (8),
    Bluetooth volume (0x10), MIDI channel (0x20). Only the fields that are given are sent.
    A two-byte mask (used for another product) makes this pedal read its second byte as a value.
    """

    mask = 0
    values = bytearray()
    if wireless_enabled is not None:
        mask |= 0x01
        values.append(_flag(wireless_enabled, "Bluetooth"))
    if loopback_enabled is not None:
        mask |= 0x02
        values.append(_flag(loopback_enabled, "loopback"))
    if input_gain_db is not None:
        mask |= 0x04
        gain = _integer(input_gain_db, "input gain", MAX_INPUT_GAIN_DB, -MAX_INPUT_GAIN_DB)
        values.extend(struct.pack("b", gain))
    if usb_volume is not None:
        mask |= 0x08
        values.append(_integer(usb_volume, "USB volume", MAX_SETTING_VOLUME))
    if bt_volume is not None:
        mask |= 0x10
        values.append(_integer(bt_volume, "Bluetooth volume", MAX_SETTING_VOLUME))
    if midi_channel is not None:
        mask |= 0x20
        values.append(_integer(midi_channel, "MIDI channel", MAX_MIDI_CHANNEL))
    if mask == 0:
        raise ValidationError("no setting was given")
    return bytes([mask]) + bytes(values)


def preset_name_payload(name: str) -> bytes:
    """The live field that renames the active preset: ``08 length characters`` (command 0x6d).

    The pedal shows names of up to eight printable ASCII characters and pads them with spaces, so
    trailing spaces are dropped. The change lives in RAM until the preset is saved.
    """

    if not isinstance(name, str):
        raise ValidationError("preset name must be text")
    trimmed = name.rstrip(" ")
    if not trimmed or len(trimmed) > MAX_PRESET_NAME_LENGTH or any(not 0x20 <= ord(c) <= 0x7E for c in trimmed):
        raise ValidationError(
            f"preset name must be 1 to {MAX_PRESET_NAME_LENGTH} printable ASCII characters"
        )
    return bytes((PRESET_NAME_FIELD, len(trimmed))) + trimmed.encode("ascii")
