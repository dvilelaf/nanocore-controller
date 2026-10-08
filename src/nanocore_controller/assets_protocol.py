"""Pure payload builders and reply parsers for the amplifier (AMP) and impulse response (IR) slots.

Nothing here talks to a device: it only builds request payloads, parses replies and checks data.
The commands were read from the official app's native library; ``docs/amp-ir-protocol.md``
says which parts were checked against data and which come from the decompiled code alone.

Summary (the same shape for both kinds, see :data:`AMP` and :data:`IR`)::

    info      empty payload          -> storage info (slot count, size limit, active slot)
    slot info [slot]                 -> 30 byte slot record (nanocore_protocol.parse_asset_slot)
    read      [slot, offset u16, n]  -> [slot, offset u16, total u16, data]   (n <= 200)
    begin     [length u24 | slot<<24 as u32]
    chunk     [offset u24 | slot<<24 as u32] + data
    commit    [length u24 | slot<<24 as u32][crc32 u32]
    abort     empty payload
    name      [slot] + ASCII name

All integers are little endian. The CRC is the ordinary zlib CRC-32 of the slot data.
"""

import math
import struct
import zlib
from collections.abc import Iterator
from dataclasses import dataclass

from .errors import ProtocolError, ValidationError
from .nanocore_protocol import MAX_ASSET_SLOT, AssetSlot, _integer, parse_asset_slot

# --- limits and constants -------------------------------------------------------------------

MAX_SLOT_NUMBER = 37  # the amplifier storage has 38 slots (0 to 37), the IR storage 30 (0 to 29); the pedal says
ASSET_SLOT_COUNT = MAX_ASSET_SLOT + 1  # slots 0 to 29 hold the amplifiers and IRs a preset can select
MAX_TRANSFER_LENGTH = 0xFFFFFF  # begin, chunk offsets and commit carry 24 bits
MAX_READ_REQUEST = 200  # the app never asks for more than 200 bytes at a time
MAX_READABLE_SIZE = 0xFFFF  # read offsets and totals are 16 bits
MAX_NAME_LENGTH = 15  # the app truncates names to 15 characters (the slot record has 16 bytes)
IR_MAX_BYTES = 4096  # 1024 float32 samples; also the default limit when the pedal does not say
IR_SAMPLE_SIZE = 4

SAPF_MAGIC = b"SAPF"
SAPF_VERSION = 1
SAPF_HEADER_SIZE = 28  # magic, version, 16 header bytes, payload size
SAPF_TRAILER_SIZE = 32  # authentication code at the end
SAPF_OVERHEAD = SAPF_HEADER_SIZE + SAPF_TRAILER_SIZE  # 60, the "+ 0x3c" of the app
EADL_MAGIC = b"EADL"
STORED_AMP_MAGIC = b"DDPB"  # what the pedal stores (and returns) for an amplifier, in the clear

# Default chunk sizes of the app, in bytes of data per request.
IR_WRITE_CHUNK = 64  # command 0x52
AMP_WRITE_CHUNK = 128  # command 0x32 (env NANOCORE_AMP_WRITE_CHUNK); shrinks to 16 on errors
STREAM_CHUNK = 48  # commands 0x3a and 0x5b, which are not acknowledged one by one
STREAM_WINDOW_CHUNKS = 3  # 3 chunks (144 bytes) may be in flight
STREAM_DELAY_SECONDS = 0.009  # pause after each streamed chunk

DEVICE_STATUS_MESSAGES = {
    0: "ok",
    1: "bad payload",
    2: "no active write session",
    3: "slot out of range",
    4: "flash read/write failed",
    5: "CRC mismatch",
    6: "device not ready",
    0x7E: "command not implemented",
}


def status_message(status: int) -> str:
    """The app's wording for a device status code."""

    return DEVICE_STATUS_MESSAGES.get(status, "device reported error")


@dataclass(frozen=True)
class AssetKind:
    """The command numbers of one kind of slot. ``None`` means the app never sends it."""

    name: str
    info: int
    begin: int
    chunk: int
    commit: int
    abort: int
    set_name: int
    slot_info: int
    cursor: int | None
    read: int
    stream_chunk: int | None
    apply: int | None  # IR only: sent after the name, purpose unverified
    max_bytes_default: int | None
    write_chunk: int


