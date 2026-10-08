import importlib.util
import unittest
from pathlib import Path

from nanocore_controller.nanocore_protocol import EffectSnapshot, RuntimeSnapshot

SCRIPT = Path(__file__).parent.parent / "scripts" / "calibrate_mapping.py"
spec = importlib.util.spec_from_file_location("calibrate_mapping", SCRIPT)
calibrate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(calibrate)


def snapshot(*effects):
    return RuntimeSnapshot(3, 6, 80, tuple(effects), (0, 1, 2, 3, 4, 5, 6, 7))


def effect(enabled=True, variant=0, params=(0.5, 0.5)):
    return EffectSnapshot(enabled, variant, tuple(params))


class DiffSnapshotsTest(unittest.TestCase):
    def base(self):
        return snapshot(*[effect() for _ in range(8)])

    def test_identical_snapshots_have_no_changes(self):
        self.assertEqual(calibrate.diff_snapshots(self.base(), self.base()), [])

    def test_reports_the_effect_and_parameter_that_moved(self):
        after = snapshot(*[effect() for _ in range(5)], effect(params=(0.5, 0.9)), effect(), effect())
        changes = calibrate.diff_snapshots(self.base(), after)
        self.assertEqual([(c.effect, c.field, c.index) for c in changes], [(5, "param", 1)])
        self.assertEqual((changes[0].old, changes[0].new), (0.5, 0.9))

    def test_reports_enabled_and_variant_changes(self):
        after = snapshot(effect(enabled=False), effect(variant=3), *[effect() for _ in range(6)])
        fields = [(c.effect, c.field) for c in calibrate.diff_snapshots(self.base(), after)]
        self.assertEqual(fields, [(0, "enabled"), (1, "variant")])

    def test_a_change_in_the_number_of_parameters_is_reported_once(self):
        after = snapshot(effect(params=(0.5, 0.5, 0.5)), *[effect() for _ in range(7)])
        changes = calibrate.diff_snapshots(self.base(), after)
        self.assertEqual([(c.field, c.old, c.new) for c in changes], [("param_count", 2, 3)])

    def test_tiny_float_noise_is_ignored(self):
        after = snapshot(effect(params=(0.5 + 1e-9, 0.5)), *[effect() for _ in range(7)])
        self.assertEqual(calibrate.diff_snapshots(self.base(), after), [])

    def test_probes_never_include_the_tuner_or_preset_stepping_controllers(self):
        probed = {cc for ccs in calibrate.PROBE_RANGES.values() for cc in ccs}
        self.assertFalse(probed & {80, 81, 82})
        self.assertTrue(probed <= set(range(128)))
