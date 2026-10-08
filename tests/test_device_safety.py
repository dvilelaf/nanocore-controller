import asyncio
import copy
import logging
import tempfile
import unittest
from pathlib import Path

from nanocore_controller.device import NanocoreDevice
from nanocore_controller.errors import (
    BackupError,
    DeviceStatusError,
    DeviceTimeout,
    PartialApplyError,
    SlotChangedError,
    ValidationError,
    VerificationError,
)
from support import PedalSession, load_fixture, wait_until

SAVE = 0x46
RUNTIME = 0x63


class SafetyTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.document = load_fixture()
        self.session = PedalSession(self.document)
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        self.device = NanocoreDevice(self.session, sleep=self._no_sleep)
        await self.device.connect()
        self.backup = self.directory / "before.json"

    async def _no_sleep(self, seconds):
        await asyncio.sleep(0)

    def _cleanup(self):
        for child in self.directory.glob("*"):
            child.unlink()
        self.directory.rmdir()


class BackupActiveTest(SafetyTestCase):
    async def test_writes_a_validated_private_file_and_returns_the_document(self):
        document = await self.device.backup_active(self.backup)

        self.assertEqual(document["preset"]["slot"], 8)
        self.assertEqual(self.backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(document["raw_snapshot"], self.document["raw_snapshot"])

    async def test_never_overwrites_an_existing_file(self):
        self.backup.write_text("precious")

        with self.assertRaises(BackupError):
            await self.device.backup_active(self.backup)
        self.assertEqual(self.backup.read_text(), "precious")


class SaveActiveTest(SafetyTestCase):
    async def test_the_backup_exists_and_is_valid_before_the_flash_write(self):
        seen = {}

        def at_save(session):
            seen["backup_exists"] = self.backup.exists()

        self.session.before_command[SAVE] = at_save

        snapshot = await self.device.save_active(self.backup)

        self.assertTrue(seen["backup_exists"])
        self.assertEqual(self.session.saved_slots, [8])
        self.assertEqual(snapshot.active_preset, 8)

    async def test_a_preset_switch_after_the_backup_aborts_before_any_write(self):
        def switch_once_backed_up(session):
            if self.backup.exists():
                session.switch_to_slot(3)

        self.session.before_command[RUNTIME] = switch_once_backed_up

        with self.assertRaises(SlotChangedError) as caught:
            await self.device.save_active(self.backup)

        self.assertEqual((caught.exception.expected_slot, caught.exception.actual_slot), (8, 3))
        self.assertEqual(self.session.saved_slots, [])
        self.assertNotIn(SAVE, self.session.log)

    async def test_an_existing_backup_path_aborts_before_any_write(self):
        self.backup.write_text("precious")

        with self.assertRaises(BackupError):
            await self.device.save_active(self.backup)

        self.assertEqual(self.session.saved_slots, [])
        self.assertEqual(self.backup.read_text(), "precious")

    async def test_a_state_that_keeps_changing_is_never_saved(self):
        counter = {"n": 0}

        def knob(session):
            counter["n"] += 1
            session.runtime[2] = counter["n"] % 100

        self.session.before_command[RUNTIME] = knob

        with self.assertRaises(VerificationError):
            await self.device.save_active(self.backup)

        self.assertEqual(self.session.saved_slots, [])
        self.assertFalse(self.backup.exists())

    async def test_a_state_change_during_the_write_is_reported(self):
        def knob(session):
            session.runtime[2] = 12

        self.session.before_command[SAVE] = knob

        with self.assertRaisesRegex(VerificationError, "changed while it was being saved"):
            await self.device.save_active(self.backup)

    async def test_a_save_timeout_says_the_write_may_have_been_applied(self):
        self.session.save_error = DeviceTimeout(SAVE, maybe_applied=True)

        with self.assertRaises(DeviceTimeout) as caught:
            await self.device.save_active(self.backup)

        self.assertTrue(caught.exception.maybe_applied)

    async def test_the_flash_write_is_audited(self):
        with self.assertLogs("nanocore.audit", logging.INFO) as logs:
            await self.device.save_active(self.backup)

        text = "\n".join(logs.output)
        self.assertIn("FLASH WRITE slot=8", text)
        self.assertIn("FLASH WRITE verified", text)

    async def test_a_cancelled_caller_does_not_interrupt_the_save(self):
        gate = asyncio.Event()
        real_query = self.session.query

        async def gated(command, payload=b"", **kwargs):
            if command == SAVE:
                await gate.wait()
            return await real_query(command, payload, **kwargs)

        self.session.query = gated
        caller = asyncio.create_task(self.device.save_active(self.backup))
        await asyncio.sleep(0.05)
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        gate.set()
        await wait_until(lambda: self.session.saved_slots == [8])

        self.assertEqual(self.session.saved_slots, [8])
        self.assertEqual((await self.device.read_live()).active_preset, 8)


class RestoreActiveTest(SafetyTestCase):
    async def test_applies_the_plan_after_writing_the_safety_backup(self):
        seen = {}

        def at_first_write(session):
            seen.setdefault("backup_exists", self.backup.exists())

        self.session.before_command[0x6D] = at_first_write

        result = await self.device.restore_active(self.document, self.backup)

        self.assertEqual(result, {"operations_applied": 20, "verified": True})
        self.assertTrue(seen["backup_exists"])
        self.assertEqual(len(self.session.live_writes), 20)

    async def test_an_invalid_document_is_rejected_before_any_io(self):
        broken = copy.deepcopy(self.document)
        broken["preset"]["chain_order"] = [0, 1, 2, 3, 4, 5, 6, "x"]

        with self.assertRaises(ValidationError):
            await self.device.restore_active(broken, self.backup)

        self.assertEqual(self.session.queries, [])
        self.assertFalse(self.backup.exists())

    async def test_a_backup_for_another_slot_is_refused_without_writing(self):
        self.session.switch_to_slot(3)

        with self.assertRaisesRegex(ValidationError, "slot 9, but that slot is not active"):
            await self.device.restore_active(self.document, self.backup)

        self.assertEqual(self.session.live_writes, [])
        self.assertFalse(self.backup.exists())

    async def test_a_preset_switch_after_the_safety_backup_aborts_before_writing(self):
        def switch_once_backed_up(session):
            if self.backup.exists():
                session.switch_to_slot(3)

        self.session.before_command[RUNTIME] = switch_once_backed_up

        with self.assertRaises(SlotChangedError):
            await self.device.restore_active(self.document, self.backup)

        self.assertEqual(self.session.live_writes, [])

    async def test_a_link_lost_midway_reports_how_far_it_got_and_that_rollback_failed(self):
        self.session.fail_all_live_writes_from = 7

        with self.assertRaises(PartialApplyError) as caught:
            await self.device.restore_active(self.document, self.backup)

        error = caught.exception
        self.assertEqual((error.applied, error.total), (6, 20))
        self.assertEqual(error.backup_path, self.backup)
        self.assertIn("operation 7 failed", str(error))
        self.assertTrue(any("also failed" in note for note in error.__notes__))

    async def test_a_transient_failure_is_followed_by_a_rollback_from_the_safety_backup(self):
        self.session.fail_live_write = {7: DeviceStatusError(0x6D, 0x03)}

        with self.assertRaises(PartialApplyError) as caught:
            await self.device.restore_active(self.document, self.backup)

        error = caught.exception
        self.assertEqual(error.applied, 6)
        self.assertTrue(any("put back" in note for note in error.__notes__))
        # 7 writes before the failure, then the whole 20-operation rollback plan.
        self.assertEqual(len(self.session.live_writes), 7 + 20)

    async def test_a_cancelled_caller_does_not_interrupt_a_restore(self):
        gate = asyncio.Event()
        real_query = self.session.query

        async def gated(command, payload=b"", **kwargs):
            if command == 0x6D and not gate.is_set():
                await gate.wait()
            return await real_query(command, payload, **kwargs)

        self.session.query = gated
        caller = asyncio.create_task(self.device.restore_active(self.document, self.backup))
        await asyncio.sleep(0.05)
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        gate.set()
        await wait_until(lambda: len(self.session.live_writes) == 20)

        self.assertEqual(len(self.session.live_writes), 20)
        self.assertEqual((await self.device.read_live()).active_preset, 8)


class RestoreVerificationRollbackTest(SafetyTestCase):
    def mismatching_once_writes_are_done(self, undo_afterwards):
        calls = {"after_writes": 0}

        def hook(session):
            if len(session.live_writes) >= 20:
                calls["after_writes"] += 1
                if calls["after_writes"] == 1:
                    session.runtime[2] = 1  # the read-back does not match what was written
                elif undo_afterwards:
                    session.runtime[:] = bytearray.fromhex(self.document["raw_snapshot"])

        self.session.before_command[RUNTIME] = hook

    async def test_a_mismatching_read_back_is_rolled_back_and_says_so(self):
        self.mismatching_once_writes_are_done(undo_afterwards=True)

        with self.assertRaises(VerificationError) as caught:
            await self.device.restore_active(self.document, self.backup)

        self.assertIs(caught.exception.rolled_back, True)
        self.assertGreaterEqual(len(self.session.live_writes), 40)  # the plan, then the rollback plan
        self.assertTrue(any("put back" in note for note in caught.exception.__notes__))

    async def test_a_rollback_that_does_not_verify_is_reported_as_failed(self):
        self.mismatching_once_writes_are_done(undo_afterwards=False)

        with self.assertRaises(VerificationError) as caught:
            await self.device.restore_active(self.document, self.backup)

        self.assertIs(caught.exception.rolled_back, False)
        self.assertTrue(any("also failed" in note for note in caught.exception.__notes__))