AMP = AssetKind(
    name="amp",
    info=0x30,
    begin=0x31,
    chunk=0x32,
    commit=0x33,
    abort=0x34,
    set_name=0x35,
    slot_info=0x36,
    cursor=0x38,
    read=0x39,
    stream_chunk=0x3A,
    apply=None,
    max_bytes_default=None,
    write_chunk=AMP_WRITE_CHUNK,
)
IR = AssetKind(
    name="ir",
    info=0x50,
    begin=0x51,
    chunk=0x52,
    commit=0x53,
    abort=0x54,
    set_name=0x55,
    slot_info=0x56,
    cursor=0x5A,
    read=0x59,
    stream_chunk=0x5B,
    apply=0x57,
    max_bytes_default=IR_MAX_BYTES,
    write_chunk=IR_WRITE_CHUNK,
)


@dataclass(frozen=True)
class Timeouts:
    """Reply timeouts, in seconds, that the app uses for each step on this pedal."""

    info: float
    begin: float
    chunk: float
    commit: float
    abort: float
    set_name: float
    read: float
    cursor: float


AMP_TIMEOUTS = Timeouts(info=2.0, begin=10.0, chunk=1.5, commit=12.0, abort=0.3, set_name=2.0, read=1.0, cursor=1.2)
IR_TIMEOUTS = Timeouts(info=2.0, begin=2.0, chunk=0.5, commit=2.0, abort=0.3, set_name=2.0, read=1.0, cursor=1.2)
IR_APPLY_TIMEOUT = 0.8
SLOT_INFO_TIMEOUT = 1.2


def _slot(slot: object) -> int:
    return _integer(slot, "slot", MAX_SLOT_NUMBER)


def _length(length: object, name: str = "length") -> int:
    return _integer(length, name, MAX_TRANSFER_LENGTH, 1)


def _word(slot: int, value: int) -> bytes:
    """The 32 bit word ``value (24 bits) | slot << 24`` that begin, chunk and commit start with."""

    return struct.pack("<I", value | (slot << 24))


def _bytes(data: object, name: str) -> bytes:
    if not isinstance(data, (bytes, bytearray)):
        raise ValidationError(f"{name} must be bytes")
    return bytes(data)


def crc32(data: bytes) -> int:
    """The checksum of a slot: the zlib CRC-32 (polynomial 0xEDB88320) of all of its bytes."""

    return zlib.crc32(_bytes(data, "data")) & 0xFFFFFFFF


# --- storage info (0x30 amp, 0x50 IR) -------------------------------------------------------


@dataclass(frozen=True)
class StorageInfo:
    """Answer of the info command: the slot count, the largest accepted size, the active slot."""

    slot_count: int
    max_bytes: int
    active_slot: int | None
    header: bytes  # the first 8 bytes, meaning unknown
    raw: bytes


def parse_storage_info(payload: bytes) -> StorageInfo:
    """Decode the answer of ``0x30`` / ``0x50`` (at least 20 bytes; the app reads three fields).

    Bytes 8 to 11 are the slot count, 12 to 15 the size limit and 16 and 17 the active slot
    (``0xffff`` for none). The app rejects the reply when the first two are zero.
    """

    payload = _bytes(payload, "payload")
    if len(payload) < 20:
        raise ProtocolError("asset storage info must be at least 20 bytes")
    count, limit = struct.unpack_from("<II", payload, 8)
    active = int.from_bytes(payload[16:18], "little")
    if count == 0 or limit == 0:
        raise ProtocolError("asset storage info reports no slots or no capacity")
    return StorageInfo(count, limit, None if active == 0xFFFF else active, payload[:8], payload)


# --- slot info (0x36 amp, 0x56 IR) -----------------------------------------------------------


def slot_info_payload(slot: int) -> bytes:
    """Payload of the slot info request: just the slot (``0x36`` amp, ``0x56`` IR)."""

    return bytes((_slot(slot),))


def parse_slot_info(payload: bytes, slot: int) -> AssetSlot:
    """Parse a slot info reply and check that it answers for ``slot``."""

    info = parse_asset_slot(_bytes(payload, "payload"))
    if info.slot != _slot(slot):
        raise ProtocolError(f"slot info answers for slot {info.slot}, not {slot}")
    return info


