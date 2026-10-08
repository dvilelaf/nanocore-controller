import contextlib
import io
import json
import logging
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from nanocore_controller import cli
from nanocore_controller.cli import COMMANDS, DryRun, build_parser, command_payload, run
from nanocore_controller.config import DeviceProfile
from nanocore_controller.errors import DeviceTimeout
from support import PedalSession, load_fixture

ADDRESS = "AA:BB:CC:DD:EE:FF"
FIXTURE = Path(__file__).parent / "fixtures" / "backup-v1.json"
README = Path(__file__).parent.parent / "README.md"
USB_PORT = "hw:6,0,0"


class CliTestCase(unittest.TestCase):
    """Runs the CLI against a stateful fake pedal; no transport is ever really opened."""

    def setUp(self):
        self.session = PedalSession(load_fixture())
        self.opened = []
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(self.cleanup_directory)
        self.addCleanup(cli.configure_logging, 0)

    def cleanup_directory(self):
        for path in self.directory.iterdir():
            path.unlink()
        self.directory.rmdir()

    def open_ble(self, address, *, adapter):
        self.opened.append(("ble", address, adapter))
        return self.session

    def open_usb(self, port):
        self.opened.append(("usb", port))
        return self.session

    def run_cli(self, *args, profile=None, stderr=None):
        if profile is None:
            profile = DeviceProfile(ADDRESS, "hci1")
        output = io.StringIO()
        with (
            mock.patch("nanocore_controller.cli.load_profile", return_value=profile) as load,
            mock.patch("nanocore_controller.cli.BleMidiSession", self.open_ble),
            mock.patch("nanocore_controller.cli.AmidiSession", self.open_usb),
            mock.patch(
                "nanocore_controller.cli.discover_nanocore_midi_port", return_value=USB_PORT
            ) as discover,
            contextlib.redirect_stderr(stderr or io.StringIO()),
        ):
            code = run(list(args), output=output)
        self.load_profile, self.discover = load, discover
        return code, output.getvalue()

    def run_usb(self, *args):
        return self.run_cli("--transport", "usb", "--port", USB_PORT, *args)

    def assertNothingOpened(self):
        self.assertEqual(self.opened, [])
        self.assertEqual(self.session.connect_count, 0)


