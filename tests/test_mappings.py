import unittest

from nanocore_controller.mappings import (
    BLOCKS,
    parameter_cc,
    percent_to_midi,
    resolve_type,
    validate_parameter,
)


class MappingTests(unittest.TestCase):
    def test_reverb_block_uses_documented_ccs(self):
        self.assertEqual(BLOCKS["reverb"].on_cc, 27)
        self.assertEqual(BLOCKS["reverb"].type_cc, 47)

    def test_amp_and_cab_direct_parameters(self):
        self.assertEqual(parameter_cc("amp_gain"), 60)
        self.assertEqual(parameter_cc("cab_level"), 67)

    def test_named_reverb_type_resolves_to_shimmer_id(self):
        self.assertEqual(resolve_type("reverb", "shimmer"), 4)

    def test_decimal_string_type_resolves_before_named_lookup(self):
        self.assertEqual(resolve_type("amp", "4"), 4)
        self.assertEqual(resolve_type("cab", "127"), 127)

    def test_decimal_string_type_rejects_values_outside_midi_range(self):
        with self.assertRaisesRegex(ValueError, "0 to 127"):
            resolve_type("amp", "128")
        with self.assertRaisesRegex(ValueError, "0 to 127"):
            resolve_type("cab", "-1")

    def test_percent_maps_to_midi_seven_bit_value(self):
        self.assertEqual(percent_to_midi(50), 64)

    def test_parameter_compatibility_is_checked(self):
        self.assertTrue(validate_parameter("amp_gain", "amp"))
        self.assertFalse(validate_parameter("amp_gain", "reverb"))

    def test_unknown_type_is_rejected(self):
        with self.assertRaises(ValueError):
            resolve_type("reverb", "not-a-reverb")


if __name__ == "__main__":
    unittest.main()
