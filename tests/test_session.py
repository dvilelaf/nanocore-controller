import asyncio
import unittest

from nanocore_controller.ble_midi import MidiEvent
from nanocore_controller.errors import DeviceDisconnected, DeviceStatusError
from nanocore_controller.session import FakeSession, SysexSession


class FakeSessionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = FakeSession()
        await self.session.connect()

    async def test_satisfies_the_session_protocol(self):
        self.assertIsInstance(self.session, SysexSession)

    async def test_connect_and_close_are_idempotent_and_counted(self):
        await self.session.connect()
        self.assertTrue(self.session.connected)
        await self.session.close()
        await self.session.close()
        self.assertFalse(self.session.connected)
        self.assertEqual(self.session.close_count, 2)

    async def test_query_returns_scripted_payloads_in_order_and_repeats_the_last(self):
        self.session.script(0x63, b"\x01", b"\x02")

        payloads = [(await self.session.query(0x63)).payload for _ in range(3)]

        self.assertEqual(payloads, [b"\x01", b"\x02", b"\x02"])
        self.assertEqual(self.session.queries, [(0x63, b"")] * 3)

    async def test_scripted_exception_is_raised(self):
        self.session.script(0x46, DeviceStatusError(0x46, 0x03))

        with self.assertRaises(DeviceStatusError):
            await self.session.query(0x46, b"\x08")

    async def test_callable_result_receives_the_request_payload(self):
        self.session.script(0x36, lambda payload: bytes([payload[0] + 1]))

        self.assertEqual((await self.session.query(0x36, b"\x04")).payload, b"\x05")

    async def test_unscripted_command_fails_loudly(self):
        with self.assertRaisesRegex(AssertionError, "0x77"):
            await self.session.query(0x77)

    async def test_disconnected_session_refuses_to_talk(self):
        await self.session.close()

        with self.assertRaises(DeviceDisconnected):
            await self.session.send(b"\xb0\x50\x7f")
        with self.assertRaises(DeviceDisconnected):
            await self.session.query(0x63)

    async def test_events_stream_ends_when_the_session_closes(self):
        event = MidiEvent(0, b"\xb0\x50\x7f")
        self.session.push_event(event)

        async def collect():
            return [item async for item in self.session.events()]

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        await self.session.close()

        self.assertEqual(await asyncio.wait_for(task, 1), [event])

    async def test_connect_failure_can_be_injected(self):
        failing = FakeSession()
        failing.fail_connect = RuntimeError("boom")

        with self.assertRaisesRegex(RuntimeError, "boom"):
            await failing.connect()
        self.assertFalse(failing.connected)