class BluetoothTests(CliTestCase):
    def test_parser_defaults_to_bluetooth_and_accepts_global_connection_options(self):
        args = build_parser().parse_args(
            ["--address", ADDRESS, "--adapter", "hci2", "--config", "/tmp/nano.json", "status"]
        )
        self.assertEqual(args.transport, "bluetooth")
        self.assertEqual(args.address, ADDRESS)
        self.assertEqual(args.adapter, "hci2")
        self.assertEqual(args.config, "/tmp/nano.json")

    def test_configure_saves_profile_and_reports_exact_path(self):
        path = self.directory / "profile.json"
        with mock.patch("nanocore_controller.cli.save_profile") as save:
            code, output = self.run_cli("--config", str(path), "configure", ADDRESS, "--adapter", "hci7")
        self.assertEqual(code, 0)
        save.assert_called_once_with(DeviceProfile(ADDRESS, "hci7"), path)
        self.assertIn(str(path), output)

    def test_missing_profile_has_actionable_configure_message_and_never_discovers_usb(self):
        output = io.StringIO()
        with (
            mock.patch(
                "nanocore_controller.cli.load_profile", side_effect=FileNotFoundError("missing")
            ),
            mock.patch("nanocore_controller.cli.discover_nanocore_midi_port") as discover,
            mock.patch("nanocore_controller.cli.BleMidiSession") as bluetooth,
        ):
            code = run(["status"], output=output)
        self.assertEqual(code, 2)
        self.assertIn("configure", output.getvalue())
        discover.assert_not_called()
        bluetooth.assert_not_called()

    def test_devices_defaults_to_configured_bluetooth_endpoint(self):
        code, output = self.run_cli("devices")
        self.assertEqual(code, 0)
        self.assertIn(ADDRESS, output)
        self.assertIn("hci1", output)
        self.assertNothingOpened()

    def test_explicit_usb_devices_discovers_midi_and_never_opens_a_session(self):
        code, output = self.run_cli("--transport", "usb", "devices")
        self.assertEqual(code, 0)
        self.assertIn(USB_PORT, output)
        self.assertNothingOpened()

    def test_status_and_presets_read_through_one_session(self):
        code, output = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["preset"]["slot"], 8)
        self.assertEqual((self.session.connect_count, self.session.close_count), (1, 1))
        self.assertEqual(self.opened, [("ble", ADDRESS, "hci1")])
        self.session = PedalSession(load_fixture())
        code, output = self.run_cli("presets")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)[0]["slot"], 8)

    def test_status_records_the_bluetooth_endpoint_in_the_document(self):
        code, output = self.run_cli("status")
        self.assertEqual(json.loads(output)["device"], {"address": ADDRESS, "adapter": "hci1"})

    def test_explicit_connection_options_override_saved_profile(self):
        code, unused = self.run_cli("--address", "00:11:22:33:44:55", "--adapter", "hci9", "status")
        self.assertEqual(code, 0)
        self.assertEqual(self.opened, [("ble", "00:11:22:33:44:55", "hci9")])

    def test_ble_status_and_catalog_aliases_preserve_positionals(self):
        code, unused = self.run_cli("ble-status", "00:11:22:33:44:55", "--adapter", "hci4")
        self.assertEqual(code, 0)
        self.assertEqual(self.opened, [("ble", "00:11:22:33:44:55", "hci4")])
        self.opened.clear()
        code, output = self.run_cli("ble-catalog", ADDRESS)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)[0]["display_number"], 9)

    def test_bluetooth_preset_uses_private_selection_not_program_change(self):
        code, output = self.run_cli("preset", "9")
        self.assertEqual(code, 0)
        self.assertEqual(self.session.queries[0], (0x6D, bytes.fromhex("09 08")))
        self.assertEqual(self.session.sent, [])
        self.assertIn("preset 9", output)

    def test_invalid_preset_number_is_rejected_before_connecting(self):
        code, output = self.run_cli("preset", "129")
        self.assertEqual(code, 2)
        self.assertIn("1 to 128", output)
        self.assertNothingOpened()

    def test_bluetooth_live_midi_commands_print_bytes_and_read_back_cheaply(self):
        cases = (
            (("block", "reverb", "on"), bytes.fromhex("b0 1b 7f")),
            (("type", "amp", "4"), bytes.fromhex("b0 2b 04")),
            (("param", "amp_gain", "50%"), bytes.fromhex("b0 3c 40")),
        )
        for argv, expected in cases:
            with self.subTest(argv=argv):
                self.session = PedalSession(load_fixture())
                code, output = self.run_cli(*argv)
                self.assertEqual(code, 0)
                self.assertEqual(self.session.sent, [expected])
                self.assertEqual(self.session.log, [0x63])
                self.assertIn(expected.hex(" "), output)

    def test_bluetooth_next_previous_and_tuner_send_existing_midi_payloads(self):
        cases = (
            (("next",), bytes.fromhex("b0 52 7f")),
            (("previous",), bytes.fromhex("b0 51 7f")),
            (("tuner", "on"), bytes.fromhex("b0 50 7f")),
        )
        for argv, expected in cases:
            with self.subTest(argv=argv):
                self.session = PedalSession(load_fixture())
                code, output = self.run_cli(*argv)
                self.assertEqual(code, 0)
                self.assertEqual(self.session.sent, [expected])
                self.assertEqual(self.session.log, [])
                self.assertIn(expected.hex(" "), output)

    def test_bluetooth_connection_error_never_falls_back_to_usb(self):
        self.session.fail_connect = RuntimeError("BLE unavailable")
        code, output = self.run_cli("status")
        self.assertEqual(code, 2)
        self.assertIn("BLE unavailable", output)
        self.assertEqual(self.opened, [("ble", ADDRESS, "hci1")])
        self.discover.assert_not_called()

    def test_ble_capture_validates_address_and_adapter(self):
        with mock.patch("nanocore_controller.cli.capture_ble") as capture:
            code, output = self.run_cli("ble-capture", "not-an-address")
        self.assertEqual(code, 2)
        self.assertIn("Bluetooth address", output)
        capture.assert_not_called()


