import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "contrib" / "midi-router" / "nanocore_midi_router.py"
spec = importlib.util.spec_from_file_location("nanocore_midi_router", SCRIPT)
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)

GT10 = {
    "output_channel": 1,
    "drop_bank_select": True,
    "program_change": {"max": 63},
    "cc": {
        "80": {"action": "toggle", "send": 20},
        "81": {"action": "momentary", "send": 21},
        "82": {"action": "pass"},
        "7": {"action": "map", "send": 60},
    },
}


def run(config, *chunks):
    t = router.Translator(config)
    out = []
    for chunk in chunks:
        out.extend(t.feed(bytes(chunk)))
    return [bytes(m) for m in out]


class ParserTests(unittest.TestCase):
    def test_splits_messages_and_handles_running_status(self):
        t = router.Translator({"cc": {"1": {"action": "pass"}, "2": {"action": "pass"}}})
        out = t.feed(bytes([0xB0, 1, 10, 2, 20, 1, 30]))
        self.assertEqual(out, [bytes([0xB0, 1, 10]), bytes([0xB0, 2, 20]), bytes([0xB0, 1, 30])])

    def test_message_split_across_reads(self):
        t = router.Translator({"cc": {"1": {"action": "pass"}}})
        self.assertEqual(t.feed(bytes([0xB0, 1])), [])
        self.assertEqual(t.feed(bytes([99])), [bytes([0xB0, 1, 99])])

    def test_real_time_bytes_inside_a_message_are_ignored(self):
        t = router.Translator({"cc": {"1": {"action": "pass"}}})
        self.assertEqual(t.feed(bytes([0xB0, 0xF8, 1, 0xFE, 99])), [bytes([0xB0, 1, 99])])

    def test_sysex_is_dropped(self):
        t = router.Translator({"cc": {"1": {"action": "pass"}}})
        self.assertEqual(t.feed(bytes([0xF0, 0x41, 0x10, 0xF7, 0xB0, 1, 5])), [bytes([0xB0, 1, 5])])

    def test_garbage_data_bytes_without_status_are_dropped(self):
        t = router.Translator({"cc": {"1": {"action": "pass"}}})
        self.assertEqual(t.feed(bytes([5, 6, 0xB0, 1, 7])), [bytes([0xB0, 1, 7])])


class ProgramChangeTests(unittest.TestCase):
    def test_program_change_is_forwarded_on_the_output_channel(self):
        self.assertEqual(run(GT10, [0xC5, 3]), [bytes([0xC0, 3])])

    def test_program_change_above_the_last_preset_is_dropped(self):
        self.assertEqual(run(GT10, [0xC0, 63]), [bytes([0xC0, 63])])
        self.assertEqual(run(GT10, [0xC0, 64]), [])

    def test_program_change_offset(self):
        cfg = {**GT10, "program_change": {"offset": 8, "max": 63}}
        self.assertEqual(run(cfg, [0xC0, 1]), [bytes([0xC0, 9])])

    def test_bank_select_is_dropped(self):
        out = run(GT10, [0xB0, 0, 0], [0xB0, 32, 1], [0xC0, 9])
        self.assertEqual(out, [bytes([0xC0, 9])])

    def test_bank_select_can_be_kept(self):
        cfg = {**GT10, "drop_bank_select": False, "cc": {"0": {"action": "pass"}}}
        self.assertEqual(run(cfg, [0xB0, 0, 1]), [bytes([0xB0, 0, 1])])

    def test_program_change_can_be_turned_off(self):
        cfg = {**GT10, "program_change": None}
        self.assertEqual(run(cfg, [0xC0, 3]), [])


class BankTests(unittest.TestCase):
    """What a GT-10 really sends for a patch change: CC 0, CC 32 and then the Program Change."""

    BANKS = {**GT10, "program_change": {"max": 63, "banks": {"0": 0, "3": -92}}}

    def patch(self, bank, program):
        return [[0xB0, 0, bank], [0xB0, 32, 0], [0xC0, program]]

    def test_bank_zero_patches_pass_through(self):
        self.assertEqual(run(GT10, *self.patch(0, 0)), [bytes([0xC0, 0])])
        self.assertEqual(run(GT10, *self.patch(0, 3)), [bytes([0xC0, 3])])

    def test_a_bank_that_is_not_listed_is_dropped(self):
        # the default is bank 0 only: bank 3 program 96 would be above the 64 presets anyway
        self.assertEqual(run(GT10, *self.patch(3, 96)), [])

    def test_banks_can_be_shifted_onto_presets(self):
        self.assertEqual(run(self.BANKS, *self.patch(3, 96)), [bytes([0xC0, 4])])
        self.assertEqual(run(self.BANKS, *self.patch(3, 99)), [bytes([0xC0, 7])])

    def test_the_bank_is_remembered_between_messages(self):
        out = run(self.BANKS, *self.patch(3, 96), *self.patch(0, 2))
        self.assertEqual(out, [bytes([0xC0, 4]), bytes([0xC0, 2])])

    def test_a_bank_select_never_reaches_the_pedal(self):
        out = run(self.BANKS, *self.patch(3, 97))
        self.assertTrue(all(m[0] & 0xF0 != 0xB0 for m in out))

    def test_a_shift_that_leaves_the_range_is_dropped(self):
        cfg = {**GT10, "program_change": {"max": 63, "banks": {"0": 0, "3": -100}}}
        self.assertEqual(run(cfg, *self.patch(3, 96)), [])


