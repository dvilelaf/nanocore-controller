import asyncio
import unittest

from nanocore_controller.ble_state import (
    read_active_state,
    read_active_state_from_session,
    read_preset_catalog,
    read_preset_catalog_from_session,
)
from nanocore_controller.errors import DeviceStatusError, ProtocolError, VerificationError
from nanocore_controller.nanocore_protocol import NanocoreResponse


class FakeSession:
    def __init__(self, address, *, adapter):
        self.address = address
        self.adapter = adapter
        self.closed = False

    async def connect(self):
        pass

    async def query(self, command, payload=b"", **kwargs):
        if command == 0x63:
            body = bytearray([3, 8, 84])
            for _ in range(8):
                body.extend([0, 0, 0])
            body.extend([8, 0, 1, 2, 3, 4, 5, 6, 7])
            return NanocoreResponse(1, command, 0, bytes(body))
        if command == 0x40:
            return NanocoreResponse(2, command, 0, bytes([6, 1, 8, 0]) + b"WarmCln ")
        if command in (0x36, 0x56):
            slot = payload[0]
            active = slot == (4 if command == 0x36 else 5)
            name = ("Amp" if command == 0x36 else "Cab").encode().ljust(16, b"\0")
            body = bytes([slot, 1, active]) + (4096).to_bytes(4, "little")
            body += (123).to_bytes(4, "little") + bytes.fromhex("1a 1a 00") + name
            return NanocoreResponse(3, command, 0, body)
        raise AssertionError(command)

    async def close(self):
        self.closed = True


class BleStateTests(unittest.TestCase):
    def test_session_helpers_reuse_the_caller_session(self):
        session = FakeSession("AA:BB:CC:DD:EE:FF", adapter="hci1")

        catalog = asyncio.run(read_preset_catalog_from_session(session))
        state = asyncio.run(read_active_state_from_session(session))

        self.assertEqual(catalog[0]["name"], "WarmCln")
        self.assertEqual(state["device"], {"address": session.address, "adapter": session.adapter})
        self.assertFalse(session.closed)

    def test_skips_status_03_while_scanning_asset_slots(self):
        class BusySlotSession(FakeSession):
            async def query(self, command, payload=b"", **kwargs):
                if command == 0x36 and payload == bytes([0]):
                    raise DeviceStatusError(0x36, 0x03)
                return await super().query(command, payload, **kwargs)

        state = asyncio.run(
            read_active_state(
                "AA:BB:CC:DD:EE:FF",
                adapter="hci1",
                session_factory=BusySlotSession,
            )
        )
        self.assertEqual(state["preset"]["amp_slot"], 4)

    def test_reads_complete_current_state_without_mutating_commands(self):
        state = asyncio.run(
            read_active_state(
                "AA:BB:CC:DD:EE:FF",
                adapter="hci1",
                session_factory=FakeSession,
            )
        )
        self.assertEqual(state["preset"]["slot"], 8)
        self.assertEqual(state["preset"]["display_number"], 9)
        self.assertEqual(state["preset"]["name"], "WarmCln")
        self.assertEqual(state["preset"]["amp_slot"], 4)
        self.assertEqual(state["preset"]["ir_slot"], 5)
        self.assertEqual(state["preset"]["chain_order"], list(range(8)))
        self.assertIn("raw_snapshot", state)

    def test_reads_catalog_in_six_preset_pages(self):
        class CatalogSession(FakeSession):
            attempts = 0

            async def query(self, command, payload=b"", **kwargs):
                self.assertion = command
                start = payload[0]
                if start == 0 and CatalogSession.attempts == 0:
                    CatalogSession.attempts += 1
                    raise TimeoutError
                count = 6 if start == 0 else 1
                body = bytearray([start, count])
                for slot in range(start, start + count):
                    body.extend([slot, 0])
                    body.extend(f"P{slot + 1:02d}".encode().ljust(8, b" "))
                return NanocoreResponse(slot, command, 0, bytes(body))

        catalog = asyncio.run(
            read_preset_catalog(
                "AA:BB:CC:DD:EE:FF",
                adapter="hci1",
                session_factory=CatalogSession,
            )
        )
        self.assertEqual(len(catalog), 7)
        self.assertEqual(catalog[-1], {"slot": 6, "display_number": 7, "flags": 0, "name": "P07"})


if __name__ == "__main__":
    unittest.main()


class StateReadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_cached_slot_is_probed_first_so_the_usual_read_is_cheap(self):
        session = FakeSession("AA:BB:CC:DD:EE:FF", adapter="hci0")
        calls = []
        real_query = session.query

        async def query(command, payload=b"", **kwargs):
            calls.append((command, payload))
            return await real_query(command, payload, **kwargs)

        session.query = query

        state = await read_active_state_from_session(session, amp_hint=4, ir_hint=5)

        self.assertEqual((state["preset"]["amp_slot"], state["preset"]["ir_slot"]), (4, 5))
        self.assertEqual(calls.count((0x36, bytes([4]))), 1)
        self.assertEqual(sum(1 for command, _ in calls if command in (0x36, 0x56)), 2)

    async def test_a_wrong_hint_falls_back_to_scanning(self):
        session = FakeSession("AA:BB:CC:DD:EE:FF", adapter="hci0")

        state = await read_active_state_from_session(session, amp_hint=20, ir_hint=0)

        self.assertEqual((state["preset"]["amp_slot"], state["preset"]["ir_slot"]), (4, 5))

    async def test_other_device_statuses_are_not_mistaken_for_an_empty_slot(self):
        class FailingSession(FakeSession):
            async def query(self, command, payload=b"", **kwargs):
                if command == 0x36:
                    raise DeviceStatusError(0x36, 0x7E)
                return await super().query(command, payload, **kwargs)

        session = FailingSession("AA:BB:CC:DD:EE:FF", adapter="hci0")

        with self.assertRaises(DeviceStatusError) as caught:
            await read_active_state_from_session(session)
        self.assertEqual(caught.exception.status, 0x7E)

    async def test_an_answer_for_the_wrong_slot_is_a_protocol_error(self):
        class WrongSlotSession(FakeSession):
            async def query(self, command, payload=b"", **kwargs):
                response = await super().query(command, payload, **kwargs)
                if command == 0x36:
                    return NanocoreResponse(1, command, 0, bytes([29]) + response.payload[1:])
                return response

        session = WrongSlotSession("AA:BB:CC:DD:EE:FF", adapter="hci0")

        with self.assertRaisesRegex(ProtocolError, "asked for asset slot"):
            await read_active_state_from_session(session, amp_hint=4)

    async def test_a_preset_change_during_the_read_makes_it_start_over(self):
        class ChangingSession(FakeSession):
            runtime_reads = 0

            async def query(self, command, payload=b"", **kwargs):
                response = await super().query(command, payload, **kwargs)
                if command == 0x63:
                    self.runtime_reads += 1
                    if self.runtime_reads == 2:  # the end-of-read check of attempt 1
                        body = bytearray(response.payload)
                        body[1] = 3
                        return NanocoreResponse(1, command, 0, bytes(body))
                return response

        session = ChangingSession("AA:BB:CC:DD:EE:FF", adapter="hci0")

        state = await read_active_state_from_session(session, amp_hint=4, ir_hint=5)

        self.assertEqual(state["preset"]["slot"], 8)
        self.assertEqual(session.runtime_reads, 4)

    async def test_a_state_that_never_settles_is_a_verification_error(self):
        class RestlessSession(FakeSession):
            reads = 0

            async def query(self, command, payload=b"", **kwargs):
                response = await super().query(command, payload, **kwargs)
                if command == 0x63:
                    self.reads += 1
                    body = bytearray(response.payload)
                    body[2] = self.reads % 100
                    return NanocoreResponse(1, command, 0, bytes(body))
                return response

        session = RestlessSession("AA:BB:CC:DD:EE:FF", adapter="hci0")

        with self.assertRaises(VerificationError):
            await read_active_state_from_session(session, amp_hint=4, ir_hint=5)

    async def test_a_quick_read_skips_the_consistency_check(self):
        session = FakeSession("AA:BB:CC:DD:EE:FF", adapter="hci0")
        counted = []
        real_query = session.query

        async def query(command, payload=b"", **kwargs):
            counted.append(command)
            return await real_query(command, payload, **kwargs)

        session.query = query

        await read_active_state_from_session(session, amp_hint=4, ir_hint=5, consistent=False)

        self.assertEqual(counted.count(0x63), 1)


class SilentPedalTests(unittest.IsolatedAsyncioTestCase):
    async def test_when_every_asset_slot_stays_silent_the_pedal_is_not_answering(self):
        from nanocore_controller.errors import DeviceTimeout

        class SilentSession(FakeSession):
            async def query(self, command, payload=b"", **kwargs):
                if command in (0x36, 0x56):
                    raise DeviceTimeout(command, maybe_applied=False)
                return await super().query(command, payload, **kwargs)

        session = SilentSession("AA:BB:CC:DD:EE:FF", adapter="hci0")

        with self.assertRaises(DeviceTimeout):
            await read_active_state_from_session(session, consistent=False)


class LostReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_lost_reply_from_the_active_slot_does_not_hide_it(self):
        from nanocore_controller.errors import DeviceTimeout

        class LossySession(FakeSession):
            lost = False

            async def query(self, command, payload=b"", **kwargs):
                if command == 0x56 and payload[0] == 5 and not self.lost:
                    self.lost = True
                    raise DeviceTimeout(command, maybe_applied=False)
                return await super().query(command, payload, **kwargs)

        session = LossySession("AA:BB:CC:DD:EE:FF", adapter="hci0")

        state = await read_active_state_from_session(session, consistent=False)

        self.assertEqual(state["preset"]["ir_slot"], 5)