class DryRunTests(CliTestCase):
    def test_dry_run_opens_no_device_and_prints_live_midi(self):
        code, output = self.run_cli("--dry-run", "tuner", "on")
        self.assertEqual(code, 0)
        self.assertIn("b0 50 7f", output)
        self.assertNothingOpened()
        self.discover.assert_not_called()

    def test_bluetooth_preset_dry_run_prints_deterministic_private_sysex(self):
        code, output = self.run_cli("--dry-run", "preset", "9")
        self.assertEqual(code, 0)
        self.assertIn("f0 7d 4e 43 70 00 02 00 00 6d 00 02 00 00 09 08 f7", output)
        self.assertNothingOpened()

    def test_usb_preset_dry_run_prints_program_change(self):
        code, output = self.run_cli("--transport", "usb", "--dry-run", "preset", "9")
        self.assertEqual(code, 0)
        self.assertIn("c0 08", output)
        self.assertNothingOpened()

    def test_commands_that_touch_the_pedal_reject_dry_run_without_loading_config(self):
        for argv in (
            ["status"],
            ["presets"],
            ["backup", "unused.json"],
            ["save", "--safety-backup", "unused.json"],
            ["ble-capture", ADDRESS],
        ):
            for transport in ("bluetooth", "usb"):
                with self.subTest(argv=argv, transport=transport):
                    code, output = self.run_cli("--transport", transport, "--dry-run", *argv)
                    self.assertEqual(code, 2)
                    self.assertIn("--dry-run", output)
                    self.load_profile.assert_not_called()
                    self.assertNothingOpened()

    def test_dry_run_cannot_be_combined_with_restore_apply(self):
        code, output = self.run_cli(
            "--dry-run", "restore", str(FIXTURE), "--apply", "--safety-backup", "x.json"
        )
        self.assertEqual(code, 2)
        self.assertIn("--dry-run cannot be combined with --apply", output)
        self.assertNothingOpened()

    def test_usb_devices_dry_run_requires_a_port(self):
        code, output = self.run_cli("--transport", "usb", "--dry-run", "devices")
        self.assertEqual(code, 2)
        self.assertIn("requires --port", output)
        self.discover.assert_not_called()


