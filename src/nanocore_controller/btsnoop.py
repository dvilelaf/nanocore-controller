"""Read the NANOCORE traffic out of an Android Bluetooth HCI snoop log.

The official app talks to the pedal with the same SysEx frames this package uses, carried in BLE-MIDI
over ATT. Android can record that traffic (``btsnoop_hci.log``); this module turns such a file into
the list of requests and responses so that commands we do not know yet can be read off a capture.

Only reading is done here; nothing is sent to a pedal.
"""

import logging
import struct
from collections.abc import Iterable
from dataclasses import dataclass, field

from .ble_midi import BleMidiDecoder
from .errors import NanocoreError
from .nanocore_protocol import (
    DEVICE_TO_HOST,
    HOST_TO_DEVICE,
    SYSEX_PREFIX,
    NanocoreResponse,
    ResponseAssembler,
    decode_response,
    unpack_7bit,
)

log = logging.getLogger("nanocore.btsnoop")

BTSNOOP_MAGIC = b"btsnoop\0"
HCI_UART = 1002  # the only link type Android writes
BTSNOOP_EPOCH_OFFSET = 0x00DCDDB30F2F8000  # microseconds from year 0 to 1970
H4_ACL = 2
ATT_CID = 4
ATT_WRITE_REQUEST, ATT_WRITE_COMMAND, ATT_PREPARE_WRITE, ATT_EXECUTE_WRITE = 0x12, 0x52, 0x16, 0x18
ATT_NOTIFICATION, ATT_INDICATION = 0x1B, 0x1D
APP_TO_PEDAL, PEDAL_TO_APP = "app->pedal", "pedal->app"


@dataclass(frozen=True)
class CapturedFrame:
    time: float  # seconds since the first record of the file
    direction: str
    sequence: int
    command: int
    status: int | None  # responses only: 0 is success, 0x10/0x11 are chunks of a longer answer
    payload: bytes
    assembled: bool = False  # a long answer put back together from its chunks


@dataclass
class _Link:
    """Bytes of an L2CAP packet still arriving in later ACL fragments."""

    data: bytearray = field(default_factory=bytearray)
    expected: int = 0


def _records(data: bytes) -> Iterable[tuple[bool, bytes, float]]:
    if not data.startswith(BTSNOOP_MAGIC) or len(data) < 16:
        raise ValueError("not a btsnoop log (no 'btsnoop' header)")
    version, datalink = struct.unpack_from(">II", data, 8)
    if datalink != HCI_UART:
        raise ValueError(f"unsupported btsnoop link type {datalink}; expected {HCI_UART}")
    offset = 16
    while offset + 24 <= len(data):
        _, included, flags, _, stamp = struct.unpack_from(">IIIIq", data, offset)
        offset += 24
        packet = data[offset : offset + included]
        offset += included
        yield bool(flags & 1), bytes(packet), (stamp - BTSNOOP_EPOCH_OFFSET) / 1_000_000


def _att_pdus(data: bytes) -> Iterable[tuple[bool, bytes, float]]:
    """Reassemble ATT packets from ACL fragments, one logical connection and direction at a time."""

    links: dict[tuple[int, bool], _Link] = {}
    for received, packet, seconds in _records(data):
        if len(packet) < 5 or packet[0] != H4_ACL:
            continue
        flags, length = struct.unpack_from("<HH", packet, 1)
        handle, boundary = flags & 0x0FFF, (flags >> 12) & 0b11
        fragment = packet[5 : 5 + length]
        key = (handle, received)
        if boundary == 0b01:  # continuation
            link = links.get(key)
            if link is None:
                continue
            link.data += fragment
        else:
            if len(fragment) < 4:
                continue
            link = _Link(bytearray(fragment), struct.unpack_from("<H", fragment)[0] + 4)
            links[key] = link
        if len(link.data) < link.expected:
            continue
        done, link.data = bytes(link.data[: link.expected]), bytearray()
        del links[key]
        cid = struct.unpack_from("<H", done, 2)[0]
        if cid == ATT_CID:
            yield received, done[4:], seconds


