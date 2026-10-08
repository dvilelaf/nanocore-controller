import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nanocore_controller import baselines
from nanocore_controller.baselines import BaselineStore, default_baseline_root
from nanocore_controller.errors import ValidationError
from support import load_fixture


class BaselineStoreTest(unittest.TestCase):
    def setUp(self):
        self.document = load_fixture()
        self.root = Path(tempfile.mkdtemp()) / "baselines"
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root.parent, ignore_errors=True))
        self.store = BaselineStore(self.root, keep=3)

    def test_records_a_validated_private_copy_under_the_slot_directory(self):
        entry = self.store.record(self.document)

        self.assertEqual((entry.slot, entry.name), (8, "Fixture"))
        self.assertEqual(entry.path.parent, self.root / "slot-008")
        self.assertEqual(entry.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(entry.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.store.load(8, entry.id), self.document)

    def test_lists_newest_first_and_keeps_only_the_newest_entries(self):
        ids = [self.store.record(self.document).id for _ in range(5)]

        listed = [entry.id for entry in self.store.list(8)]

        self.assertEqual(listed, sorted(ids, reverse=True)[:3])
        self.assertEqual(len(list((self.root / "slot-008").glob("*.json"))), 3)

    def test_two_records_in_the_same_instant_get_distinct_names(self):
        class FixedClock:
            @staticmethod
            def now(tz=None):
                import datetime as dt

                return dt.datetime(2026, 10, 7, 20, 0, 0, 123456, tzinfo=dt.UTC)

        with mock.patch.object(baselines, "datetime", FixedClock):
            first = self.store.record(self.document)
            second = self.store.record(self.document)

        self.assertNotEqual(first.id, second.id)
        self.assertTrue(second.id.endswith("-1"))

    def test_other_slots_are_independent(self):
        other = copy.deepcopy(self.document)
        raw = bytearray.fromhex(other["raw_snapshot"])
        raw[1] = 2
        other["raw_snapshot"] = raw.hex(" ")
        other["preset"]["slot"] = 2
        other["preset"]["display_number"] = 3

        self.store.record(self.document)
        entry = self.store.record(other)

        self.assertEqual(entry.path.parent, self.root / "slot-002")
        self.assertEqual(len(self.store.list(2)), 1)
        self.assertEqual(len(self.store.list(8)), 1)

    def test_a_document_that_could_not_be_restored_is_not_recorded(self):
        broken = copy.deepcopy(self.document)
        broken["preset"]["chain_order"] = [0, 1, 2, 3, 4, 5, 6, 7, 7]

        with self.assertRaises(ValidationError):
            self.store.record(broken)
        self.assertFalse(self.root.exists())

    def test_an_unknown_slot_lists_as_empty(self):
        self.assertEqual(self.store.list(40), [])

    def test_ids_that_could_escape_the_directory_are_refused(self):
        for bad in ("../../etc/passwd", "../slot-009/x", "", "20261007T200000123456Z/../x", None, 5):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                self.store.load(8, bad)

    def test_slots_must_be_valid_integers(self):
        for bad in (-1, 128, True, "8", None):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                self.store.list(bad)

    def test_a_document_without_a_slot_is_refused(self):
        with self.assertRaises(ValidationError):
            self.store.record({"preset": {}})
        with self.assertRaises(ValidationError):
            self.store.record({})

    def test_corrupt_files_are_skipped_when_listing(self):
        entry = self.store.record(self.document)
        (entry.path.parent / "20261007T200000000001Z.json").write_text("{not json")

        self.assertEqual([item.id for item in self.store.list(8)], [entry.id])

    def test_keep_must_be_a_positive_integer(self):
        for bad in (0, -1, True, 2.5):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                BaselineStore(self.root, keep=bad)

    def test_default_root_follows_xdg_data_home_only_when_absolute(self):
        with mock.patch.dict("os.environ", {"XDG_DATA_HOME": "/data/x"}):
            self.assertEqual(
                default_baseline_root(), Path("/data/x/nanocore-controller/baselines")
            )
        with mock.patch.dict("os.environ", {"XDG_DATA_HOME": "relative/path"}):
            self.assertEqual(
                default_baseline_root(),
                Path.home() / ".local/share/nanocore-controller/baselines",
            )
