import unittest

from nanocore_controller.ble_restore import ASSET_SETTLE_POLLS, verify_restore
from nanocore_controller.errors import VerificationError
from nanocore_controller.nanocore_protocol import NanocoreResponse
from support import PedalSession, load_fixture


class SlowAssetPedal(PedalSession):
    """A pedal whose amplifier and IR slots only report active some queries after being asked for."""

    def __init__(self, polls_until_active: int) -> None:
        super().__init__(load_fixture())
        self.polls_until_active = polls_until_active
        self.asked: dict[int, int] = {}

    async def query(self, command, payload=b"", **kwargs):
        if command in (0x36, 0x56):
            response = await super().query(command, payload, **kwargs)
            seen = self.asked[command] = self.asked.get(command, 0) + 1
            raw = bytearray(response.payload)
            raw[2] = 1 if seen > self.polls_until_active else 0  # byte 2 is the active flag
            return NanocoreResponse(response.sequence, command, response.status, bytes(raw))
        return await super().query(command, payload, **kwargs)


class SettleTest(unittest.IsolatedAsyncioTestCase):
    async def connected(self, pedal):
        await pedal.connect()
        self.addAsyncCleanup(pedal.close)
        return pedal

    async def test_verification_waits_for_a_slow_amplifier_to_load(self):
        pedal = await self.connected(SlowAssetPedal(polls_until_active=3))
        sleeps = []

        async def sleep(seconds):
            sleeps.append(seconds)

        await verify_restore(pedal.document, pedal, sleep=sleep)
        self.assertEqual(len(sleeps), 6)  # three polls for the amplifier, three for the IR

    async def test_a_slot_that_never_becomes_active_is_still_reported(self):
        pedal = await self.connected(SlowAssetPedal(polls_until_active=10_000))
        sleeps = []

        async def sleep(seconds):
            sleeps.append(seconds)

        with self.assertRaises(VerificationError) as raised:
            await verify_restore(pedal.document, pedal, sleep=sleep)
        self.assertIn("amplifier", str(raised.exception))
        self.assertEqual(len(sleeps), ASSET_SETTLE_POLLS)

    async def test_a_slot_that_is_active_at_once_costs_no_waiting(self):
        pedal = await self.connected(SlowAssetPedal(polls_until_active=0))
        sleeps = []

        async def sleep(seconds):
            sleeps.append(seconds)

        await verify_restore(pedal.document, pedal, sleep=sleep)
        self.assertEqual(sleeps, [])


if __name__ == "__main__":
    unittest.main()