def _midi_values(data: bytes) -> Iterable[tuple[bool, bytes, float]]:
    """The values written or notified over ATT, with long writes put back together."""

    prepared: dict[int, bytearray] = {}
    for received, pdu, seconds in _att_pdus(data):
        if not pdu:
            continue
        opcode = pdu[0]
        if opcode in (ATT_WRITE_REQUEST, ATT_WRITE_COMMAND, ATT_NOTIFICATION, ATT_INDICATION) and len(pdu) > 3:
            yield received, pdu[3:], seconds
        elif opcode == ATT_PREPARE_WRITE and len(pdu) > 5:
            handle, offset = struct.unpack_from("<HH", pdu, 1)
            buffer = prepared.setdefault(handle, bytearray())
            buffer[offset : offset + len(pdu) - 5] = pdu[5:]
        elif opcode == ATT_EXECUTE_WRITE and len(pdu) > 1 and pdu[1] == 1:
            for buffer in prepared.values():
                yield received, bytes(buffer), seconds
            prepared.clear()


def _decode_request(message: bytes) -> tuple[int, int, bytes]:
    prefix = SYSEX_PREFIX + bytes([HOST_TO_DEVICE])
    raw = unpack_7bit(message[len(prefix) : -1])
    if len(raw) < 7 or raw[0] != 2:
        raise NanocoreError("invalid NANOCORE request header")
    sequence, command, length = struct.unpack_from("<HHH", raw, 1)
    if len(raw) != 7 + length:
        raise NanocoreError("NANOCORE request payload length mismatch")
    return sequence, command, raw[7:]


def read_frames(data: bytes) -> list[CapturedFrame]:
    """Every NANOCORE request and response found in a btsnoop log, in order."""

    frames: list[CapturedFrame] = []
    decoders = {False: BleMidiDecoder(), True: BleMidiDecoder()}
    assembler = ResponseAssembler()
    first_time: float | None = None
    for received, value, seconds in _midi_values(data):
        if first_time is None:
            first_time = seconds
        try:
            events = decoders[received].feed(value)
        except ValueError as exc:
            log.debug("skipping a packet that is not BLE-MIDI: %s", exc)
            continue
        for event in events:
            message = event.message
            if message[:4] != SYSEX_PREFIX or len(message) < 6:
                continue
            at = seconds - first_time
            try:
                if message[4] == HOST_TO_DEVICE and not received:
                    sequence, command, payload = _decode_request(message)
                    frames.append(CapturedFrame(at, APP_TO_PEDAL, sequence, command, None, payload))
                elif message[4] == DEVICE_TO_HOST and received:
                    response = decode_response(message)
                    frames.append(_response_frame(at, response, assembled=False))
                    whole = assembler.feed(response)
                    if whole is not None and response.status in (0x10, 0x11):
                        frames.append(_response_frame(at, whole, assembled=True))
            except NanocoreError as exc:
                log.debug("skipping a damaged frame: %s", exc)
    # Time zero is the first NANOCORE frame, not the first packet of the log.
    if frames:
        base = frames[0].time
        frames = [CapturedFrame(f.time - base, f.direction, f.sequence, f.command, f.status, f.payload, f.assembled) for f in frames]
    return frames


def _response_frame(at: float, response: NanocoreResponse, *, assembled: bool) -> CapturedFrame:
    return CapturedFrame(at, PEDAL_TO_APP, response.sequence, response.command, response.status, response.payload, assembled)


def read_frames_from_file(path: str) -> list[CapturedFrame]:
    with open(path, "rb") as handle:
        return read_frames(handle.read())


def format_frames(frames: Iterable[CapturedFrame], *, limit: int = 48) -> str:
    """One line per frame: time, direction, sequence, command, status and the payload in hex."""

    lines = []
    for frame in frames:
        payload = frame.payload.hex(" ")
        shown = payload if len(frame.payload) <= limit else payload[: limit * 3 - 1] + f" ... ({len(frame.payload)} bytes)"
        status = "" if frame.status is None else f" status=0x{frame.status:02x}"
        mark = " (assembled)" if frame.assembled else ""
        lines.append(
            f"{frame.time:9.3f}s  {frame.direction}  seq={frame.sequence:<5} cmd=0x{frame.command:02x}{status}{mark}  {shown}"
        )
    return "\n".join(lines)
