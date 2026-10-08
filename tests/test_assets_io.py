import hashlib
import json
import stat
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from nanocore_controller import assets_protocol as ap
from nanocore_controller.asset_backup import backup_assets
from nanocore_controller.device import NanocoreDevice
from nanocore_controller.errors import ProtocolError, ValidationError
from nanocore_controller.nanocore_protocol import NanocoreResponse
from support import PedalSession, load_fixture
from test_cli import CliTestCase

AMP_SLOTS, IR_SLOTS = 38, 30
AMP_SIZE, IR_SIZE = 12242, 4096


def blob(kind: str, slot: int, size: int) -> bytes:
    seed = f"{kind}{slot}".encode()
    out = bytearray()
    while len(out) < size:
        seed = hashlib.sha256(seed).digest()
        out += seed
    return bytes(out[:size])


class AssetPedal(PedalSession):
    """Serves the storage info, slot infos and slot reads of 38 amplifier and 30 IR slots."""

    def __init__(self) -> None:
        super().__init__(load_fixture())
        self.data = {
            ("amp", s): blob("amp", s, AMP_SIZE) for s in range(AMP_SLOTS)
        } | {("ir", s): blob("ir", s, IR_SIZE if s != 11 else 2780) for s in range(IR_SLOTS)}
        self.names = {key: f"{key[0].upper()}{key[1]:02d}" for key in self.data}
        self.active = {"amp": 12, "ir": 2}
        self.corrupt: set[tuple[str, int]] = set()
        self.read_log: list[tuple[int, bytes]] = []
        self.lose: dict[tuple[int, bytes], int] = {}  # (command, payload start) -> replies to lose before answering

    def _kind(self, command: int) -> str | None:
        if command in (0x30, 0x36, 0x39):
            return "amp"
        if command in (0x50, 0x56, 0x59):
            return "ir"
        return None

    async def query(self, command, payload=b"", **kwargs):
        kind = self._kind(command)
        if kind is None:
            return await super().query(command, payload, **kwargs)
        key = (command, bytes(payload[:3]))
        if self.lose.get(key, 0) > 0:  # a reply lost on the link, as Bluetooth does now and then
            self.lose[key] -= 1
            from nanocore_controller.errors import DeviceTimeout

            raise DeviceTimeout(command, maybe_applied=False)
        count = AMP_SLOTS if kind == "amp" else IR_SLOTS
        limit = 12288 if kind == "amp" else 4096
        if command in (0x30, 0x50):
            body = bytes(8) + struct.pack("<IIH", count, limit, self.active[kind]) + bytes(2)
            return NanocoreResponse(1, command, 0, body)
        slot = payload[0]
        if slot >= count:
            from nanocore_controller.errors import DeviceStatusError

            raise DeviceStatusError(command, 3)
        data = self.data[(kind, slot)]
        if command in (0x36, 0x56):
            name = self.names[(kind, slot)].encode().ljust(16, b"\0")
            body = bytes((slot, 1, int(self.active[kind] == slot)))
            body += struct.pack("<II", len(data), zlib.crc32(data)) + bytes.fromhex("1a1a00") + name
            return NanocoreResponse(1, command, 0, body)
        offset, wanted = struct.unpack_from("<HB", payload, 1)
        self.read_log.append((command, payload))
        chunk = data[offset : offset + wanted]
        if (kind, slot) in self.corrupt and chunk:
            chunk = bytes([chunk[0] ^ 0xFF]) + chunk[1:]
        return NanocoreResponse(1, command, 0, struct.pack("<BHH", slot, offset, len(data)) + chunk)


class ProtocolLimitsTest(unittest.TestCase):
    def test_amplifier_slots_go_up_to_37(self):
        self.assertEqual(ap.slot_info_payload(37), bytes([37]))
        self.assertEqual(ap.read_payload(37, 0, 16), struct.pack("<BHB", 37, 0, 16))

    def test_slot_numbers_still_have_a_limit(self):
        for bad in (-1, 38, 300, True, "3"):
            with self.subTest(bad), self.assertRaises(ValidationError):
                ap.slot_info_payload(bad)  # type: ignore[arg-type]


class DeviceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pedal = AssetPedal()
        self.device = NanocoreDevice(self.pedal)
        await self.device.connect()
        self.addAsyncCleanup(self.device.close)

    async def test_storage_info_tells_the_slot_count_and_the_active_slot(self):
        amp = await self.device.asset_storage(ap.AMP)
        ir = await self.device.asset_storage(ap.IR)
        self.assertEqual((amp.slot_count, amp.active_slot, ir.slot_count, ir.active_slot), (38, 12, 30, 2))

    async def test_listing_returns_every_present_slot_with_its_name(self):
        amps = await self.device.list_assets(ap.AMP)
        self.assertEqual(len(amps), 38)
        self.assertEqual([a.slot for a in amps][:3], [0, 1, 2])
        self.assertEqual(amps[12].name, "AMP12")
        self.assertTrue(amps[12].active and not amps[0].active)

    async def test_reading_a_slot_returns_data_checked_against_its_crc(self):
        info, data = await self.device.read_asset(ap.IR, 11)
        self.assertEqual(len(data), 2780)
        self.assertEqual(data, self.pedal.data[("ir", 11)])
        self.assertEqual(info.checksum, zlib.crc32(data))

    async def test_a_slot_is_read_in_windows_of_at_most_200_bytes(self):
        await self.device.read_asset(ap.IR, 2)
        sizes = [payload[3] for _, payload in self.pedal.read_log]
        self.assertTrue(all(1 <= n <= 200 for n in sizes))
        self.assertEqual(sum(sizes), IR_SIZE)

    async def test_a_reply_lost_on_the_link_is_asked_for_again(self):
        self.pedal.lose[(0x36, bytes([10]))] = 2  # slot info of amplifier 10, twice
        amps = await self.device.list_assets(ap.AMP)
        self.assertEqual(len(amps), 38)
        self.pedal.lose[(0x59, struct.pack("<BHB", 2, 200, 200)[:3])] = 1  # a window of the IR read
        info, data = await self.device.read_asset(ap.IR, 2)
        self.assertEqual(data, self.pedal.data[("ir", 2)])

    async def test_a_link_that_stays_silent_is_reported_after_a_few_tries(self):
        from nanocore_controller.errors import DeviceTimeout

        self.pedal.lose[(0x36, bytes([4]))] = 100
        with self.assertRaises(DeviceTimeout):
            await self.device.list_assets(ap.AMP)

    async def test_corrupt_data_is_reported_not_returned(self):
        self.pedal.corrupt.add(("amp", 5))
        with self.assertRaises(ProtocolError):
            await self.device.read_asset(ap.AMP, 5)

    async def test_a_slot_that_does_not_exist_is_refused_before_any_read(self):
        with self.assertRaises(ValidationError):
            await self.device.read_asset(ap.IR, 30)
        self.assertEqual(self.pedal.read_log, [])


class BackupTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pedal = AssetPedal()
        self.device = NanocoreDevice(self.pedal)
        await self.device.connect()
        self.addAsyncCleanup(self.device.close)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name) / "assets"

    async def test_every_slot_is_written_with_a_manifest_and_private_permissions(self):
        manifest = await backup_assets(self.device, self.directory)
        self.assertEqual(len(manifest["items"]), 68)
        files = sorted(p.name for p in self.directory.iterdir())
        self.assertIn("amp-12-AMP12.bin", files)
        self.assertIn("ir-29-IR29.bin", files)
        self.assertEqual(stat.S_IMODE((self.directory / "amp-12-AMP12.bin").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual((self.directory / "amp-12-AMP12.bin").read_bytes(), self.pedal.data[("amp", 12)])

    async def test_the_manifest_records_what_is_needed_to_check_and_restore(self):
        await backup_assets(self.device, self.directory)
        saved = json.loads((self.directory / "manifest.json").read_text())
        self.assertEqual(saved["format"], "nanocore-controller-assets")
        item = next(i for i in saved["items"] if i["kind"] == "ir" and i["slot"] == 11)
        data = self.pedal.data[("ir", 11)]
        self.assertEqual(
            (item["size"], item["crc32"], item["sha256"], item["name"], item["file"]),
            (2780, zlib.crc32(data), hashlib.sha256(data).hexdigest(), "IR11", "ir-11-IR11.bin"),
        )

    async def test_an_existing_backup_is_never_overwritten(self):
        await backup_assets(self.device, self.directory)
        with self.assertRaises(ValidationError):
            await backup_assets(self.device, self.directory)

    async def test_one_kind_can_be_backed_up_on_its_own(self):
        manifest = await backup_assets(self.device, self.directory, kinds=(ap.IR,))
        self.assertEqual({i["kind"] for i in manifest["items"]}, {"ir"})
        self.assertEqual(len(manifest["items"]), 30)

    async def test_a_corrupt_slot_stops_the_backup_and_is_named(self):
        self.pedal.corrupt.add(("amp", 7))
        with self.assertRaises(ProtocolError) as raised:
            await backup_assets(self.device, self.directory)
        self.assertIn("amp 7", str(raised.exception))


class CliTest(CliTestCase):
    def setUp(self):
        super().setUp()
        self.session = AssetPedal()

    def test_list_prints_both_kinds(self):
        code, output = self.run_usb("assets", "list")
        self.assertEqual(code, 0)
        document = json.loads(output)
        self.assertEqual((len(document["amp"]), len(document["ir"])), (38, 30))
        self.assertEqual(document["amp"][12]["name"], "AMP12")
        self.assertTrue(document["amp"][12]["active"])

    def test_read_writes_one_slot_to_a_new_file(self):
        out = self.directory / "amp12.bin"
        code, output = self.run_usb("assets", "read", "amp", "12", str(out))
        self.assertEqual(code, 0)
        self.assertEqual(out.read_bytes(), self.session.data[("amp", 12)])
        self.assertIn("crc32", output)

    def test_backup_writes_the_whole_set(self):
        target = self.directory / "assets-backup"
        code, output = self.run_usb("assets", "backup", str(target))
        self.assertEqual(code, 0)
        self.assertEqual(len(list(target.glob("*.bin"))), 68)
        self.assertTrue((target / "manifest.json").exists())
        for leftover in list(target.iterdir()):
            leftover.unlink()
        target.rmdir()

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(SystemExit) as raised:
            self.run_usb("assets", "read", "cab", "1", str(self.directory / "x.bin"))
        self.assertEqual(raised.exception.code, 2)
        self.assertNothingOpened()


if __name__ == "__main__":
    unittest.main()
