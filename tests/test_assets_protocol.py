import struct
import unittest
import zlib
from pathlib import Path

from nanocore_controller import assets_protocol as ap
from nanocore_controller.errors import ProtocolError, ValidationError
from nanocore_controller.nanocore_protocol import parse_asset_slot

# Factory files extracted from the official app (see docs/amp-ir-protocol.md). Tests that need them
# are skipped when the directory is absent.
FACTORY = Path(
    "/tmp/claude-1000/-tmp/86b9d813-624b-4b84-9aaa-9fb763b9d0a6/scratchpad/fac/"
    "assets/flutter_assets/assets/nanocore/factory_presets"
)

# (slot, name, size, checksum) of factory IR slots, read from a real pedal with command 0x56.
REAL_IR_SLOTS = [
    (0, 4096, 3669048126),
    (2, 4096, 1785848145),
    (3, 4096, 495440862),
    (4, 4096, 427454397),
    (5, 4096, 3987728053),
    (7, 4096, 3207569225),
    (9, 4096, 1155150923),
    (10, 4096, 2229255039),
    (13, 4096, 3849827229),
    (14, 4096, 1688230527),
    (15, 3764, 11547137),
    (16, 4096, 801193537),
    (24, 4096, 3760194811),
    (25, 4096, 3223657454),
    (26, 4096, 2455333328),
    (27, 4096, 3708781408),
    (28, 4096, 3095321110),
]


def slot_record(slot: int, size: int, checksum: int, name: str, *, active: bool = False) -> bytes:
    raw = bytes((slot, 1, int(active))) + struct.pack("<II", size, checksum) + b"\x1a\x1a\x00"
    return raw + name.encode().ljust(16, b"\0")


def sapf(payload: bytes) -> bytes:
    header = b"SAPF" + struct.pack("<I", 1) + bytes(range(16)) + struct.pack("<I", len(payload))
    return header + payload + bytes(32)


