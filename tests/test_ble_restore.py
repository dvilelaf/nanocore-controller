import copy
import json
import random
import unittest
from pathlib import Path

from nanocore_controller.ble_restore import (
    apply_restore,
    apply_restore_with_session,
    build_restore_plan,
    validate_backup,
)
from nanocore_controller.errors import NanocoreError, ValidationError
from nanocore_controller.nanocore_protocol import NanocoreResponse

FIXTURE = Path(__file__).parent / "fixtures" / "backup-v1.json"


class BleRestoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backup = json.loads(FIXTURE.read_text())

    def test_validates_real_backup(self):
        validate_backup(self.backup)

    def test_rejects_nonfinite_parameter(self):
        backup = copy.deepcopy(self.backup)
        backup["preset"]["effects"][0]["params"] = [float("nan")]
        with self.assertRaisesRegex(ValueError, "finite"):
            validate_backup(backup)

    def test_rejects_wrong_effect_order(self):
        backup = copy.deepcopy(self.backup)
        backup["preset"]["effects"][2]["index"] = 7
        with self.assertRaisesRegex(ValueError, "indices must be 0 through 7"):
            validate_backup(backup)

    def test_rejects_invalid_chain(self):
        backup = copy.deepcopy(self.backup)
        backup["preset"]["chain_order"] = [0] * 8
        with self.assertRaisesRegex(ValueError, "permutation"):
            validate_backup(backup)

    def test_rejects_json_that_disagrees_with_raw_snapshot(self):
        backup = copy.deepcopy(self.backup)
        backup["preset"]["effects"][0]["params"] = [0.5]
        with self.assertRaisesRegex(ValueError, "raw snapshot"):
            validate_backup(backup)

    def test_rejects_asset_slot_that_disagrees_with_raw_metadata(self):
        backup = copy.deepcopy(self.backup)
        backup["preset"]["amp_slot"] = 6
        with self.assertRaisesRegex(ValueError, "amp.*metadata"):
            validate_backup(backup)

    def test_rejects_non_object_root_and_device(self):
        with self.assertRaisesRegex(ValueError, "object"):
            validate_backup([])
        backup = copy.deepcopy(self.backup)
        backup["device"] = []
        with self.assertRaisesRegex(ValueError, "device.*object"):
            validate_backup(backup)

    def test_builds_lossless_set_field_plan(self):
        plan = build_restore_plan(self.backup)
        self.assertEqual(len(plan), 20)
        self.assertTrue(all(operation.command == 0x6D for operation in plan))
        self.assertEqual(plan[0].payload, bytes.fromhex("06 04"))
        self.assertEqual(plan[1].payload, bytes.fromhex("07 05"))
        self.assertEqual(plan[2].payload, bytes.fromhex("02 00 00 00"))
        self.assertEqual(plan[3].payload, bytes.fromhex("02 01 00 00"))
        self.assertEqual(plan[10].payload, bytes.fromhex("03 00 00"))
        self.assertEqual(plan[11].payload, bytes.fromhex("03 01 00"))
        self.assertEqual(plan[-2].payload, bytes.fromhex("04 54"))
        self.assertEqual(
            plan[-1].payload,
            bytes.fromhex("05 08 00 01 02 03 04 05 06 07"),
        )

    def test_applies_plan_and_reads_back_runtime_and_assets(self):
        backup = self.backup

        class Session:
            instances = []

            def __init__(self, address, *, adapter):
                self.calls = []
                self.instances.append(self)

            async def connect(self): pass
            async def close(self): pass

            async def query(self, command, payload=b""):
                self.calls.append((command, payload))
                if command == 0x63:
                    return NanocoreResponse(1, command, 0, bytes.fromhex(backup["raw_snapshot"]))
                if command == 0x36:
                    return NanocoreResponse(1, command, 0, bytes.fromhex(backup["assets"]["amp"]["raw"]))
                if command == 0x56:
                    return NanocoreResponse(1, command, 0, bytes.fromhex(backup["assets"]["ir"]["raw"]))
                return NanocoreResponse(1, command, 0, b"")

        result = __import__("asyncio").run(
            apply_restore(backup, "AA:BB:CC:DD:EE:FF", session_factory=Session)
        )
        self.assertEqual(result["operations_applied"], 20)
        self.assertEqual(len(Session.instances[0].calls), 23)

    def test_session_helper_preserves_validation_and_readback_without_closing(self):
        backup = self.backup

        class Session:
            def __init__(self):
                self.calls = []
                self.closed = False

            async def close(self):
                self.closed = True

            async def query(self, command, payload=b""):
                self.calls.append((command, payload))
                if command == 0x63:
                    raw = bytes.fromhex(backup["raw_snapshot"])
                elif command == 0x36:
                    raw = bytes.fromhex(backup["assets"]["amp"]["raw"])
                elif command == 0x56:
                    raw = bytes.fromhex(backup["assets"]["ir"]["raw"])
                else:
                    raw = b""
                return NanocoreResponse(1, command, 0, raw)

        session = Session()
        result = __import__("asyncio").run(apply_restore_with_session(backup, session))
        self.assertEqual(result, {"operations_applied": 20, "verified": True})
        self.assertEqual(len(session.calls), 23)
        self.assertFalse(session.closed)

    def test_apply_rejects_failed_readback(self):
        backup = self.backup

        class Session:
            def __init__(self, address, *, adapter): pass
            async def connect(self): pass
            async def close(self): pass
            async def query(self, command, payload=b""):
                if command == 0x63:
                    raw = bytearray.fromhex(backup["raw_snapshot"])
                    raw[2] = 1
                    return NanocoreResponse(1, command, 0, bytes(raw))
                return NanocoreResponse(1, command, 0, b"")

        with self.assertRaisesRegex(RuntimeError, "read-back verification"):
            __import__("asyncio").run(
                apply_restore(backup, "AA:BB:CC:DD:EE:FF", session_factory=Session)
            )

    def test_apply_reports_which_operation_failed(self):
        class Session:
            def __init__(self, address, *, adapter): pass
            async def connect(self): pass
            async def close(self): pass
            async def query(self, command, payload=b""):
                raise TimeoutError

        with self.assertRaisesRegex(RuntimeError, "operation 1.*amplifier"):
            __import__("asyncio").run(
                apply_restore(self.backup, "AA:BB:CC:DD:EE:FF", session_factory=Session)
            )

    def test_apply_retries_transient_write_timeout(self):
        backup = self.backup

        class Session:
            attempts = {}

            def __init__(self, address, *, adapter): pass
            async def connect(self): pass
            async def close(self): pass
            async def query(self, command, payload=b""):
                key = (command, payload)
                self.attempts[key] = self.attempts.get(key, 0) + 1
                if command == 0x6D and self.attempts[key] == 1:
                    raise TimeoutError
                if command == 0x63:
                    return NanocoreResponse(1, command, 0, bytes.fromhex(backup["raw_snapshot"]))
                if command == 0x36:
                    return NanocoreResponse(1, command, 0, bytes.fromhex(backup["assets"]["amp"]["raw"]))
                if command == 0x56:
                    return NanocoreResponse(1, command, 0, bytes.fromhex(backup["assets"]["ir"]["raw"]))
                return NanocoreResponse(1, command, 0, b"")

        result = __import__("asyncio").run(
            apply_restore(backup, "AA:BB:CC:DD:EE:FF", session_factory=Session)
        )
        self.assertTrue(result["verified"])
        self.assertTrue(all(attempts == 2 for key, attempts in Session.attempts.items() if key[0] == 0x6D))

    def test_apply_validates_before_creating_or_connecting_a_session(self):
        class Session:
            instances = []

            def __init__(self, address, *, adapter):
                self.instances.append(self)

            async def connect(self):
                raise AssertionError("invalid backups must not connect")

        with self.assertRaisesRegex(ValueError, "backup root"):
            __import__("asyncio").run(
                apply_restore([], "AA:BB:CC:DD:EE:FF", session_factory=Session)
            )
        self.assertEqual(Session.instances, [])


