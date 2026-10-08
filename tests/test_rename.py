import json
import tempfile
import unittest
from pathlib import Path

from nanocore_controller.device import NanocoreDevice
from nanocore_controller.errors import ValidationError, VerificationError
from nanocore_controller.nanocore_protocol import preset_name_payload
from support import PedalSession, load_fixture
from test_cli import CliTestCase


class PayloadTest(unittest.TestCase):
    def test_field_8_with_the_length_and_the_characters(self):
        self.assertEqual(preset_name_payload("Clean"), bytes([8, 5]) + b"Clean")
        self.assertEqual(preset_name_payload("TightRhy"), bytes([8, 8]) + b"TightRhy")

    def test_trailing_spaces_are_dropped_because_the_pedal_pads_names(self):
        self.assertEqual(preset_name_payload("Lead  "), bytes([8, 4]) + b"Lead")

    def test_spaces_inside_are_kept(self):
        self.assertEqual(preset_name_payload("A B"), bytes([8, 3]) + b"A B")

    def test_names_the_pedal_cannot_show_are_refused(self):
        for bad in ("", "   ", "ninecharsX", "Ñandú", "tab\there", "new\nline", "\x7f", 5, None, b"abc"):
            with self.subTest(bad), self.assertRaises(ValidationError):
                preset_name_payload(bad)  # type: ignore[arg-type]


class DeviceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pedal = PedalSession(load_fixture())
        self.device = NanocoreDevice(self.pedal)
        await self.device.connect()
        self.addAsyncCleanup(self.device.close)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.backup = Path(self._tmp.name) / "before-rename.json"

    async def test_setting_the_name_is_live_only_and_checks_the_echo(self):
        name = await self.device.set_preset_name("Metal")
        self.assertEqual(name, "Metal")
        self.assertEqual(self.pedal.live_writes[-1], bytes([8, 5]) + b"Metal")
        self.assertEqual(self.pedal.saved_slots, [])

    async def test_an_echo_that_is_not_the_name_is_reported(self):
        self.pedal.name_echo = bytes([8, 3]) + b"abc"
        with self.assertRaises(VerificationError):
            await self.device.set_preset_name("Metal")

    async def test_an_invalid_name_sends_nothing(self):
        before = len(self.pedal.live_writes)
        with self.assertRaises(ValidationError):
            await self.device.set_preset_name("far too long")
        self.assertEqual(len(self.pedal.live_writes), before)

    async def test_renaming_stores_the_name_after_a_backup_and_reads_it_back(self):
        name = await self.device.rename_active("Metal", self.backup)
        self.assertEqual(name, "Metal")
        self.assertTrue(self.backup.exists())
        self.assertEqual(self.pedal.saved_slots, [self.pedal.runtime[1]])
        catalog = await self.device.catalog()
        self.assertEqual([e["name"].rstrip() for e in catalog][0], "Metal")

    async def test_a_name_that_does_not_stick_in_the_catalog_is_reported(self):
        self.pedal.catalog_names = {}
        original = self.pedal.query

        async def refuse_to_store(command, payload=b"", **kwargs):
            if command == 0x46:
                self.pedal.live_name = None  # the pedal saves, but the name is not part of it
            return await original(command, payload, **kwargs)

        self.pedal.query = refuse_to_store  # type: ignore[method-assign]
        with self.assertRaises(VerificationError):
            await self.device.rename_active("Metal", self.backup)


class CliTest(CliTestCase):
    def test_rename_needs_a_safety_backup(self):
        code, _ = self.run_usb("rename", "Metal")
        self.assertNotEqual(code, 0)
        self.assertNothingOpened()

    def test_an_invalid_name_is_refused_before_opening_anything(self):
        code, _ = self.run_usb("rename", "far too long", "--safety-backup", str(self.directory / "b.json"))
        self.assertNotEqual(code, 0)
        self.assertNothingOpened()

    def test_rename_stores_the_new_name(self):
        backup = self.directory / "rename-backup.json"
        code, output = self.run_usb("rename", "Metal", "--safety-backup", str(backup))
        self.assertEqual(code, 0)
        self.assertIn("Metal", output)
        self.assertTrue(backup.exists())
        self.assertEqual(self.session.catalog_names[self.session.runtime[1]], "Metal")
        self.assertEqual(json.loads(backup.read_text())["format"], "nanocore-controller-backup")


if __name__ == "__main__":
    unittest.main()