# --- reading ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadChunk:
    slot: int
    offset: int
    total: int
    data: bytes


def read_payload(slot: int, offset: int, length: int) -> bytes:
    """Payload of ``0x39`` (amp) / ``0x59`` (IR): ``slot, offset u16, length u8`` (1 to 200)."""

    return struct.pack(
        "<BHB",
        _slot(slot),
        _integer(offset, "offset", MAX_READABLE_SIZE),
        _integer(length, "length", MAX_READ_REQUEST, 1),
    )


def parse_read_chunk(payload: bytes, slot: int, offset: int, total: int) -> ReadChunk:
    """Decode a read reply ``slot, offset u16, total u16, data`` and check it as the app does.

    The reply must repeat the slot and the requested offset, carry the same total as the slot info,
    and must not run past that total. An empty data part means the end.
    """

    payload = _bytes(payload, "payload")
    if len(payload) < 5:
        raise ProtocolError("truncated slot read reply")
    got_slot, got_offset, got_total = struct.unpack_from("<BHH", payload)
    data = payload[5:]
    if got_slot != slot or got_offset != offset or got_total != total:
        raise ProtocolError("slot read reply does not match the request")
    if offset + len(data) > total:
        raise ProtocolError("slot read reply runs past the slot size")
    return ReadChunk(got_slot, got_offset, got_total, data)


def read_windows(size: int, window: int = MAX_READ_REQUEST) -> Iterator[tuple[int, int]]:
    """Yield ``(offset, length)`` read requests that cover ``size`` bytes."""

    _integer(size, "size", MAX_READABLE_SIZE, 1)
    _integer(window, "window", MAX_READ_REQUEST, 1)
    for offset in range(0, size, window):
        yield offset, min(window, size - offset)


def verify_slot_data(info: AssetSlot, data: bytes) -> bytes:
    """Check data read from a slot against its slot info: length and CRC (``READ CRC mismatch``)."""

    data = _bytes(data, "data")
    if not info.present:
        raise ProtocolError("the slot is empty")
    if len(data) != info.size:
        raise ProtocolError(f"read {len(data)} bytes but the slot holds {info.size}")
    actual = crc32(data)
    if actual != info.checksum:
        raise ProtocolError(f"READ CRC mismatch: slot says {info.checksum:08x}, data is {actual:08x}")
    return data


# --- writing ---------------------------------------------------------------------------------


def begin_payload(slot: int, length: int) -> bytes:
    """Payload of ``0x31`` / ``0x51``: opens a write session for ``length`` bytes in ``slot``."""

    return _word(_slot(slot), _length(length))


def chunk_payload(slot: int, offset: int, data: bytes) -> bytes:
    """Payload of ``0x32`` / ``0x52`` (and the unacknowledged ``0x3a`` / ``0x5b``)."""

    data = _bytes(data, "chunk")
    if not data:
        raise ValidationError("a chunk needs at least one byte")
    _integer(offset, "offset", MAX_TRANSFER_LENGTH)
    if offset + len(data) > MAX_TRANSFER_LENGTH:
        raise ValidationError("chunk runs past the largest transfer")
    return _word(_slot(slot), offset) + data


def commit_payload(slot: int, length: int, checksum: int) -> bytes:
    """Payload of ``0x33`` / ``0x53``: total length and CRC-32 of everything that was sent."""

    return _word(_slot(slot), _length(length)) + struct.pack(
        "<I", _integer(checksum, "checksum", 0xFFFFFFFF)
    )


def encode_asset_name(name: str) -> bytes:
    """Encode a slot name like the app: printable ASCII, trailing spaces dropped, 15 characters."""

    if not isinstance(name, str):
        raise ValidationError("slot name must be text")
    trimmed = name.rstrip(" ")
    if not trimmed or len(trimmed) > MAX_NAME_LENGTH or any(not 0x20 <= ord(c) <= 0x7E for c in trimmed):
        raise ValidationError(f"slot name must be 1 to {MAX_NAME_LENGTH} printable ASCII characters")
    return trimmed.encode("ascii")


