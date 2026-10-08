import json
import unittest

from nanocore_controller.device import NanocoreDevice
from nanocore_controller.errors import ProtocolError, ValidationError, VerificationError
from nanocore_controller.nanocore_protocol import (
    GET_SETTINGS_COMMAND,
    GlobalSettings,
    global_settings_payload,
    parse_global_settings,
)
from support import PedalSession, load_fixture
from test_cli import CliTestCase

# What a real pedal answered to command 0x65 (firmware as of 2026-10-08), with the input gain set to +1 in the app.
REAL_REPLY = bytes.fromhex("01 01 00 01 64 64 00")


class ParseTest(unittest.TestCase):
    def test_a_real_reply(self):
        settings = parse_global_settings(REAL_REPLY)
        self.assertEqual(
            settings,
            GlobalSettings(
                version=1,
                wireless_enabled=True,
                loopback_enabled=False,
                input_gain_db=1,
                usb_volume=100,
                bt_volume=100,
                midi_channel=0,
            ),
        )

    def test_the_input_gain_is_a_signed_byte(self):
        low = parse_global_settings(bytes([1, 1, 0, 0xEC, 50, 50, 0]))
        high = parse_global_settings(bytes([1, 1, 0, 20, 50, 50, 0]))
        self.assertEqual((low.input_gain_db, high.input_gain_db), (-20, 20))

    def test_volumes_and_channel_are_limited_like_the_official_app_does(self):
        settings = parse_global_settings(bytes([1, 0, 1, 0, 0x70, 0xFF, 0x40]))
        self.assertEqual((settings.usb_volume, settings.bt_volume, settings.midi_channel), (100, 100, 16))
        self.assertEqual((settings.wireless_enabled, settings.loopback_enabled), (False, True))

    def test_a_reply_that_is_too_short_is_refused(self):
        with self.assertRaises(ProtocolError):
            parse_global_settings(REAL_REPLY[:6])

    def test_a_newer_layout_needs_its_two_extra_bytes_in_order(self):
        settings = parse_global_settings(bytes([2, 1, 0, 0, 50, 50, 0, 10, 90]))
        self.assertEqual((settings.version, settings.volume_floor, settings.volume_ceiling), (2, 10, 90))
        for bad in (bytes([2, 1, 0, 0, 50, 50, 0]), bytes([2, 1, 0, 0, 50, 50, 0, 90, 10])):
            with self.assertRaises(ProtocolError):
                parse_global_settings(bad)


class PayloadTest(unittest.TestCase):
    def test_nothing_to_change_is_refused(self):
        with self.assertRaises(ValidationError):
            global_settings_payload()

    def test_the_mask_is_one_byte_not_two(self):
        # A two-byte mask was sent first and the pedal read the second byte as the value: it set the
        # input gain to 0 and refused the rest with status 0x01.
        payload = global_settings_payload(input_gain_db=1)
        self.assertEqual(len(payload), 2)
        self.assertEqual(payload, bytes([0x04, 0x01]))

    def test_one_field(self):
        self.assertEqual(global_settings_payload(input_gain_db=2), bytes([0x04, 0x02]))
        self.assertEqual(global_settings_payload(input_gain_db=-3), bytes([0x04, 0xFD]))

    def test_every_field_in_mask_order(self):
        payload = global_settings_payload(
            wireless_enabled=False,
            loopback_enabled=True,
            input_gain_db=-3,
            usb_volume=50,
            bt_volume=60,
            midi_channel=5,
        )
        self.assertEqual(payload, bytes([0x3F, 0x00, 0x01, 0xFD, 50, 60, 5]))

    def test_fields_are_written_in_mask_order_whatever_order_they_are_given(self):
        a = global_settings_payload(midi_channel=5, wireless_enabled=True)
        b = global_settings_payload(wireless_enabled=True, midi_channel=5)
        self.assertEqual(a, b)
        self.assertEqual(a, bytes([0x21, 0x01, 0x05]))

    def test_values_outside_what_the_app_allows_are_refused(self):
        for kwargs in (
            {"input_gain_db": 21},
            {"input_gain_db": -21},
            {"usb_volume": 101},
            {"usb_volume": -1},
            {"bt_volume": 101},
            {"midi_channel": 17},
            {"midi_channel": -1},
        ):
            with self.subTest(kwargs), self.assertRaises(ValidationError):
                global_settings_payload(**kwargs)

    def test_types_are_strict(self):
        for kwargs in (
            {"input_gain_db": 1.5},
            {"input_gain_db": True},
            {"usb_volume": "50"},
            {"wireless_enabled": 1},
            {"loopback_enabled": "yes"},
        ):
            with self.subTest(kwargs), self.assertRaises(ValidationError):
                global_settings_payload(**kwargs)


class DeviceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pedal = PedalSession(load_fixture())
        self.device = NanocoreDevice(self.pedal)
        await self.device.connect()
        self.addAsyncCleanup(self.device.close)

    async def test_reading_asks_command_0x65_with_no_payload(self):
        settings = await self.device.read_global_settings()
        self.assertEqual(settings.input_gain_db, 1)
        self.assertEqual(self.pedal.queries[-1], (GET_SETTINGS_COMMAND, b""))

    async def test_writing_sends_command_0x66_and_returns_the_new_settings(self):
        settings = await self.device.write_global_settings(input_gain_db=2)
        self.assertEqual(self.pedal.set_payloads, [bytes([0x04, 0x02])])
        self.assertEqual(settings.input_gain_db, 2)
        self.assertEqual((await self.device.read_global_settings()).input_gain_db, 2)

    async def test_a_write_the_pedal_did_not_take_is_reported(self):
        self.pedal.ignore_settings_writes = True
        with self.assertRaises(VerificationError):
            await self.device.write_global_settings(input_gain_db=2)

    async def test_nothing_is_sent_for_an_invalid_write(self):
        before = len(self.pedal.queries)
        with self.assertRaises(ValidationError):
            await self.device.write_global_settings(input_gain_db=99)
        with self.assertRaises(ValidationError):
            await self.device.write_global_settings()
        self.assertEqual(len(self.pedal.queries), before)


class CliTest(CliTestCase):
    def test_settings_reads_and_prints_them(self):
        code, output = self.run_usb("settings")
        self.assertEqual(code, 0)
        document = json.loads(output)
        self.assertEqual(document["input_gain_db"], 1)
        self.assertEqual((document["usb_volume"], document["bt_volume"], document["midi_channel"]), (100, 100, 0))
        self.assertEqual(self.session.set_payloads, [])

    def test_changes_without_apply_print_the_plan_and_open_nothing(self):
        code, output = self.run_usb("settings", "--input-gain", "2", "--loopback", "on")
        self.assertEqual(code, 0)
        self.assertIn("0x66", output)
        self.assertIn("06 01 02", output)
        self.assertIn("--apply", output)
        self.assertNothingOpened()

    def test_apply_writes_and_prints_what_the_pedal_reports(self):
        code, output = self.run_usb("settings", "--input-gain", "2", "--bluetooth", "off", "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(self.session.set_payloads, [bytes([0x05, 0x00, 0x02])])
        document = json.loads(output)
        self.assertEqual((document["input_gain_db"], document["wireless_enabled"]), (2, False))

    def test_apply_without_any_change_is_refused(self):
        code, _ = self.run_usb("settings", "--apply")
        self.assertNotEqual(code, 0)
        self.assertNothingOpened()

    def test_values_are_checked_before_anything_is_opened(self):
        for flag, value in (("--input-gain", "99"), ("--usb-volume", "101"), ("--midi-channel", "17")):
            with self.subTest(flag):
                code, _ = self.run_usb("settings", flag, value, "--apply")
                self.assertNotEqual(code, 0)
                self.assertNothingOpened()
        self.assertEqual(self.session.set_payloads, [])

    def test_dry_run_cannot_be_combined_with_apply(self):
        code, _ = self.run_usb("--dry-run", "settings", "--input-gain", "2", "--apply")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.session.set_payloads, [])


if __name__ == "__main__":
    unittest.main()