class UsbTests(CliTestCase):
    def test_usb_preset_maps_visible_number_to_zero_based_program(self):
        code, output = self.run_usb("preset", "9")
        self.assertEqual(code, 0)
        self.assertEqual(self.session.sent, [bytes.fromhex("c0 08")])
        self.assertEqual(self.opened, [("usb", USB_PORT)])
        self.assertIn("c0 08", output)

    def test_usb_port_is_discovered_when_not_given(self):
        code, unused = self.run_cli("--transport", "usb", "next")
        self.assertEqual(code, 0)
        self.assertEqual(self.opened, [("usb", USB_PORT)])
        self.discover.assert_called_once()

    def test_usb_status_reads_and_records_the_port(self):
        code, output = self.run_usb("status")
        self.assertEqual(code, 0)
        document = json.loads(output)
        self.assertEqual(document["preset"]["slot"], 8)
        self.assertEqual(document["device"], {"transport": "usb", "port": USB_PORT})
        self.assertEqual((self.session.connect_count, self.session.close_count), (1, 1))

    def test_usb_presets(self):
        code, output = self.run_usb("presets")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)[0]["display_number"], 9)

    def test_usb_live_commands_read_back_with_the_cheap_snapshot(self):
        code, output = self.run_usb("param", "amp_gain", "50%")
        self.assertEqual(code, 0)
        self.assertEqual(self.session.log, [0x63])
        self.assertIn("b0 3c 40", output)

    def test_usb_backup(self):
        path = self.directory / "usb-backup.json"
        code, output = self.run_usb("backup", str(path))
        self.assertEqual(code, 0)
        self.assertIn(str(path), output)
        self.assertEqual(json.loads(path.read_text())["device"]["port"], USB_PORT)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_usb_save_writes_backup_then_flash(self):
        safety = self.directory / "before-save.json"
        code, output = self.run_usb("save", "--safety-backup", str(safety))
        self.assertEqual(code, 0)
        self.assertTrue(safety.exists())
        self.assertEqual(self.session.saved_slots, [8])
        self.assertIn("saved preset 9", output)

    def test_usb_restore_apply(self):
        safety = self.directory / "before-restore.json"
        code, output = self.run_usb("restore", str(FIXTURE), "--apply", "--safety-backup", str(safety))
        self.assertEqual(code, 0)
        self.assertTrue(safety.exists())
        self.assertGreater(len(self.session.live_writes), 0)
        self.assertIn("read-back verified", output)
        self.assertIn(str(safety), output)

    def test_usb_restore_plan_prints_without_a_device(self):
        code, output = self.run_usb("restore", str(FIXTURE))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)[0]["command"], "0x6d")
        self.assertNothingOpened()

    def test_usb_ble_capture_is_rejected_clearly(self):
        with mock.patch("nanocore_controller.cli.capture_ble") as capture:
            code, output = self.run_usb("ble-capture", ADDRESS)
        self.assertEqual(code, 2)
        self.assertIn("ble-capture is not supported with USB transport", output)
        capture.assert_not_called()
        self.assertNothingOpened()

    def test_usb_save_and_restore_still_require_a_safety_backup_before_opening(self):
        for argv in (("save",), ("restore", str(FIXTURE), "--apply")):
            with self.subTest(argv=argv):
                code, output = self.run_usb(*argv)
                self.assertEqual(code, 2)
                self.assertIn("--safety-backup", output)
                self.assertNothingOpened()