def name_payload(slot: int, name: str) -> bytes:
    """Payload of ``0x35`` / ``0x55``: the slot, then the name."""

    return bytes((_slot(slot),)) + encode_asset_name(name)


def apply_payload(slot: int) -> bytes:
    """Payload of the IR ``0x57`` that the app sends after naming (effect unverified)."""

    return bytes((_slot(slot),))


def parse_cursor(payload: bytes) -> int:
    """Answer of ``0x38`` / ``0x5a``: the number of bytes the pedal has received (u32, 4+ bytes)."""

    payload = _bytes(payload, "payload")
    if len(payload) < 4:
        raise ProtocolError("truncated write cursor reply")
    return int.from_bytes(payload[:4], "little")


def split_chunks(data: bytes, size: int) -> Iterator[tuple[int, bytes]]:
    """Yield ``(offset, piece)`` pieces of at most ``size`` bytes."""

    data = _bytes(data, "data")
    _integer(size, "chunk size", MAX_TRANSFER_LENGTH, 1)
    for offset in range(0, len(data), size):
        yield offset, data[offset : offset + size]


@dataclass(frozen=True)
class WriteStep:
    """One request of a write sequence."""

    command: int
    payload: bytes
    timeout: float
    purpose: str


def write_sequence(
    kind: AssetKind,
    slot: int,
    data: bytes,
    *,
    name: str | None = None,
    chunk_size: int | None = None,
    apply: bool | None = None,
) -> list[WriteStep]:
    """The acknowledged write sequence the app falls back to: begin, chunks, commit, name.

    For IR the app also sends ``0x57`` after the name (``apply``, on by default); its purpose is
    not verified. The caller still has to check each status, retry only what the app retries,
    send the abort command (``kind.abort``, empty payload) after a failed session, and read the
    slot back to compare. Streaming with the unacknowledged commands is not planned here.
    """

    data = _bytes(data, "data")
    _slot(slot)
    _length(len(data), "data length")
    if kind.max_bytes_default is not None and len(data) > kind.max_bytes_default:
        raise ValidationError(f"{kind.name} data is larger than {kind.max_bytes_default} bytes")
    timeouts = AMP_TIMEOUTS if kind is AMP else IR_TIMEOUTS
    size = kind.write_chunk if chunk_size is None else chunk_size
    steps = [WriteStep(kind.begin, begin_payload(slot, len(data)), timeouts.begin, "begin")]
    for offset, piece in split_chunks(data, size):
        steps.append(WriteStep(kind.chunk, chunk_payload(slot, offset, piece), timeouts.chunk, "chunk"))
    steps.append(WriteStep(kind.commit, commit_payload(slot, len(data), crc32(data)), timeouts.commit, "commit"))
    if name is not None:
        steps.append(WriteStep(kind.set_name, name_payload(slot, name), timeouts.set_name, "name"))
    if apply is None:
        apply = kind.apply is not None
    if apply and kind.apply is not None:
        steps.append(WriteStep(kind.apply, apply_payload(slot), IR_APPLY_TIMEOUT, "apply"))
    return steps


# --- IR file content -------------------------------------------------------------------------


def validate_ir(data: bytes, max_bytes: int = IR_MAX_BYTES) -> int:
    """Check IR data as the app does and return the sample count.

    The data is little endian float32 samples: a multiple of four bytes, at most ``max_bytes``
    (4096 on this pedal, reported by ``0x50``) and every value finite.
    """

    data = _bytes(data, "IR data")
    _integer(max_bytes, "max bytes", MAX_TRANSFER_LENGTH, 4)
    if not data or len(data) % IR_SAMPLE_SIZE:
        raise ValidationError("IR data must be a whole number of float32 samples")
    if len(data) > max_bytes:
        raise ValidationError(f"IR data is larger than {max_bytes} bytes ({max_bytes // 4} samples)")
    count = len(data) // IR_SAMPLE_SIZE
    if not all(math.isfinite(value) for value in struct.unpack(f"<{count}f", data)):
        raise ValidationError("IR data contains a value that is not finite")
    return count


