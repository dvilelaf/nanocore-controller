import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location("build_mapping", ROOT / "scripts" / "build_mapping.py")
build_mapping = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_mapping)

BLOCK_IDS = ("fx1", "fx2", "amp", "cab", "del", "mod", "rev", "eq")


def catalog():
    return {
        "blocks": [
            {"id": block, "types": [{"id": 0, "name": f"{block}-type"}, {"id": 1, "name": f"{block}-two"}]}
            for block in BLOCK_IDS
        ]
    }


def change(effect, index, new=0.5):
    return {"effect": effect, "field": "param", "index": index, "old": 0.1, "new": new}


def record(runtime_index, ccs, count, variant=0):
    cc_map = {str(cc): [{"value": 25, "changes": [change(runtime_index, index)]}] for cc, index in ccs.items()}
    return {"name": "x", "variant": variant, "param_count": count, "cc_map": cc_map}


def measurements(**overrides):
    blocks = {}
    for index, block in enumerate(BLOCK_IDS):
        blocks[block] = {"runtime_index": index, "types": {"0": record(index, {60 + index: 0}, 2)}}
    blocks.update(overrides)
    return {
        "blocks": blocks,
        "chain_order": {"original": [0, 1], "sent": [1, 0], "read_back": [1, 0], "restored": [0, 1]},
        "captured_at": "2026-10-08T00:00:00+00:00",
    }


class BuildMappingTest(unittest.TestCase):
    def test_lists_the_controller_of_each_snapshot_parameter(self):
        result = build_mapping.build(measurements(), catalog())

        self.assertEqual(result["runtime_blocks"], list(BLOCK_IDS))
        params = result["blocks"]["fx2"]["types"]["0"]["params"]
        self.assertEqual(params, [{"index": 0, "cc": 61}, {"index": 1, "cc": None}])

    def test_blocks_with_a_fixed_type_controller_apply_to_every_type(self):
        result = build_mapping.build(measurements(), catalog())

        self.assertEqual(list(result["blocks"]["amp"]["types"]), ["*"])
        self.assertEqual(list(result["blocks"]["cab"]["types"]), ["*"])
        self.assertEqual(list(result["blocks"]["eq"]["types"]), ["0"])

    def test_the_variant_is_the_one_the_pedal_reported(self):
        blocks = {"rev": {"runtime_index": 6, "types": {"1": record(6, {85: 0}, 1, variant=5)}}}

        result = build_mapping.build(measurements(**blocks), catalog())

        self.assertEqual(result["blocks"]["rev"]["types"]["1"]["variant"], 5)

    def test_two_controllers_on_one_parameter_are_rejected(self):
        bad = record(5, {70: 0, 71: 0}, 2)
        with self.assertRaisesRegex(build_mapping.MappingError, "same parameter"):
            build_mapping.build(measurements(mod={"runtime_index": 5, "types": {"0": bad}}), catalog())

    def test_a_block_that_toggled_nothing_is_rejected(self):
        with self.assertRaisesRegex(build_mapping.MappingError, "changed no snapshot entry"):
            build_mapping.build(measurements(eq={"runtime_index": None, "types": {}}), catalog())

    def test_two_blocks_on_one_snapshot_entry_are_rejected(self):
        with self.assertRaisesRegex(build_mapping.MappingError, "share snapshot entry"):
            build_mapping.build(measurements(eq={"runtime_index": 0, "types": {"0": record(0, {}, 1)}}), catalog())

    def test_effects_changed_by_a_shared_controller_are_not_attributed_to_the_block(self):
        shared = record(1, {68: 0}, 3)  # fx2 probe of CC68 that moved snapshot entry 5 (modulation)
        shared["cc_map"]["68"][0]["changes"] = [change(5, 0)]
        result = build_mapping.build(measurements(fx2={"runtime_index": 1, "types": {"0": shared}}), catalog())

        self.assertEqual([p["cc"] for p in result["blocks"]["fx2"]["types"]["0"]["params"]], [None] * 3)

    def test_a_chain_order_that_was_not_read_back_is_rejected(self):
        data = measurements()
        data["chain_order"]["read_back"] = [0, 1]
        with self.assertRaisesRegex(build_mapping.MappingError, "not read back"):
            build_mapping.build(data, catalog())

    def test_the_published_tables_are_what_the_measurements_produce(self):
        calibration = ROOT / "docs" / "calibration"
        expected = build_mapping.build(
            json.loads((calibration / "measurements.json").read_text()),
            json.loads((calibration / "editor-catalog.json").read_text()),
        )

        self.assertEqual(json.loads((calibration / "mapping.json").read_text()), expected)
        self.assertEqual(
            json.loads((ROOT / "web" / "src" / "data" / "measuredMapping.json").read_text()), expected
        )

    def test_every_block_of_the_real_pedal_has_a_snapshot_entry_and_known_variants(self):
        table = json.loads((ROOT / "docs" / "calibration" / "mapping.json").read_text())

        self.assertEqual(table["runtime_blocks"], ["fx1", "fx2", "amp", "cab", "del", "mod", "rev", "eq"])
        reverb = {key: t["variant"] for key, t in table["blocks"]["rev"]["types"].items()}
        self.assertEqual(reverb, {"0": 0, "1": 1, "2": 2, "3": 3, "4": 5, "5": 6, "6": 7})
