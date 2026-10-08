"""Server tests for the global settings endpoints."""

from unittest import mock

from nanocore_controller.errors import DeviceStatusError
from support_server import ServerTestCase, wait_until


class SettingsTest(ServerTestCase):
    async def test_get_returns_the_global_settings(self):
        response = await self.get("/api/settings")
        self.assertEqual(response.status, 200)
        self.assertEqual(
            await response.json(),
            {
                "version": 1,
                "wireless_enabled": True,
                "loopback_enabled": False,
                "input_gain_db": 1,
                "usb_volume": 100,
                "bt_volume": 100,
                "midi_channel": 0,
                "volume_floor": None,
                "volume_ceiling": None,
            },
        )

    async def test_post_changes_only_the_given_fields_and_returns_the_new_settings(self):
        response = await self.post("/api/settings", {"input_gain_db": -7, "loopback_enabled": True})
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual((body["input_gain_db"], body["loopback_enabled"], body["usb_volume"]), (-7, True, 100))
        self.assertEqual(self.pedal.set_payloads, [bytes((0x06, 1, 0xF9))])
        again = await (await self.get("/api/settings")).json()
        self.assertEqual(again, body)

    async def test_every_field_can_be_written(self):
        for payload in (
            {"wireless_enabled": False},
            {"usb_volume": 0},
            {"bt_volume": 100},
            {"midi_channel": 16},
            {"input_gain_db": 20},
            {"input_gain_db": -20},
        ):
            response = await self.post("/api/settings", payload)
            self.assertEqual(response.status, 200, payload)
            body = await response.json()
            for key, value in payload.items():
                self.assertEqual(body[key], value)

    async def test_validation_is_strict(self):
        bad = [
            {},
            [],
            "x",
            {"unknown": 1},
            {"usb_volume": 5, "unknown": 1},
            {"input_gain_db": 21},
            {"input_gain_db": -21},
            {"input_gain_db": 1.5},
            {"input_gain_db": True},
            {"input_gain_db": "3"},
            {"usb_volume": 101},
            {"usb_volume": -1},
            {"bt_volume": 100.0},
            {"midi_channel": 17},
            {"midi_channel": None},
            {"loopback_enabled": 1},
            {"loopback_enabled": "true"},
            {"wireless_enabled": None},
            {"version": 2},
            {"volume_floor": 1},
        ]
        for payload in bad:
            response = await self.post("/api/settings", payload)
            self.assertEqual(response.status, 400, payload)
            self.assertEqual((await response.json())["error"]["code"], "validation", payload)
        self.assertEqual(self.pedal.set_payloads, [])

    async def test_only_json_is_accepted(self):
        response = await self.client.post(
            "/api/settings", data=b'{"usb_volume": 5}', headers={**self.auth, "Content-Type": "text/plain"}
        )
        self.assertEqual(response.status, 400)
        response = await self.client.post(
            "/api/settings", data=b"{", headers={**self.auth, "Content-Type": "application/json"}
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(self.pedal.set_payloads, [])

    async def test_a_foreign_origin_cannot_change_settings(self):
        response = await self.post(
            "/api/settings", {"usb_volume": 5}, headers={**self.auth, "Origin": "http://evil.example"}
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(self.pedal.set_payloads, [])

    async def test_settings_do_not_touch_the_saving_state(self):
        await self.post("/api/settings", {"usb_volume": 5})
        self.assertEqual(self.server.autosaver.state, "saved")
        self.assertIsNone(self.server.autosaver.unsaved_slot)
        self.assertEqual(self.pedal.live_writes, [])
        self.assertEqual(self.pedal.saved_slots, [])

    async def test_a_pedal_that_does_not_take_the_value_is_a_verification_failure(self):
        self.pedal.ignore_settings_writes = True
        response = await self.post("/api/settings", {"usb_volume": 5})
        self.assertEqual(response.status, 502)
        self.assertEqual((await response.json())["error"]["code"], "verification_failed")

    async def test_a_status_error_from_the_pedal_is_a_gateway_error(self):
        with mock.patch.object(self.device, "write_global_settings", side_effect=DeviceStatusError(0x66, 1)):
            response = await self.post("/api/settings", {"usb_volume": 5})
        self.assertEqual(response.status, 502)
        self.assertEqual((await response.json())["error"]["code"], "device_status")

    async def test_reads_and_writes_go_through_the_single_writer(self):
        submitted = []
        original = self.server.writer.submit

        async def spy(run, edit=None):
            submitted.append(run)
            return await original(run, edit)

        with mock.patch.object(self.server.writer, "submit", spy):
            await self.get("/api/settings")
            await self.post("/api/settings", {"usb_volume": 5})
        self.assertEqual(len(submitted), 2)

    async def test_nothing_works_without_a_link(self):
        self.pedal.drop()
        await wait_until(lambda: not self.server.connected)
        self.assertEqual((await self.get("/api/settings")).status, 503)
        self.assertEqual((await self.post("/api/settings", {"usb_volume": 5})).status, 503)

    async def test_writes_are_rate_limited(self):
        statuses = [(await self.post("/api/settings", {"usb_volume": n})).status for n in range(40)]
        self.assertEqual(statuses[0], 200)
        self.assertIn(429, statuses)


class SettingsReadOnlyTest(ServerTestCase):
    options_kwargs = {"read_only": True}

    async def test_writing_is_refused_and_reading_still_works(self):
        for payload in ({"usb_volume": 5}, {}, {"junk": 1}):
            response = await self.post("/api/settings", payload)
            self.assertEqual(response.status, 403)
            self.assertEqual((await response.json())["error"]["code"], "read_only")
        self.assertEqual(self.pedal.set_payloads, [])
        self.assertEqual((await self.get("/api/settings")).status, 200)