def ir_from_samples(samples: list[float] | tuple[float, ...], max_bytes: int = IR_MAX_BYTES) -> bytes:
    """Pack float samples as IR data (no resampling, trimming or normalising is done here)."""

    if not isinstance(samples, (list, tuple)) or not samples:
        raise ValidationError("IR samples must be a non-empty list")
    if any(type(value) not in (int, float) for value in samples):
        raise ValidationError("IR samples must be numbers")
    try:
        data = struct.pack(f"<{len(samples)}f", *samples)
    except (OverflowError, struct.error) as exc:
        raise ValidationError("IR sample out of float32 range") from exc
    validate_ir(data, max_bytes)
    return data


def ir_to_samples(data: bytes) -> tuple[float, ...]:
    """Unpack IR data into float samples."""

    count = validate_ir(data, MAX_TRANSFER_LENGTH)
    return struct.unpack(f"<{count}f", data)


# --- amplifier container ---------------------------------------------------------------------


@dataclass(frozen=True)
class SapfContainer:
    """The structure of an amplifier blob (``SAPF``, version 1). The authentication code is not checked.

    Layout: ``"SAPF"``, version u32, 16 bytes (a nonce in the files of the app), payload size u32,
    payload, 32 byte authentication code. The pedal stores and returns a blob of this shape whose
    total length is the ``size`` of its slot info (12242 for the factory amps).
    """

    version: int
    header_bytes: bytes
    payload_size: int
    total_size: int
    trailer: bytes


def unwrap_eadl(data: bytes) -> bytes:
    """Return the ``SAPF`` blob of a file, unwrapping the optional ``EADL`` outer container.

    ``EADL``: magic, version u16 (1), header length u32 at offset 6, then that many header bytes
    and the blob. Without the magic the data is returned unchanged. Read from the app's code;
    no sample file was available.
    """

    data = _bytes(data, "data")
    if len(data) < 4 or data[:4] != EADL_MAGIC:
        return data
    if len(data) < 10:
        raise ProtocolError("truncated EADL container")
    if struct.unpack_from("<H", data, 4)[0] != 1:
        raise ProtocolError("unsupported EADL container version")
    start = struct.unpack_from("<I", data, 6)[0] + 10
    if len(data) < start + 4 or data[start : start + 4] != SAPF_MAGIC:
        raise ProtocolError("EADL container does not hold a SAPF blob")
    return data[start:]


def parse_sapf(data: bytes) -> SapfContainer:
    """Check the framing of an amplifier blob: magic, version 1 and total = payload + 60."""

    data = _bytes(data, "data")
    if len(data) < SAPF_OVERHEAD or data[:4] != SAPF_MAGIC:
        raise ValidationError("not a SAPF amplifier blob")
    version = struct.unpack_from("<I", data, 4)[0]
    if version != SAPF_VERSION:
        raise ValidationError(f"unsupported SAPF version {version}")
    payload_size = struct.unpack_from("<I", data, 24)[0]
    if len(data) != payload_size + SAPF_OVERHEAD:
        raise ValidationError("SAPF payload size does not match the length of the data")
    return SapfContainer(version, data[8:24], payload_size, len(data), data[-SAPF_TRAILER_SIZE:])


def validate_amp(data: bytes, max_bytes: int | None = None) -> SapfContainer:
    """Check the framing of an amplifier blob and, when given, the size limit reported by ``0x30``."""

    container = parse_sapf(data)
    if container.total_size > MAX_READABLE_SIZE:
        raise ValidationError("amplifier blob is larger than the 16 bit read limit")
    # The app compares the limit of 0x30 with the payload size field, not with the total length.
    if max_bytes is not None and container.payload_size > max_bytes:
        raise ValidationError(f"amplifier payload is larger than {max_bytes} bytes")
    return container


def validate_stored_amp(data: bytes, max_bytes: int | None = None) -> int:
    """Check an amplifier blob as the pedal stores it (what ``read_asset`` returns); returns its length.

    Only the framing is known: it starts with ``DDPB`` and fits the storage. Its content is not
    checked, so write only blobs that were read from a pedal.
    """

    data = _bytes(data, "data")
    if not data.startswith(STORED_AMP_MAGIC):
        raise ValidationError("an amplifier blob read from the pedal starts with DDPB")
    if max_bytes is not None and len(data) > max_bytes:
        raise ValidationError(f"amplifier data is larger than {max_bytes} bytes")
    return len(data)
