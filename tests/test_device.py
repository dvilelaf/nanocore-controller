import asyncio
import logging
import unittest

from nanocore_controller.ble_midi import MidiEvent
from nanocore_controller.device import NanocoreDevice
from nanocore_controller.errors import ValidationError, VerificationError
from nanocore_controller.session import FakeSession
from support import runtime_payload

SET_LIVE = 0x6D
SELECT = 0x6D  # firmware 1.04 recalls presets through the live-field command
RUNTIME = 0x63


class DeviceTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = FakeSession()
        self.sleeps = []

        async def sleep(seconds):
            self.sleeps.append(seconds)

        self.device = NanocoreDevice(self.session, sleep=sleep)
        await self.device.connect()

    def sent_commands(self):
        return [command for command, _ in self.session.queries]


class ReadTest(DeviceTestCase):
    async def test_read_live_is_a_single_runtime_query(self):
        self.session.script(RUNTIME, runtime_payload(active_preset=5, volume=70))

        snapshot = await self.device.read_live()

        self.assertEqual((snapshot.active_preset, snapshot.preset_volume), (5, 70))
        self.assertEqual(self.session.queries, [(RUNTIME, b"")])


class SelectPresetTest(DeviceTestCase):
    async def test_recall_is_verified_by_reading_the_runtime_state_back(self):
        self.session.script(SELECT, b"")
        self.session.script(RUNTIME, runtime_payload(active_preset=8))

        snapshot = await self.device.select_preset(9)

        self.assertEqual(snapshot.active_preset, 8)
        self.assertEqual(self.session.queries[0], (SELECT, bytes.fromhex("09 08")))
        self.assertEqual(self.sent_commands(), [SELECT, RUNTIME])

    async def test_a_late_device_is_given_three_read_backs(self):
        self.session.script(SELECT, b"")
        self.session.script(
            RUNTIME,
            runtime_payload(active_preset=3),
            runtime_payload(active_preset=3),
            runtime_payload(active_preset=8),
        )

        snapshot = await self.device.select_preset(9)

        self.assertEqual(snapshot.active_preset, 8)
        self.assertEqual(self.sleeps, [0.05, 0.05])

    async def test_a_recall_that_never_lands_is_a_verification_error(self):
        self.session.script(SELECT, b"")
        self.session.script(RUNTIME, runtime_payload(active_preset=3))

        with self.assertRaisesRegex(VerificationError, "expected slot 8, got 3"):
            await self.device.select_preset(9)
        self.assertEqual(self.sent_commands().count(RUNTIME), 3)

    async def test_invalid_numbers_are_rejected_before_any_io(self):
        for value in (0, 129, -1, True, 1.0, "9", None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                await self.device.select_preset(value)
        self.assertEqual(self.session.queries, [])


class LiveWriteTest(DeviceTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.session.script(SET_LIVE, b"")

    async def assert_payload(self, expected_hex):
        self.assertEqual(self.session.queries, [(SET_LIVE, bytes.fromhex(expected_hex))])

    async def test_param(self):
        await self.device.set_param(2, 0, 0.5)
        await self.assert_payload("01 02 00 00 00 00 3f")

    async def test_enabled(self):
        await self.device.set_enabled(3, True)
        await self.assert_payload("03 03 01")

    async def test_volume(self):
        await self.device.set_volume(70)
        await self.assert_payload("04 46")

    async def test_chain_order(self):
        await self.device.set_chain_order(range(8))
        await self.assert_payload("05 08 00 01 02 03 04 05 06 07")

    async def test_amp_and_ir_slots(self):
        await self.device.select_amp(12)
        await self.device.select_ir(2)
        self.assertEqual(
            [payload.hex(" ") for _, payload in self.session.queries], ["06 0c", "07 02"]
        )

    async def test_every_live_write_leaves_an_audit_record(self):
        with self.assertLogs("nanocore.audit", logging.INFO) as logs:
            await self.device.set_volume(70)

        self.assertIn("volume 70", logs.output[0])

    async def test_send_midi_refuses_an_empty_message(self):
        with self.assertRaises(ValidationError):
            await self.device.send_midi(b"")
        await self.device.send_midi(b"\xb0\x50\x7f")
        self.assertEqual(self.session.sent, [b"\xb0\x50\x7f"])


class ConcurrencyTest(DeviceTestCase):
    async def test_operations_never_interleave_on_the_session(self):
        in_flight = 0
        peak = 0

        async def slow_query(command, payload=b"", *, sequence=None, timeout=5.0):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return await FakeSession.query(self.session, command, payload)

        self.session.query = slow_query
        self.session.script(SET_LIVE, b"")
        self.session.script(RUNTIME, runtime_payload())

        await asyncio.gather(
            self.device.set_volume(10),
            self.device.read_live(),
            self.device.set_volume(20),
            self.device.read_live(),
        )

        self.assertEqual(peak, 1)

    async def test_a_cancelled_caller_does_not_stop_a_write_halfway(self):
        gate = asyncio.Event()
        real_query = self.session.query

        async def gated_query(command, payload=b"", *, sequence=None, timeout=5.0):
            if command == SELECT:
                await gate.wait()
            return await real_query(command, payload)

        self.session.query = gated_query
        self.session.script(SELECT, b"")
        self.session.script(RUNTIME, runtime_payload(active_preset=8))

        caller = asyncio.create_task(self.device.select_preset(9))
        await asyncio.sleep(0.01)
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        gate.set()
        await asyncio.sleep(0.05)

        self.assertEqual(self.sent_commands(), [SELECT, RUNTIME])
        # The lock was released: the device is usable again.
        self.assertEqual((await self.device.read_live()).active_preset, 8)


class LifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_failed_connect_closes_the_session_and_keeps_the_original_error(self):
        session = FakeSession()
        session.fail_connect = RuntimeError("no adapter")

        with self.assertRaisesRegex(RuntimeError, "no adapter"):
            async with NanocoreDevice(session):
                self.fail("must not enter")
        self.assertEqual(session.close_count, 1)

    async def test_a_failing_close_never_hides_the_error_from_the_body(self):
        session = FakeSession()

        async def broken_close():
            raise OSError("link already gone")

        session.close = broken_close

        with self.assertRaisesRegex(ValueError, "from the body"):
            async with NanocoreDevice(session):
                raise ValueError("from the body")

    async def test_cancellation_inside_the_body_propagates(self):
        session = FakeSession()

        with self.assertRaises(asyncio.CancelledError):
            async with NanocoreDevice(session):
                raise asyncio.CancelledError
        self.assertEqual(session.close_count, 1)

    async def test_clean_exit_closes_once(self):
        session = FakeSession()

        async with NanocoreDevice(session) as device:
            self.assertTrue(device.connected)
        self.assertFalse(session.connected)
        self.assertEqual(session.close_count, 1)


class EventsTest(DeviceTestCase):
    async def test_device_originated_events_are_passed_through(self):
        event = MidiEvent(0, b"\xc0\x05")
        self.session.push_event(event)

        stream = self.device.events()
        self.assertEqual(await asyncio.wait_for(anext(stream), 1), event)