class WorkflowTests(CliTestCase):
    def test_backup_is_exclusive_and_goes_through_the_device(self):
        path = self.directory / "backup.json"
        code, output = self.run_cli("backup", str(path))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(path.read_text())["preset"]["slot"], 8)
        self.assertIn(f"backup written to {path}", output)
        code, output = self.run_cli("backup", str(path))
        self.assertEqual(code, 2)
        self.assertIn("error:", output)

    def test_ble_backup_alias_preserves_positionals(self):
        path = self.directory / "backup.json"
        code, unused = self.run_cli("ble-backup", "00:11:22:33:44:55", str(path))
        self.assertEqual(code, 0)
        self.assertEqual(self.opened, [("ble", "00:11:22:33:44:55", "hci1")])

    def test_restore_plan_is_nonwriting_and_requires_no_connection(self):
        code, output = self.run_cli("restore", str(FIXTURE))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)[0]["command"], "0x6d")
        self.load_profile.assert_not_called()
        self.assertNothingOpened()

    def test_restore_apply_saves_current_state_first_in_one_session(self):
        safety = self.directory / "before.json"
        code, output = self.run_cli("restore", str(FIXTURE), "--apply", "--safety-backup", str(safety))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(safety.read_text())["preset"]["slot"], 8)
        self.assertEqual((self.session.connect_count, self.session.close_count), (1, 1))
        self.assertIn("restored", output)
        self.assertIn("verified", output)

    def test_restore_apply_requires_safety_backup_before_connecting(self):
        code, output = self.run_cli("restore", str(FIXTURE), "--apply")
        self.assertEqual(code, 2)
        self.assertIn("--safety-backup", output)
        self.assertNothingOpened()

    def test_save_writes_safety_backup_before_persisting_active_preset(self):
        safety = self.directory / "before-save.json"
        self.session.before_command[0x46] = lambda session: self.assertTrue(safety.exists())
        code, output = self.run_cli("save", "--safety-backup", str(safety))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(safety.read_text())["preset"]["slot"], 8)
        self.assertEqual(self.session.saved_slots, [8])
        self.assertIn("saved preset 9", output)
        self.assertIn(str(safety), output)

    def test_save_requires_safety_backup_before_connecting(self):
        code, output = self.run_cli("save")
        self.assertEqual(code, 2)
        self.assertIn("--safety-backup", output)
        self.assertNothingOpened()

    def test_save_is_refused_when_the_active_preset_moves_before_the_write(self):
        safety = self.directory / "before-save.json"
        calls = []

        def switch_on_second_snapshot(session):
            calls.append(1)
            if len(calls) == 3:
                session.switch_to_slot(7)

        self.session.before_command[0x63] = switch_on_second_snapshot
        code, output = self.run_cli("save", "--safety-backup", str(safety))
        self.assertEqual(code, 2)
        self.assertIn("active preset changed", output)
        self.assertEqual(self.session.saved_slots, [])

    def test_invalid_restore_is_rejected_before_connecting_or_writing_safety_backup(self):
        backup = self.directory / "invalid.json"
        safety = self.directory / "before.json"
        backup.write_text('{"format": "wrong"}', encoding="utf-8")
        code, output = self.run_cli("restore", str(backup), "--apply", "--safety-backup", str(safety))
        self.assertEqual(code, 2)
        self.assertIn("unsupported", output)
        self.assertNothingOpened()
        self.assertFalse(safety.exists())

    def test_missing_restore_file_is_a_clean_error(self):
        code, output = self.run_cli("restore", str(self.directory / "absent.json"))
        self.assertEqual(code, 2)
        self.assertTrue(output.startswith("error:"))

    def test_restore_slot_mismatch_writes_nothing(self):
        safety = self.directory / "before.json"
        self.session.switch_to_slot(7)
        code, output = self.run_cli("restore", str(FIXTURE), "--apply", "--safety-backup", str(safety))
        self.assertEqual(code, 2)
        self.assertIn("preset slot 9", output)
        self.assertFalse(safety.exists())
        self.assertEqual(self.session.live_writes, [])

    def test_legacy_restore_can_use_backup_device_metadata(self):
        safety = self.directory / "before.json"
        with mock.patch(
            "nanocore_controller.cli.load_profile", side_effect=FileNotFoundError("missing")
        ):
            output = io.StringIO()
            with (
                mock.patch("nanocore_controller.cli.BleMidiSession", self.open_ble),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                code = run(
                    ["ble-restore", str(FIXTURE), "--apply", "--safety-backup", str(safety)],
                    output=output,
                )
        self.assertEqual(code, 0)
        self.assertEqual(self.opened, [("ble", "00:11:22:33:44:55", "hci1")])


class InjectionTests(CliTestCase):
    def test_a_device_factory_can_be_injected(self):
        from nanocore_controller.device import NanocoreDevice

        seen = []

        def factory(args, document):
            seen.append((args.command, document))
            return NanocoreDevice(self.session)

        output = io.StringIO()
        code = run(["status"], output=output, device_factory=factory)
        self.assertEqual(code, 0)
        self.assertEqual(seen, [("status", None)])
        self.assertEqual(json.loads(output.getvalue())["preset"]["slot"], 8)

    def test_building_a_device_opens_nothing(self):
        args = build_parser().parse_args(["--address", ADDRESS, "--adapter", "hci0", "status"])
        with (
            mock.patch("nanocore_controller.cli.BleMidiSession", self.open_ble),
            mock.patch("nanocore_controller.cli.load_profile", side_effect=FileNotFoundError),
        ):
            device = cli.create_device(args)
        self.assertEqual(self.opened, [("ble", ADDRESS, "hci0")])
        self.assertEqual(self.session.connect_count, 0)
        self.assertFalse(device.connected)


class ErrorTests(CliTestCase):
    def test_unconnected_timeout_that_may_have_been_applied_says_so(self):
        def time_out(session):
            raise DeviceTimeout(0x6D, maybe_applied=True)

        self.session.before_command[0x63] = time_out
        code, output = self.run_cli("block", "reverb", "on")
        self.assertEqual(code, 2)
        self.assertIn("error: NANOCORE command 0x6d timed out", output)
        self.assertIn("may have been applied", output)
        self.assertIn("nanocore status", output)
        self.assertNotIn("Traceback", output)

    def test_read_timeout_gets_no_applied_hint(self):
        def time_out(session):
            raise DeviceTimeout(0x63, maybe_applied=False)

        self.session.before_command[0x63] = time_out
        code, output = self.run_cli("status")
        self.assertEqual(code, 2)
        self.assertNotIn("may have been applied", output)

    def test_partial_restore_reports_counts_notes_and_safety_backup(self):
        safety = self.directory / "before.json"
        self.session.fail_all_live_writes_from = 2
        code, output = self.run_cli("restore", str(FIXTURE), "--apply", "--safety-backup", str(safety))
        self.assertEqual(code, 2)
        self.assertIn("operations were applied", output)
        self.assertIn(str(safety), output)
        self.assertIn("rolling back", output)
        self.assertNotIn("Traceback", output)

    def test_typed_errors_map_to_a_message_and_exit_code_2(self):
        from nanocore_controller import errors

        raised = (
            errors.DeviceNotFound("no pedal"),
            errors.DeviceDisconnected("link dropped"),
            errors.DeviceStatusError(0x63, 3),
            errors.ValidationError("bad input"),
            errors.BackupError("cannot write"),
            TimeoutError("slow"),
            OSError("io"),
            ValueError("value"),
            RuntimeError("runtime"),
        )
        for exc in raised:
            with self.subTest(error=type(exc).__name__):
                self.session = PedalSession(load_fixture())

                def fail(session, exc=exc):
                    raise exc

                self.session.before_command[0x63] = fail
                code, output = self.run_cli("status")
                self.assertEqual(code, 2)
                self.assertEqual(output.splitlines()[0], f"error: {exc}")
                self.assertNotIn("Traceback", output)

    def test_unexpected_exceptions_are_not_swallowed(self):
        def fail(session):
            raise LookupError("a bug")

        self.session.before_command[0x63] = fail
        with self.assertRaises(LookupError):
            self.run_cli("status")

    def test_the_sent_bytes_are_reported_even_if_the_read_back_fails(self):
        from nanocore_controller.errors import DeviceTimeout

        def broken(session):
            raise DeviceTimeout(0x63, maybe_applied=False)

        self.session.before_command[0x63] = broken
        code, output = self.run_cli("param", "amp_gain", "70%")
        self.assertEqual(code, 2)
        self.assertLess(output.index("sent bytes"), output.index("error:"))


class LoggingTests(CliTestCase):
    def nanocore_handlers(self):
        return [h for h in logging.getLogger("nanocore").handlers if not isinstance(h, logging.NullHandler)]

    def test_no_verbosity_configures_nothing(self):
        self.run_cli("status")
        self.assertEqual(self.nanocore_handlers(), [])
        self.assertEqual(logging.getLogger("nanocore.audit").level, logging.NOTSET)

    def test_v_logs_the_audit_trail_to_stderr_only(self):
        stderr = io.StringIO()
        code, output = self.run_cli("-v", "preset", "9", stderr=stderr)
        self.assertEqual(code, 0)
        self.assertEqual(logging.getLogger("nanocore.audit").level, logging.INFO)
        self.assertIn("nanocore.audit", stderr.getvalue())
        self.assertIn("select preset", stderr.getvalue())
        self.assertNotIn("nanocore.audit", output)

    def test_vv_enables_debug_for_the_whole_package(self):
        self.run_cli("-vv", "status")
        self.assertEqual(logging.getLogger("nanocore").level, logging.DEBUG)

    def test_repeated_runs_do_not_stack_handlers(self):
        self.run_cli("-v", "status")
        self.session = PedalSession(load_fixture())
        self.run_cli("-v", "status")
        self.assertEqual(len(self.nanocore_handlers()), 1)

    def test_library_modules_install_no_handlers(self):
        import importlib

        for name in ("device", "session", "backup_io", "ble_midi", "usb_midi", "controller"):
            importlib.import_module(f"nanocore_controller.{name}")
        cli.configure_logging(0)
        self.assertEqual(self.nanocore_handlers(), [])
        self.assertEqual(logging.getLogger("nanocore.audit").handlers, [])


class ServeTests(CliTestCase):
    def tearDown(self):
        sys.modules.pop("nanocore_controller.serve", None)

    def test_serve_forwards_the_rest_of_the_command_line(self):
        calls = []
        fake = types.ModuleType("nanocore_controller.serve")
        fake.main = lambda argv: calls.append(argv) or 0
        sys.modules["nanocore_controller.serve"] = fake
        code, output = self.run_cli("serve", "--port", "8800", "--no-autosave", "--read-only")
        self.assertEqual(code, 0)
        self.assertEqual(calls, [["--port", "8800", "--no-autosave", "--read-only"]])
        self.assertNothingOpened()

    def test_serve_passes_help_and_returns_the_server_exit_code(self):
        calls = []
        fake = types.ModuleType("nanocore_controller.serve")
        fake.main = lambda argv: calls.append(argv) or 3
        sys.modules["nanocore_controller.serve"] = fake
        code, output = self.run_cli("serve", "--help")
        self.assertEqual(code, 3)
        self.assertEqual(calls, [["--help"]])

    def test_serve_without_the_extra_explains_how_to_install_it(self):
        sys.modules["nanocore_controller.serve"] = None
        code, output = self.run_cli("serve")
        self.assertEqual(code, 2)
        self.assertEqual(
            output.strip(),
            "error: nanocore serve needs the 'serve' extra: "
            "pip install 'nanocore-controller[serve]'",
        )

    def test_serve_is_not_a_dry_run_command(self):
        fake = types.ModuleType("nanocore_controller.serve")
        fake.main = lambda argv: self.fail("serve must not start")
        sys.modules["nanocore_controller.serve"] = fake
        code, output = self.run_cli("--dry-run", "serve")
        self.assertEqual(code, 2)
        self.assertIn("--dry-run is not supported for serve", output)

    def test_connection_options_given_before_serve_are_forwarded_to_the_server(self):
        calls = []
        fake = types.ModuleType("nanocore_controller.serve")
        fake.main = lambda argv: calls.append(argv) or 0
        sys.modules["nanocore_controller.serve"] = fake
        code, _ = self.run_cli(
            "--transport", "usb", "--port", "hw:1,0,0", "--address", ADDRESS, "--adapter", "hci2", "serve", "--read-only"
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            calls,
            [["--transport", "usb", "--address", ADDRESS, "--adapter", "hci2", "--alsa-port", "hw:1,0,0", "--read-only"]],
        )

    def test_options_after_serve_come_later_and_win(self):
        calls = []
        fake = types.ModuleType("nanocore_controller.serve")
        fake.main = lambda argv: calls.append(argv) or 0
        sys.modules["nanocore_controller.serve"] = fake
        self.run_cli("--transport", "usb", "serve", "--transport", "bluetooth")
        self.assertEqual(calls, [["--transport", "usb", "--transport", "bluetooth"]])

    def test_a_config_file_does_not_apply_to_serve(self):
        fake = types.ModuleType("nanocore_controller.serve")
        fake.main = lambda argv: self.fail("serve must not start")
        sys.modules["nanocore_controller.serve"] = fake
        code, output = self.run_cli("--config", "/tmp/x.json", "serve")
        self.assertEqual(code, 2)
        self.assertIn("--config does not apply to serve", output)

    def test_the_word_serve_as_an_option_value_is_not_the_subcommand(self):
        fake = types.ModuleType("nanocore_controller.serve")
        fake.main = lambda argv: self.fail("serve must not run")
        sys.modules["nanocore_controller.serve"] = fake
        self.session.script(0x40, bytes([6, 1, 8, 0]) + b"Fixture ")
        code, output = self.run_cli("--config", "serve", "status")
        self.assertEqual(code, 0)
        self.assertIn('"preset"', output)

    def test_serve_options_that_look_like_global_ones_belong_to_the_server(self):
        calls = []
        fake = types.ModuleType("nanocore_controller.serve")
        fake.main = lambda argv: calls.append(argv) or 0
        sys.modules["nanocore_controller.serve"] = fake
        code, _ = self.run_cli("--transport", "usb", "serve", "--transport", "bluetooth", "--port", "9")
        self.assertEqual(code, 0)
        self.assertEqual(calls, [["--transport", "usb", "--transport", "bluetooth", "--port", "9"]])


class DispatchTests(unittest.TestCase):
    def subcommand_names(self):
        parser = build_parser()
        action = next(
            action for action in parser._actions if action.dest == "command"
        )
        return set(action.choices)

    def test_every_command_is_in_the_table_exactly_once(self):
        names = self.subcommand_names()
        self.assertEqual(names, set(COMMANDS) | set(cli.ALIASES))
        self.assertTrue(set(cli.ALIASES.values()) <= set(COMMANDS))
        self.assertFalse(set(cli.ALIASES) & set(COMMANDS))

    def test_transports_are_data_with_known_values(self):
        for name, spec in COMMANDS.items():
            with self.subTest(command=name):
                self.assertTrue(spec.transports)
                self.assertTrue(spec.transports <= {"bluetooth", "usb"})
                self.assertTrue(callable(spec.handler))

    def test_only_ble_capture_is_bluetooth_only(self):
        self.assertEqual(
            {name for name, spec in COMMANDS.items() if "usb" not in spec.transports},
            {"ble-capture"},
        )

    def test_commands_that_send_midi_have_a_payload_and_others_do_not(self):
        parser = build_parser()
        samples = {
            "preset": ["preset", "9"],
            "next": ["next"],
            "previous": ["previous"],
            "tuner": ["tuner", "on"],
            "block": ["block", "reverb", "on"],
            "type": ["type", "amp", "4"],
            "param": ["param", "amp_gain", "50%"],
        }
        for name, spec in COMMANDS.items():
            if spec.dry_run is DryRun.BYTES:
                with self.subTest(command=name):
                    self.assertIsNotNone(command_payload(parser.parse_args(samples[name])))
        self.assertEqual(
            {name for name, spec in COMMANDS.items() if spec.dry_run is DryRun.BYTES},
            set(samples),
        )

    def test_readme_transport_table_matches_the_table(self):
        rows = re.findall(
            r"^\| `([a-z-]+)` \| (yes|no) \| (yes|no) \|", README.read_text(), re.MULTILINE
        )
        documented = {name: ({"bluetooth"} if bt == "yes" else set())
                      | ({"usb"} if usb == "yes" else set())
                      for name, bt, usb in rows}
        self.assertEqual(len(rows), len(documented))
        self.assertEqual(
            documented, {name: set(spec.transports) for name, spec in COMMANDS.items()}
        )


if __name__ == "__main__":
    unittest.main()