class PayloadTests(unittest.TestCase):
    def test_command_numbers(self):
        self.assertEqual(
            (ap.AMP.info, ap.AMP.begin, ap.AMP.chunk, ap.AMP.commit, ap.AMP.abort, ap.AMP.set_name),
            (0x30, 0x31, 0x32, 0x33, 0x34, 0x35),
        )
        self.assertEqual((ap.AMP.slot_info, ap.AMP.cursor, ap.AMP.read, ap.AMP.stream_chunk), (0x36, 0x38, 0x39, 0x3A))
        self.assertEqual(
            (ap.IR.info, ap.IR.begin, ap.IR.chunk, ap.IR.commit, ap.IR.abort, ap.IR.set_name),
            (0x50, 0x51, 0x52, 0x53, 0x54, 0x55),
        )
        self.assertEqual((ap.IR.slot_info, ap.IR.apply, ap.IR.read, ap.IR.cursor, ap.IR.stream_chunk), (0x56, 0x57, 0x59, 0x5A, 0x5B))

    def test_begin_chunk_and_commit_pack_the_slot_in_the_top_byte(self):
        self.assertEqual(ap.begin_payload(12, 12242), bytes.fromhex("d22f000c"))
        self.assertEqual(ap.chunk_payload(2, 0x0140, b"\xaa\xbb"), bytes.fromhex("4001000" "2aabb"))
        self.assertEqual(ap.commit_payload(2, 4096, 1785848145), bytes.fromhex("00100002 51e1716a"))

    def test_rejects_bad_numbers(self):
        for bad in (-1, 38, True, 1.0, "1"):
            with self.assertRaises(ValidationError):
                ap.begin_payload(bad, 10)  # type: ignore[arg-type]
        for bad in (0, 0x1000000):
            with self.assertRaises(ValidationError):
                ap.begin_payload(0, bad)
        with self.assertRaises(ValidationError):
            ap.chunk_payload(0, 0, b"")
        with self.assertRaises(ValidationError):
            ap.commit_payload(0, 10, 1 << 32)

    def test_names(self):
        self.assertEqual(ap.name_payload(3, "Eng412A  "), b"\x03Eng412A")
        for bad in ("", "   ", "x" * 16, "café", "tab\t"):
            with self.assertRaises(ValidationError):
                ap.encode_asset_name(bad)
        self.assertEqual(len(ap.encode_asset_name("a" * 15)), 15)

    def test_read_payload_and_reply(self):
        self.assertEqual(ap.read_payload(12, 0x1234, 200), bytes.fromhex("0c34 12c8"))
        with self.assertRaises(ValidationError):
            ap.read_payload(12, 0, 201)
        reply = bytes.fromhex("0c 3412 d22f") + b"abc"
        chunk = ap.parse_read_chunk(reply, 12, 0x1234, 12242)
        self.assertEqual((chunk.offset, chunk.total, chunk.data), (0x1234, 12242, b"abc"))
        for slot, offset, total in ((11, 0x1234, 12242), (12, 0, 12242), (12, 0x1234, 4096)):
            with self.assertRaises(ProtocolError):
                ap.parse_read_chunk(reply, slot, offset, total)
        with self.assertRaises(ProtocolError):
            ap.parse_read_chunk(bytes.fromhex("0c 3412 d22f") + bytes(12242), 12, 0x1234, 12242)
        with self.assertRaises(ProtocolError):
            ap.parse_read_chunk(b"\x0c\x00", 12, 0, 10)

    def test_read_windows_cover_the_slot(self):
        windows = list(ap.read_windows(12242))
        self.assertEqual(windows[0], (0, 200))
        self.assertEqual(windows[-1], (12200, 42))
        self.assertEqual(sum(length for _, length in windows), 12242)

    def test_cursor_and_storage_info(self):
        self.assertEqual(ap.parse_cursor(bytes.fromhex("30 00 00 00")), 48)
        with self.assertRaises(ProtocolError):
            ap.parse_cursor(b"\0\0")
        raw = bytes(8) + struct.pack("<IIH", 30, 4096, 0xFFFF) + bytes(2)
        info = ap.parse_storage_info(raw)
        self.assertEqual((info.slot_count, info.max_bytes, info.active_slot), (30, 4096, None))
        raw = bytes(8) + struct.pack("<IIH", 30, 4096, 7) + bytes(2)
        self.assertEqual(ap.parse_storage_info(raw).active_slot, 7)
        for bad in (b"\0" * 19, bytes(8) + struct.pack("<IIH", 0, 4096, 0) + bytes(2)):
            with self.assertRaises(ProtocolError):
                ap.parse_storage_info(bad)

    def test_slot_info_checks_the_slot(self):
        record = slot_record(12, 12242, 1107111727, "MesR2")
        self.assertEqual(record[7:11], bytes.fromhex("2f2ffd41"))
        info = ap.parse_slot_info(record, 12)
        self.assertEqual((info.size, info.checksum, info.name), (12242, 0x41FD2F2F, "MesR2"))
        with self.assertRaises(ProtocolError):
            ap.parse_slot_info(record, 11)

    def test_verify_slot_data(self):
        data = bytes(range(256)) * 4
        info = parse_asset_slot(slot_record(2, len(data), zlib.crc32(data), "Eng412A"))
        self.assertEqual(ap.verify_slot_data(info, data), data)
        with self.assertRaises(ProtocolError):
            ap.verify_slot_data(info, data[:-1])
        with self.assertRaises(ProtocolError):
            ap.verify_slot_data(info, data[:-1] + b"\0")

    def test_write_sequence_for_ir(self):
        data = struct.pack("<256f", *[0.5] * 256)
        steps = ap.write_sequence(ap.IR, 5, data, name="Test")
        self.assertEqual([s.purpose for s in steps][:2], ["begin", "chunk"])
        self.assertEqual([s.purpose for s in steps][-3:], ["commit", "name", "apply"])
        chunks = [s for s in steps if s.purpose == "chunk"]
        self.assertEqual(len(chunks), 16)  # 1024 bytes in 64 byte pieces
        self.assertEqual(chunks[1].payload[:4], bytes.fromhex("40000005"))
        rebuilt = b"".join(s.payload[4:] for s in chunks)
        self.assertEqual(rebuilt, data)
        commit = next(s for s in steps if s.purpose == "commit")
        self.assertEqual(commit.payload[4:], struct.pack("<I", zlib.crc32(data)))
        self.assertEqual({s.command for s in steps}, {0x51, 0x52, 0x53, 0x55, 0x57})
        self.assertNotIn("apply", [s.purpose for s in ap.write_sequence(ap.IR, 5, data, apply=False)])
        with self.assertRaises(ValidationError):
            ap.write_sequence(ap.IR, 5, bytes(4100))

    def test_write_sequence_for_amp_uses_128_byte_chunks(self):
        blob = sapf(bytes(1000 - ap.SAPF_OVERHEAD))
        steps = ap.write_sequence(ap.AMP, 29, blob)
        self.assertEqual([s.command for s in steps], [0x31] + [0x32] * 8 + [0x33])
        self.assertEqual(len(steps[1].payload), 4 + 128)


