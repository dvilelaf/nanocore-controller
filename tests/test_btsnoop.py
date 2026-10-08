import struct
import unittest

from nanocore_controller.ble_midi import encode_packet
from nanocore_controller.btsnoop import format_frames, read_frames
from nanocore_controller.nanocore_protocol import (
    DEVICE_TO_HOST,
    SYSEX_PREFIX,
    encode_request,
    pack_7bit,
)

ATT_HANDLE = 0x0013
CONNECTION = 0x0040


def response_message(sequence: int, command: int, status: int, payload: bytes) -> bytes:
    raw = struct.pack("<BHHBH", 2, sequence, command, status, len(payload)) + payload
    return SYSEX_PREFIX + bytes([DEVICE_TO_HOST]) + pack_7bit(raw) + bytes([0xF7])


def att(opcode: int, value: bytes, handle: int = ATT_HANDLE) -> bytes:
    return bytes([opcode]) + struct.pack("<H", handle) + value


def acl(pdu: bytes, *, cid: int = 4, first: bool = True, handle: int = CONNECTION) -> bytes:
    l2cap = struct.pack("<HH", len(pdu), cid) + pdu
    return acl_raw(l2cap, first=first, handle=handle)


def acl_raw(data: bytes, *, first: bool, handle: int = CONNECTION) -> bytes:
    flags = handle | ((0b10 if first else 0b01) << 12)
    return bytes([2]) + struct.pack("<HH", flags, len(data)) + data


def snoop(*records: tuple[bool, bytes, int]) -> bytes:
    """Each record: (received, h4 packet, microseconds)."""

    out = b"btsnoop\0" + struct.pack(">II", 1, 1002)
    for received, packet, micros in records:
        out += struct.pack(">IIIIq", len(packet), len(packet), 1 if received else 0, 0, 0x00DCDDB30F2F8000 + micros)
        out += packet
    return out


class ReadFramesTest(unittest.TestCase):
    def test_a_request_written_by_the_app(self):
        message = encode_request(0x63, b"", sequence=5)
        data = snoop((False, acl(att(0x52, encode_packet(message))), 1_000_000))
        frames = read_frames(data)
        self.assertEqual(len(frames), 1)
        frame = frames[0]
        self.assertEqual((frame.direction, frame.sequence, frame.command, frame.payload), ("app->pedal", 5, 0x63, b""))
        self.assertIsNone(frame.status)

    def test_write_requests_count_too_and_times_are_relative(self):
        a = encode_request(0x40, b"\x00\x06", sequence=1)
        b = encode_request(0x40, b"\x06\x06", sequence=2)
        data = snoop(
            (False, acl(att(0x12, encode_packet(a))), 5_000_000),
            (False, acl(att(0x52, encode_packet(b))), 7_500_000),
        )
        frames = read_frames(data)
        self.assertEqual([f.sequence for f in frames], [1, 2])
        self.assertEqual([round(f.time, 3) for f in frames], [0.0, 2.5])
        self.assertEqual(frames[0].payload, b"\x00\x06")

    def test_a_response_split_over_two_notifications(self):
        message = response_message(9, 0x63, 0, bytes(range(20)))
        first = bytes((0x80, 0x80)) + message[:12]
        second = bytes((0x80,)) + message[12:-1] + bytes((0x80, 0xF7))
        data = snoop(
            (True, acl(att(0x1B, first)), 1_000),
            (True, acl(att(0x1B, second)), 2_000),
        )
        frames = read_frames(data)
        self.assertEqual(len(frames), 1)
        self.assertEqual((frames[0].direction, frames[0].sequence, frames[0].command, frames[0].status), ("pedal->app", 9, 0x63, 0))
        self.assertEqual(frames[0].payload, bytes(range(20)))

    def test_an_att_packet_fragmented_at_the_radio_level(self):
        message = encode_request(0x6D, bytes([4, 77]), sequence=3)
        pdu = att(0x52, encode_packet(message))
        l2cap = struct.pack("<HH", len(pdu), 4) + pdu
        cut = 9
        data = snoop(
            (False, acl_raw(l2cap[:cut], first=True), 1_000),
            (False, acl_raw(l2cap[cut:], first=False), 1_100),
        )
        frames = read_frames(data)
        self.assertEqual([(f.command, f.payload) for f in frames], [(0x6D, bytes([4, 77]))])

    def test_unrelated_traffic_is_ignored(self):
        message = encode_request(0x63, b"", sequence=1)
        data = snoop(
            (False, bytes([1, 0x03, 0x0C, 0x00]), 100),  # an HCI command, not ACL
            (True, acl(att(0x1B, b"\x80\x80\x90\x40\x7f")), 200),  # a plain MIDI note, not SysEx
            (False, acl(b"\x01\x02\x03", cid=5), 300),  # another L2CAP channel
            (False, acl(att(0x0A, b"")[:3]), 400),  # an ATT read
            (False, acl(att(0x52, encode_packet(message))), 500),
        )
        frames = read_frames(data)
        self.assertEqual([f.command for f in frames], [0x63])

    def test_a_streamed_response_is_also_shown_assembled(self):
        body = bytes(range(10))
        chunk1 = struct.pack("<HH", 10, 0) + body[:6]
        chunk2 = struct.pack("<HH", 10, 6) + body[6:]
        first = encode_packet(response_message(4, 0x41, 0x10, chunk1))
        second = encode_packet(response_message(4, 0x41, 0x11, chunk2))
        data = snoop((True, acl(att(0x1B, first)), 10), (True, acl(att(0x1B, second)), 20))
        frames = read_frames(data)
        self.assertEqual([f.status for f in frames], [0x10, 0x11, 0])
        self.assertTrue(frames[-1].assembled)
        self.assertEqual(frames[-1].payload, body)

    def test_a_file_that_is_not_a_btsnoop_log_is_refused(self):
        with self.assertRaises(ValueError):
            read_frames(b"not a log at all")
        with self.assertRaises(ValueError):
            read_frames(b"btsnoop\0" + struct.pack(">II", 1, 1001))  # unsupported link type

    def test_a_damaged_record_is_skipped_not_fatal(self):
        good = encode_request(0x63, b"", sequence=2)
        data = snoop(
            (False, acl(att(0x52, b"\x80\x80\xF0\x7D")), 1),  # a SysEx that never ends and is not ours
            (False, acl(att(0x52, encode_packet(good))), 2),
        )
        frames = read_frames(data)
        self.assertIn(0x63, [f.command for f in frames])

    def test_format_lists_command_direction_and_payload(self):
        message = encode_request(0x6D, bytes([4, 90]), sequence=7)
        text = format_frames(read_frames(snoop((False, acl(att(0x52, encode_packet(message))), 1))))
        self.assertIn("app->pedal", text)
        self.assertIn("0x6d", text)
        self.assertIn("04 5a", text)


if __name__ == "__main__":
    unittest.main()