def _set(*path, value):
    def mutate(document):
        target = document
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


def _delete(*path):
    def mutate(document):
        target = document
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]

    return mutate


INVALID_BACKUP_MUTATIONS = {
    "version true": _set("version", value=True),
    "version float": _set("version", value=1.0),
    "format number": _set("format", value=5),
    "device missing": _delete("device"),
    "preset list": _set("preset", value=[]),
    "volume string": _set("preset", "preset_volume", value="84"),
    "volume bool": _set("preset", "preset_volume", value=True),
    "volume float": _set("preset", "preset_volume", value=84.0),
    "volume range": _set("preset", "preset_volume", value=101),
    "slot missing": _delete("preset", "slot"),
    "slot string": _set("preset", "slot", value="8"),
    "slot float": _set("preset", "slot", value=8.0),
    "slot range": _set("preset", "slot", value=128),
    "amp slot string": _set("preset", "amp_slot", value="4"),
    "amp slot range": _set("preset", "amp_slot", value=30),
    "ir slot float": _set("preset", "ir_slot", value=5.0),
    "chain string entry": _set("preset", "chain_order", value=[0, 1, 2, 3, 4, 5, 6, "x"]),
    "chain none entry": _set("preset", "chain_order", value=[0, 1, 2, 3, 4, 5, 6, None]),
    "chain float entry": _set("preset", "chain_order", value=[0.0, 1, 2, 3, 4, 5, 6, 7]),
    "chain bool entry": _set("preset", "chain_order", value=[False, True, 2, 3, 4, 5, 6, 7]),
    "chain nested entry": _set("preset", "chain_order", value=[[0], 1, 2, 3, 4, 5, 6, 7]),
    "chain tuple": _set("preset", "chain_order", value="01234567"),
    "effects dict": _set("preset", "effects", value={}),
    "effect not object": _set("preset", "effects", 0, value=5),
    "effect index float": _set("preset", "effects", 0, "index", value=0.0),
    "effect index bool": _set("preset", "effects", 1, "index", value=True),
    "effect enabled int": _set("preset", "effects", 0, "enabled", value=0),
    "effect variant string": _set("preset", "effects", 0, "variant", value="0"),
    "effect variant range": _set("preset", "effects", 0, "variant", value=256),
    "effect variant float": _set("preset", "effects", 0, "variant", value=0.0),
    "params not list": _set("preset", "effects", 0, "params", value="ab"),
    "params tuple string": _set("preset", "effects", 0, "params", value=["0.5"]),
    "params none": _set("preset", "effects", 0, "params", value=[None]),
    "params bool": _set("preset", "effects", 0, "params", value=[True]),
    "params inf": _set("preset", "effects", 0, "params", value=[float("inf")]),
    "params huge int": _set("preset", "effects", 0, "params", value=[10**400]),
    "params range": _set("preset", "effects", 0, "params", value=[1.5]),
    "params too many": _set("preset", "effects", 0, "params", value=[0.5] * 25),
    "raw missing": _delete("raw_snapshot"),
    "raw number": _set("raw_snapshot", value=5),
    "raw not hex": _set("raw_snapshot", value="zz"),
    "raw odd hex": _set("raw_snapshot", value="030"),
    "raw non ascii": _set("raw_snapshot", value="\u00e9\u00e9"),
    "raw empty": _set("raw_snapshot", value=""),
    "assets list": _set("assets", value=[]),
    "asset amp missing": _delete("assets", "amp"),
    "asset amp string": _set("assets", "amp", value="x"),
    "asset raw number": _set("assets", "ir", "raw", value=1),
    "asset raw not hex": _set("assets", "ir", "raw", value="not hex"),
    "asset raw short": _set("assets", "ir", "raw", value="05 01 01"),
    "asset slot float": _set("assets", "amp", "slot", value=4.0),
    "asset slot string": _set("assets", "amp", "slot", value="4"),
}


class ValidateBackupHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backup = json.loads(FIXTURE.read_text())

    def test_rejects_every_malformed_field_with_validation_error(self):
        for name, mutate in INVALID_BACKUP_MUTATIONS.items():
            backup = copy.deepcopy(self.backup)
            mutate(backup)
            with self.subTest(name), self.assertRaises(ValidationError):
                validate_backup(backup)

    def test_rejects_non_mapping_roots_with_validation_error(self):
        for root in ([], "x", None, 5, 1.5):
            with self.subTest(root=root), self.assertRaises(ValidationError):
                validate_backup(root)

    def test_extra_keys_are_ignored(self):
        backup = copy.deepcopy(self.backup)
        backup["extra"] = {"anything": [1, 2, 3]}
        backup["preset"]["note"] = "hello"
        validate_backup(backup)


FUZZ_VALUES = (
    None, True, False, 0, 1, -1, 7, 8, 29, 30, 100, 101, 255, 256, 10**400, 0.0, 0.5, 1.0, -0.5, 1.5,
    float("nan"), float("inf"), float("-inf"), "", "x", "03 08", "zz", "\u00e9", [], [0], [[]], {}, {"a": 1},
    list(range(8)), [0.5] * 30,
)


def _paths(node, prefix=()):
    yield prefix
    if isinstance(node, dict):
        for key, child in node.items():
            yield from _paths(child, prefix + (key,))
    elif isinstance(node, list):
        for index, child in enumerate(node):
            yield from _paths(child, prefix + (index,))


class ValidateBackupFuzzTests(unittest.TestCase):
    def test_mutated_documents_raise_only_nanocore_errors(self):
        original = json.loads(FIXTURE.read_text())
        rng = random.Random(20261007)
        for _ in range(1500):
            document = copy.deepcopy(original)
            for _ in range(rng.randint(1, 3)):
                path = rng.choice([path for path in _paths(document) if path])
                parent = document
                for key in path[:-1]:
                    parent = parent[key]
                if isinstance(parent, dict) and rng.random() < 0.2:
                    del parent[path[-1]]
                else:
                    parent[path[-1]] = copy.deepcopy(rng.choice(FUZZ_VALUES))
            try:
                validate_backup(document)
            except NanocoreError:
                pass
            except Exception as exc:  # noqa: BLE001
                self.fail(f"{type(exc).__name__}: {exc} for {document!r}")


if __name__ == "__main__":
    unittest.main()
