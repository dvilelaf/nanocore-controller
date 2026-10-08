import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from nanocore_controller import assets_protocol as ap
from nanocore_controller.device import NanocoreDevice
from nanocore_controller.errors import DeviceStatusError, NanocoreError, ValidationError
from nanocore_controller.nanocore_protocol import NanocoreResponse
from support import load_fixture  # noqa: F401  (keeps the path set up)
from test_assets_io import AMP_SIZE, IR_SIZE, AssetPedal, blob
from test_cli import CliTestCase


def ir_blob(seed: int, samples: int = 1024) -> bytes:
    """A valid IR: finite float32 samples."""

    return struct.pack(f"<{samples}f", *[((seed * 31 + i * 7) % 200 - 100) / 100 for i in range(samples)])


class WritablePedal(AssetPedal):
    """An AssetPedal that also takes write sessions: begin, chunks, commit, name, apply, abort."""

    def __init__(self) -> None:
        super().__init__()
        self.session_slot: dict[str, tuple[int, int] | None] = {"amp": None, "ir": None}
        self.received: dict[str, bytearray] = {"amp": bytearray(), "ir": bytearray()}
        self.writes: list[tuple[int, bytes]] = []
        self.fail_chunk: int | None = None  # fail the n-th chunk (1-based) with this status
        self.fail_status = 4
        self.corrupt_commit = False
        self.live_selects: list[bytes] = []

    async def query(self, command, payload=b"", **kwargs):
        if command == 0x6D and payload[:1] in (b"\x06", b"\x07"):  # select the amplifier / IR slot
            self.live_selects.append(payload)
            self.active["amp" if payload[0] == 6 else "ir"] = payload[1]
            return NanocoreResponse(1, command, 0, b"")
        table = {0x31: "amp", 0x32: "amp", 0x33: "amp", 0x34: "amp", 0x35: "amp",
                 0x51: "ir", 0x52: "ir", 0x53: "ir", 0x54: "ir", 0x55: "ir", 0x57: "ir"}
        kind = table.get(command)
        if kind is None:
            return await super().query(command, payload, **kwargs)
        self.writes.append((command, payload))
        base = 0x31 if kind == "amp" else 0x51
        step = command - base
        if step == 0:  # begin: u32 (length | slot << 24)
            word = int.from_bytes(payload[:4], "little")
            self.session_slot[kind] = (word >> 24, word & 0xFFFFFF)
            self.received[kind] = bytearray()
        elif step == 1:  # chunk
            if self.session_slot[kind] is None:
                raise DeviceStatusError(command, 2)
            chunks = sum(1 for c, _ in self.writes if c == command)
            if self.fail_chunk == chunks:
                raise DeviceStatusError(command, self.fail_status)
            word = int.from_bytes(payload[:4], "little")
            if (word & 0xFFFFFF) != len(self.received[kind]):
                raise DeviceStatusError(command, 1)
            self.received[kind] += payload[4:]
        elif step == 2:  # commit: u32 (length | slot << 24), u32 crc
            if self.session_slot[kind] is None:
                raise DeviceStatusError(command, 2)
            slot, length = self.session_slot[kind]
            crc = int.from_bytes(payload[4:8], "little")
            data = bytes(self.received[kind])
            if self.corrupt_commit or len(data) != length or zlib.crc32(data) != crc:
                raise DeviceStatusError(command, 5)
            self.data[(kind, slot)] = data
            self.active[kind] = slot  # what a real pedal does after a write
            self.session_slot[kind] = None
        elif step == 3:  # abort
            self.session_slot[kind] = None
            self.received[kind] = bytearray()
        elif step == 4:  # name
            self.names[(kind, payload[0])] = payload[1:].decode()
        return NanocoreResponse(1, command, 0, b"")


class DeviceWriteTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pedal = WritablePedal()
        self.device = NanocoreDevice(self.pedal)
        await self.device.connect()
        self.addAsyncCleanup(self.device.close)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.safety = Path(self._tmp.name) / "old.bin"

    async def test_writing_an_ir_keeps_the_old_content_then_verifies_by_reading_back(self):
        old = self.pedal.data[("ir", 29)]
        new = ir_blob(0)
        info = await self.device.write_asset(ap.IR, 29, new, safety_backup=self.safety, name="MyIR")
        self.assertEqual(self.safety.read_bytes(), old)
        self.assertEqual(self.pedal.data[("ir", 29)], new)
        self.assertEqual((info.slot, info.name, info.checksum), (29, "MyIR", zlib.crc32(new)))

    async def test_the_sequence_is_begin_chunks_commit_name_apply(self):
        await self.device.write_asset(ap.IR, 29, ir_blob(1), safety_backup=self.safety, name="MyIR")
        commands = [c for c, _ in self.pedal.writes]
        self.assertEqual(commands[0], 0x51)
        self.assertEqual(set(commands[1:-3]), {0x52})
        self.assertEqual(len(commands[1:-3]), IR_SIZE // 64)
        self.assertEqual(commands[-3:], [0x53, 0x55, 0x57])

    async def test_an_amplifier_uses_the_amplifier_commands_and_larger_chunks(self):
        new = blob("n", 2, AMP_SIZE)
        new = b"DDPB" + new[4:]
        await self.device.write_asset(ap.AMP, 29, new, safety_backup=self.safety)
        commands = [c for c, _ in self.pedal.writes]
        self.assertEqual((commands[0], commands[-1]), (0x31, 0x33))
        self.assertEqual(self.pedal.data[("amp", 29)], new)

    async def test_the_slot_that_was_active_is_active_again_after_writing_another(self):
        self.assertEqual(self.pedal.active, {"amp": 12, "ir": 2})
        await self.device.write_asset(ap.IR, 29, ir_blob(11), safety_backup=self.safety)
        self.assertEqual(self.pedal.active["ir"], 2)
        self.assertEqual(self.pedal.live_selects, [bytes([7, 2])])
        other = Path(self._tmp.name) / "old-amp.bin"
        await self.device.write_asset(ap.AMP, 29, b"DDPB" + bytes(AMP_SIZE - 4), safety_backup=other)
        self.assertEqual(self.pedal.active["amp"], 12)

    async def test_writing_the_active_slot_selects_nothing_else(self):
        await self.device.write_asset(ap.IR, 2, ir_blob(12), safety_backup=self.safety)
        self.assertEqual(self.pedal.active["ir"], 2)
        self.assertEqual(self.pedal.live_selects, [])

    async def test_the_previous_selection_can_be_left_alone_on_request(self):
        await self.device.write_asset(ap.IR, 29, ir_blob(13), safety_backup=self.safety, keep_selection=False)
        self.assertEqual(self.pedal.active["ir"], 29)

    async def test_a_failed_chunk_aborts_the_session_and_leaves_the_slot_alone(self):
        old = self.pedal.data[("ir", 29)]
        self.pedal.fail_chunk = 3
        with self.assertRaises(NanocoreError):
            await self.device.write_asset(ap.IR, 29, ir_blob(3), safety_backup=self.safety)
        self.assertEqual(self.pedal.writes[-1][0], 0x54)  # the abort
        self.assertEqual(self.pedal.data[("ir", 29)], old)
        self.assertTrue(self.safety.exists())

    async def test_a_commit_the_pedal_refuses_is_reported_and_aborted(self):
        self.pedal.corrupt_commit = True
        with self.assertRaises(NanocoreError):
            await self.device.write_asset(ap.IR, 29, ir_blob(4), safety_backup=self.safety)
        self.assertEqual(self.pedal.writes[-1][0], 0x54)

    async def test_nothing_is_sent_for_a_request_that_cannot_be_valid(self):
        too_big = ir_blob(5, 1025)
        cases = [
            (ap.IR, 29, too_big, {}),
            (ap.IR, 30, ir_blob(6), {}),
            (ap.AMP, 38, b"DDPB" + bytes(AMP_SIZE - 4), {}),
            (ap.AMP, 1, b"XXXX" + bytes(AMP_SIZE - 4), {}),
            (ap.IR, 1, ir_blob(7), {"name": "a very long name for a slot"}),
        ]
        for kind, slot, data, extra in cases:
            with self.subTest(kind=kind.name, slot=slot), self.assertRaises(ValidationError):
                await self.device.write_asset(kind, slot, data, safety_backup=self.safety, **extra)
        self.assertEqual(self.pedal.writes, [])
        self.assertFalse(self.safety.exists())

    async def test_an_existing_safety_file_is_never_replaced(self):
        self.safety.write_bytes(b"keep me")
        with self.assertRaises(ValidationError):
            await self.device.write_asset(ap.IR, 29, ir_blob(8), safety_backup=self.safety)
        self.assertEqual(self.safety.read_bytes(), b"keep me")
        self.assertEqual(self.pedal.writes, [])


class CliWriteTest(CliTestCase):
    def setUp(self):
        super().setUp()
        self.session = WritablePedal()
        self.data_file = self.directory / "ir.bin"
        self.data_file.write_bytes(ir_blob(1))

    def test_without_apply_it_only_prints_the_plan(self):
        code, output = self.run_usb("assets", "write", "ir", "29", str(self.data_file))
        self.assertEqual(code, 0)
        self.assertIn("plan", output)
        self.assertIn("--apply", output)
        self.assertEqual(self.session.writes, [])
        self.assertNothingOpened()

    def test_apply_needs_a_safety_backup(self):
        code, _ = self.run_usb("assets", "write", "ir", "29", str(self.data_file), "--apply")
        self.assertNotEqual(code, 0)
        self.assertNothingOpened()

    def test_apply_writes_and_verifies(self):
        safety = self.directory / "old-ir.bin"
        code, output = self.run_usb(
            "assets", "write", "ir", "29", str(self.data_file), "--apply", "--safety-backup", str(safety), "--name", "Mine"
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.session.data[("ir", 29)], self.data_file.read_bytes())
        self.assertIn("verified", output)
        self.assertTrue(safety.exists())


if __name__ == "__main__":
    unittest.main()
