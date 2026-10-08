import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from nanocore_controller.nanocore_protocol import NanocoreResponse


def runtime_payload(active_preset: int) -> bytes:
    body = bytearray((3, active_preset, 84))
    for _ in range(8):
        body.extend((0, 0, 0))
    body.extend((8, 0, 1, 2, 3, 4, 5, 6, 7))
    return bytes(body)


class FakeSession:
    instances = []

    def __init__(self, address, *, adapter):
        self.address = address
        self.adapter = adapter
        self.connect_count = 0
        self.close_count = 0
        self.queries = []
        self.messages = []
        self.readback_slot = 8
        self.instances.append(self)

    async def connect(self):
        self.connect_count += 1

    async def close(self):
        self.close_count += 1

    async def query(self, command, payload=b""):
        self.queries.append((command, payload))
        if command == 0x63:
            return NanocoreResponse(1, command, 0, runtime_payload(self.readback_slot))
        return NanocoreResponse(1, command, 0, b"")

    async def send(self, message):
        self.messages.append(message)


class BluetoothControllerTests(unittest.TestCase):
    def setUp(self):
        FakeSession.instances = []

    def test_selects_display_preset_with_official_command_and_reads_back(self):
        from nanocore_controller.controller import BluetoothController

        async def exercise():
            async with BluetoothController(
                "AA:BB:CC:DD:EE:FF", adapter="hci1", session_factory=FakeSession
            ) as controller:
                snapshot = await controller.select_preset(9)
                self.assertEqual(snapshot.active_preset, 8)

        asyncio.run(exercise())
        session = FakeSession.instances[0]
        self.assertEqual(session.queries, [(0x6D, bytes.fromhex("09 08")), (0x63, b"")])
        self.assertEqual(session.connect_count, 1)
        self.assertEqual(session.close_count, 1)

    def test_closes_its_only_session_once_when_context_body_fails(self):
        from nanocore_controller.controller import BluetoothController

        async def exercise():
            async with BluetoothController("device", session_factory=FakeSession):
                raise LookupError("boom")

        with self.assertRaisesRegex(LookupError, "boom"):
            asyncio.run(exercise())
        self.assertEqual(len(FakeSession.instances), 1)
        self.assertEqual(FakeSession.instances[0].connect_count, 1)
        self.assertEqual(FakeSession.instances[0].close_count, 1)

    def test_closes_its_only_session_once_when_connect_fails(self):
        from nanocore_controller.controller import BluetoothController

        class FailingSession(FakeSession):
            async def connect(self):
                self.connect_count += 1
                raise ConnectionError("radio unavailable")

        async def exercise():
            async with BluetoothController("device", session_factory=FailingSession):
                self.fail("the context body must not run")

        with self.assertRaisesRegex(ConnectionError, "radio unavailable"):
            asyncio.run(exercise())
        self.assertEqual(len(FakeSession.instances), 1)
        self.assertEqual(FakeSession.instances[0].connect_count, 1)
        self.assertEqual(FakeSession.instances[0].close_count, 1)

    def test_connect_error_is_not_masked_when_close_also_fails(self):
        from nanocore_controller.controller import BluetoothController

        class FailingSession(FakeSession):
            async def connect(self):
                self.connect_count += 1
                raise ConnectionError("connect failed")

            async def close(self):
                self.close_count += 1
                raise OSError("close failed")

        async def exercise():
            async with BluetoothController("device", session_factory=FailingSession):
                self.fail("the context body must not run")

        with self.assertRaisesRegex(ConnectionError, "connect failed"):
            asyncio.run(exercise())
        self.assertEqual(FakeSession.instances[0].close_count, 1)

    def test_body_error_is_not_masked_when_close_also_fails(self):
        from nanocore_controller.controller import BluetoothController

        class CloseFailingSession(FakeSession):
            async def close(self):
                self.close_count += 1
                raise OSError("close failed")

        async def exercise():
            async with BluetoothController("device", session_factory=CloseFailingSession):
                raise LookupError("body failed")

        with self.assertRaisesRegex(LookupError, "body failed"):
            asyncio.run(exercise())
        self.assertEqual(FakeSession.instances[0].close_count, 1)

    def test_close_error_propagates_without_a_primary_error(self):
        from nanocore_controller.controller import BluetoothController

        class CloseFailingSession(FakeSession):
            async def close(self):
                self.close_count += 1
                raise OSError("close failed")

        async def exercise():
            async with BluetoothController("device", session_factory=CloseFailingSession):
                pass

        with self.assertRaisesRegex(OSError, "close failed"):
            asyncio.run(exercise())
        self.assertEqual(FakeSession.instances[0].close_count, 1)

    def test_rejects_empty_midi_and_sends_nonempty_midi_on_same_session(self):
        from nanocore_controller.controller import BluetoothController

        async def exercise():
            async with BluetoothController("device", session_factory=FakeSession) as controller:
                with self.assertRaisesRegex(ValueError, "empty"):
                    await controller.send_midi(b"")
                await controller.send_midi(bytes.fromhex("c0 08"))

        asyncio.run(exercise())
        session = FakeSession.instances[0]
        self.assertEqual(session.messages, [bytes.fromhex("c0 08")])
        self.assertEqual(session.connect_count, 1)
        self.assertEqual(session.close_count, 1)

    def test_rejects_invalid_display_preset_numbers(self):
        from nanocore_controller.controller import BluetoothController

        async def exercise():
            async with BluetoothController("device", session_factory=FakeSession) as controller:
                for value in (0, 129, True, "9"):
                    with self.assertRaisesRegex(ValueError, "1 to 128"):
                        await controller.select_preset(value)

        asyncio.run(exercise())
        self.assertEqual(FakeSession.instances[0].queries, [])

    def test_select_preset_fails_clearly_when_readback_does_not_match(self):
        from nanocore_controller.controller import BluetoothController

        async def exercise():
            async with BluetoothController("device", session_factory=FakeSession) as controller:
                controller._session.readback_slot = 7
                await controller.select_preset(9)

        with self.assertRaisesRegex(RuntimeError, "preset.*9.*read-back.*8"):
            asyncio.run(exercise())

    def test_select_preset_polls_stale_readback_until_target_is_active(self):
        from nanocore_controller.controller import BluetoothController

        class EventuallyCurrentSession(FakeSession):
            readback_slots = iter((7, 8))

            async def query(self, command, payload=b""):
                self.queries.append((command, payload))
                if command == 0x63:
                    return NanocoreResponse(
                        1, command, 0, runtime_payload(next(self.readback_slots))
                    )
                return NanocoreResponse(1, command, 0, b"")

        delays = []

        async def fake_sleep(delay):
            delays.append(delay)

        async def exercise():
            async with BluetoothController(
                "device",
                session_factory=EventuallyCurrentSession,
                preset_readback_delay=0.01,
                sleep=fake_sleep,
            ) as controller:
                return await controller.select_preset(9)

        snapshot = asyncio.run(exercise())
        session = FakeSession.instances[0]
        self.assertEqual(snapshot.active_preset, 8)
        self.assertEqual(
            session.queries,
            [(0x6D, bytes.fromhex("09 08")), (0x63, b""), (0x63, b"")],
        )
        self.assertEqual(delays, [0.01])

    def test_select_preset_fails_after_bounded_stale_readbacks(self):
        from nanocore_controller.controller import BluetoothController

        delays = []

        async def fake_sleep(delay):
            delays.append(delay)

        async def exercise():
            async with BluetoothController(
                "device",
                session_factory=FakeSession,
                preset_readback_delay=0.01,
                sleep=fake_sleep,
            ) as controller:
                controller._session.readback_slot = 7
                await controller.select_preset(9)

        with self.assertRaisesRegex(RuntimeError, "after 3 read-back attempts"):
            asyncio.run(exercise())
        session = FakeSession.instances[0]
        self.assertEqual([command for command, _ in session.queries].count(0x63), 3)
        self.assertEqual(delays, [0.01, 0.01])

    def test_is_a_nanocore_device_with_bluetooth_backup_metadata(self):
        from nanocore_controller.controller import BluetoothController
        from nanocore_controller.device import NanocoreDevice

        controller = BluetoothController(
            "AA:BB:CC:DD:EE:FF", adapter="hci3", session_factory=FakeSession
        )
        self.assertIsInstance(controller, NanocoreDevice)
        self.assertEqual((controller.address, controller.adapter), ("AA:BB:CC:DD:EE:FF", "hci3"))
        self.assertEqual(
            controller._device_info, {"address": "AA:BB:CC:DD:EE:FF", "adapter": "hci3"}
        )
        self.assertEqual(
            (FakeSession.instances[0].address, FakeSession.instances[0].adapter),
            ("AA:BB:CC:DD:EE:FF", "hci3"),
        )

    def test_status_and_presets_reuse_owned_session(self):
        from nanocore_controller.controller import BluetoothController

        status_result = {"preset": {"display_number": 9, "amp_slot": 1, "ir_slot": 2}}
        presets_result = [{"display_number": 9, "name": "WarmCln"}]

        async def exercise():
            async with BluetoothController(
                "device", adapter="hci7", session_factory=FakeSession
            ) as controller:
                session = FakeSession.instances[0]
                with (
                    patch(
                        "nanocore_controller.device.read_active_state_from_session",
                        AsyncMock(return_value=status_result),
                    ) as read_state,
                    patch(
                        "nanocore_controller.device.read_preset_catalog_from_session",
                        AsyncMock(return_value=presets_result),
                    ) as read_presets,
                ):
                    self.assertIs(await controller.status(), status_result)
                    self.assertIs(await controller.presets(), presets_result)
                    read_state.assert_awaited_once()
                    self.assertIs(read_state.await_args.args[0], session)
                    read_presets.assert_awaited_once_with(session)

        asyncio.run(exercise())
        self.assertEqual(FakeSession.instances[0].connect_count, 1)
        self.assertEqual(FakeSession.instances[0].close_count, 1)

    def test_concurrent_high_level_operations_do_not_overlap(self):
        from nanocore_controller.controller import BluetoothController

        active = 0
        maximum_active = 0

        async def tracked_result(result, *args):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            active -= 1
            return result

        async def tracked_status(*args, **kwargs):
            return await tracked_result({"preset": {"amp_slot": 1, "ir_slot": 2}}, *args)

        async def tracked_presets(*args):
            return await tracked_result([], *args)

        async def exercise():
            async with BluetoothController("device", session_factory=FakeSession) as controller:
                with (
                    patch(
                        "nanocore_controller.device.read_active_state_from_session",
                        side_effect=tracked_status,
                    ),
                    patch(
                        "nanocore_controller.device.read_preset_catalog_from_session",
                        side_effect=tracked_presets,
                    ),
                ):
                    await asyncio.gather(controller.status(), controller.presets())

        asyncio.run(exercise())
        self.assertEqual(maximum_active, 1)


if __name__ == "__main__":
    unittest.main()