class AseqdumpTests(unittest.TestCase):
    def test_control_change(self):
        line = " 32:0   Control change          0, controller 80, value 127"
        self.assertEqual(router.parse_aseqdump_line(line), bytes([0xB0, 80, 127]))

    def test_channel_is_kept(self):
        line = " 32:0   Control change          5, controller 7, value 9"
        self.assertEqual(router.parse_aseqdump_line(line), bytes([0xB5, 7, 9]))

    def test_program_change(self):
        line = " 32:0   Program change          0, program 96"
        self.assertEqual(router.parse_aseqdump_line(line), bytes([0xC0, 96]))

    def test_other_lines_are_ignored(self):
        for line in ("Waiting for data. Press Ctrl+C to end.", "Source  Event                  Ch  Data", "",
                     " 32:0   Note on                 0, note 60, velocity 100", " 32:0   Clock"):
            with self.subTest(line=line):
                self.assertIsNone(router.parse_aseqdump_line(line))


class ControlChangeTests(unittest.TestCase):
    def test_pass_keeps_the_controller_and_value(self):
        self.assertEqual(run(GT10, [0xB0, 82, 127], [0xB0, 82, 0]), [bytes([0xB0, 82, 127]), bytes([0xB0, 82, 0])])

    def test_momentary_renumbers_and_follows_the_switch(self):
        self.assertEqual(run(GT10, [0xB0, 81, 127], [0xB0, 81, 0]), [bytes([0xB0, 21, 127]), bytes([0xB0, 21, 0])])

    def test_toggle_flips_on_each_press_and_ignores_the_release(self):
        out = run(GT10, [0xB0, 80, 127], [0xB0, 80, 0], [0xB0, 80, 127], [0xB0, 80, 0], [0xB0, 80, 127])
        self.assertEqual(out, [bytes([0xB0, 20, 127]), bytes([0xB0, 20, 0]), bytes([0xB0, 20, 127])])

    def test_toggle_can_start_on(self):
        cfg = {**GT10, "cc": {"80": {"action": "toggle", "send": 20, "initial": True}}}
        self.assertEqual(run(cfg, [0xB0, 80, 127]), [bytes([0xB0, 20, 0])])

    def test_map_sends_continuous_values(self):
        self.assertEqual(run(GT10, [0xB0, 7, 0], [0xB0, 7, 64], [0xB0, 7, 127]),
                         [bytes([0xB0, 60, 0]), bytes([0xB0, 60, 64]), bytes([0xB0, 60, 127])])

    def test_map_can_limit_the_range(self):
        cfg = {**GT10, "cc": {"7": {"action": "map", "send": 60, "min": 20, "max": 100}}}
        self.assertEqual(run(cfg, [0xB0, 7, 0], [0xB0, 7, 127]), [bytes([0xB0, 60, 20]), bytes([0xB0, 60, 100])])

    def test_map_does_not_repeat_a_value(self):
        out = run(GT10, [0xB0, 7, 50], [0xB0, 7, 50], [0xB0, 7, 51])
        self.assertEqual(out, [bytes([0xB0, 60, 50]), bytes([0xB0, 60, 51])])

    def test_unlisted_controllers_are_dropped(self):
        self.assertEqual(run(GT10, [0xB0, 11, 5], [0xE0, 0, 64], [0x90, 60, 100]), [])

    def test_other_input_channels_all_map_to_the_output_channel(self):
        self.assertEqual(run(GT10, [0xB7, 82, 127]), [bytes([0xB0, 82, 127])])

    def test_input_channel_filter(self):
        cfg = {**GT10, "input_channel": 3}
        self.assertEqual(run(cfg, [0xB0, 82, 127]), [])
        self.assertEqual(run(cfg, [0xB2, 82, 127]), [bytes([0xB0, 82, 127])])

    def test_output_channel_is_applied(self):
        cfg = {**GT10, "output_channel": 5}
        self.assertEqual(run(cfg, [0xB0, 82, 127], [0xC0, 2]), [bytes([0xB4, 82, 127]), bytes([0xC4, 2])])


class ConfigTests(unittest.TestCase):
    def test_rejects_bad_values(self):
        for bad in (
            {"output_channel": 0},
            {"output_channel": 17},
            {"cc": {"128": {"action": "pass"}}},
            {"cc": {"1": {"action": "explode"}}},
            {"cc": {"1": {"action": "map"}}},
            {"cc": {"1": {"action": "map", "send": 200}}},
            {"program_change": {"max": 200}},
            {"program_change": {"banks": {"128": 0}}},
            {"program_change": {"banks": {"0": "x"}}},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    router.Translator(bad)

    def test_example_config_is_valid(self):
        import json

        path = SCRIPT.parent / "gt10.example.json"
        router.Translator(json.loads(path.read_text()))


if __name__ == "__main__":
    unittest.main()
