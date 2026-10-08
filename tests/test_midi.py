import unittest

from nanocore_controller.midi import control_change, program_change


class MidiEncodingTests(unittest.TestCase):
    def test_cc_on_midi_channel_one(self):
        self.assertEqual(control_change(1, 80, 127), bytes.fromhex("b0 50 7f"))

    def test_cc_on_midi_channel_sixteen(self):
        self.assertEqual(control_change(16, 20, 64), bytes.fromhex("bf 14 40"))

    def test_program_change_on_channel_two(self):
        self.assertEqual(program_change(2, 12), bytes.fromhex("c1 0c"))

    def test_cc_rejects_invalid_fields(self):
        for args in ((0, 80, 127), (17, 80, 127), (1, -1, 127), (1, 128, 127), (1, 80, -1), (1, 80, 128)):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    control_change(*args)

    def test_program_change_rejects_invalid_fields(self):
        for args in ((0, 12), (17, 12), (1, -1), (1, 128)):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    program_change(*args)


if __name__ == "__main__":
    unittest.main()