class IrTests(unittest.TestCase):
    def test_validate_ir(self):
        self.assertEqual(ap.validate_ir(struct.pack("<4f", 0.0, 1.0, -2.0, 0.5)), 4)
        self.assertEqual(ap.validate_ir(bytes(4096)), 1024)
        for bad in (b"", bytes(3), bytes(4100), struct.pack("<f", float("nan")), struct.pack("<f", float("inf"))):
            with self.assertRaises(ValidationError):
                ap.validate_ir(bad)

    def test_samples_round_trip(self):
        data = ap.ir_from_samples([0.25, -0.5, 1.5])
        self.assertEqual(ap.ir_to_samples(data), (0.25, -0.5, 1.5))
        with self.assertRaises(ValidationError):
            ap.ir_from_samples([0.0] * 1025)
        with self.assertRaises(ValidationError):
            ap.ir_from_samples([float("nan")])
        with self.assertRaises(ValidationError):
            ap.ir_from_samples([1e300])

    @unittest.skipUnless(FACTORY.exists(), "factory files extracted from the app are not available")
    def test_factory_ir_files_match_the_checksums_of_a_real_pedal(self):
        for slot, size, checksum in REAL_IR_SLOTS:
            data = (FACTORY / "ir" / f"{slot:02d}.bin").read_bytes()
            self.assertEqual(len(data), size, slot)
            self.assertEqual(ap.crc32(data), checksum, slot)
            self.assertEqual(ap.validate_ir(data), size // 4)


class AmpContainerTests(unittest.TestCase):
    def test_parse_sapf(self):
        blob = sapf(bytes(100))
        container = ap.parse_sapf(blob)
        self.assertEqual((container.version, container.payload_size, container.total_size), (1, 100, 160))
        self.assertEqual(container.header_bytes, bytes(range(16)))
        self.assertEqual(ap.validate_amp(blob, max_bytes=100).payload_size, 100)
        with self.assertRaises(ValidationError):
            ap.validate_amp(blob, max_bytes=99)

    def test_rejects_malformed_blobs(self):
        good = sapf(bytes(100))
        for bad in (
            b"",
            good[:59],
            b"XAPF" + good[4:],
            good[:4] + struct.pack("<I", 2) + good[8:],
            good + b"\0",
            good[:-1],
        ):
            with self.assertRaises(ValidationError):
                ap.parse_sapf(bad)

    def test_unwrap_eadl(self):
        blob = sapf(bytes(10))
        wrapped = b"EADL" + struct.pack("<HI", 1, 3) + b"abc" + blob
        self.assertEqual(ap.unwrap_eadl(wrapped), blob)
        self.assertEqual(ap.unwrap_eadl(blob), blob)
        for bad in (b"EADL", b"EADL" + struct.pack("<HI", 2, 0) + blob, b"EADL" + struct.pack("<HI", 1, 99) + blob):
            with self.assertRaises(ProtocolError):
                ap.unwrap_eadl(bad)

    @unittest.skipUnless(FACTORY.exists(), "factory files extracted from the app are not available")
    def test_factory_files_have_the_documented_framing(self):
        for path in sorted((FACTORY / "amp").glob("*.ead")):
            container = ap.parse_sapf(path.read_bytes())
            self.assertEqual((container.total_size, container.payload_size), (12302, 12242), path.name)

    def test_status_messages(self):
        self.assertEqual(ap.status_message(6), "device not ready")
        self.assertEqual(ap.status_message(0x7E), "command not implemented")
        self.assertEqual(ap.status_message(99), "device reported error")


if __name__ == "__main__":
    unittest.main()
