import tempfile
from pathlib import Path

from support_server import ServerTestCase, wait_until

_AUDIO_DIR = Path(tempfile.mkdtemp(prefix="nanocore-audio-switch-"))
AUDIO_FILE = _AUDIO_DIR / "speakers-off"


async def next_of_type(ws, kind, limit=10):
    for _ in range(limit):
        message = await ws.receive_json(timeout=2)
        if message["type"] == kind:
            return message
    raise AssertionError(f"no {kind!r} message arrived")


class ReleaseTest(ServerTestCase):
    async def test_the_state_says_the_pedal_is_not_released(self):
        state = await (await self.get("/api/state")).json()
        self.assertIs(state["released"], False)

    async def test_release_closes_the_usb_link_and_tells_the_pages(self):
        ws = await self.connect_ws()
        response = await self.post("/api/release")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"released": True})
        await wait_until(lambda: not self.pedal.connected)
        self.assertFalse(self.server.connected)
        message = await next_of_type(ws, "connection")
        self.assertEqual((message["connected"], message["released"]), (False, True))
        state = await (await self.get("/api/state")).json()
        self.assertEqual((state["connected"], state["released"]), (False, True))

    async def test_it_stays_released_however_long_it_waits(self):
        await self.post("/api/release")
        await wait_until(lambda: not self.pedal.connected)
        for _ in range(5):
            await self.clock.advance(60)
        self.assertFalse(self.pedal.connected)
        self.assertFalse(self.server.connected)

    async def test_edits_and_reads_of_the_pedal_are_refused_while_released(self):
        await self.post("/api/release")
        await wait_until(lambda: not self.server.connected)
        response = await self.edit({"op": "volume", "value": 1})
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())["error"]["code"], "disconnected")

    async def test_resume_reconnects_at_once_and_sends_the_state(self):
        ws = await self.connect_ws()
        await self.post("/api/release")
        await wait_until(lambda: not self.server.connected)
        response = await self.post("/api/resume")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"released": False})
        await wait_until(lambda: self.server.connected)
        self.assertTrue(self.pedal.connected)
        message = await next_of_type(ws, "connection")  # the release itself
        self.assertEqual(message["released"], True)
        message = await next_of_type(ws, "connection")
        self.assertEqual((message["connected"], message["released"]), (True, False))
        self.assertEqual((await self.edit({"op": "volume", "value": 1})).status, 202)

    async def test_releasing_twice_and_resuming_when_not_released_are_harmless(self):
        self.assertEqual((await self.post("/api/resume")).status, 200)
        self.assertTrue(self.server.connected)
        self.assertEqual((await self.post("/api/release")).status, 200)
        self.assertEqual((await self.post("/api/release")).status, 200)
        await wait_until(lambda: not self.server.connected)
        self.assertEqual((await self.post("/api/resume")).status, 200)
        await wait_until(lambda: self.server.connected)

    async def test_both_need_the_token(self):
        for path in ("/api/release", "/api/resume"):
            response = await self.client.post(path)
            self.assertEqual(response.status, 401, path)
        self.assertTrue(self.server.connected)


class ReleaseInReadOnlyTest(ServerTestCase):
    options_kwargs = {"read_only": True}

    async def test_a_read_only_server_can_still_release_the_pedal(self):
        self.assertEqual((await self.post("/api/release")).status, 200)
        await wait_until(lambda: not self.pedal.connected)
        self.assertEqual((await self.post("/api/resume")).status, 200)
        await wait_until(lambda: self.server.connected)


class AudioSwitchTest(ServerTestCase):
    options_kwargs = {"audio_switch_file": AUDIO_FILE}

    async def asyncSetUp(self) -> None:
        AUDIO_FILE.unlink(missing_ok=True)
        self.addCleanup(lambda: AUDIO_FILE.unlink(missing_ok=True))
        await super().asyncSetUp()

    async def test_the_state_says_the_speakers_are_on(self):
        state = await (await self.get("/api/state")).json()
        self.assertEqual(state["audio"], {"available": True, "on": True})

    async def test_switching_off_creates_the_file_and_tells_the_pages(self):
        ws = await self.connect_ws()
        response = await self.post("/api/audio", {"on": False})
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"available": True, "on": False})
        self.assertTrue(AUDIO_FILE.exists())
        message = await next_of_type(ws, "audio")
        self.assertEqual((message["available"], message["on"]), (True, False))
        state = await (await self.get("/api/state")).json()
        self.assertEqual(state["audio"], {"available": True, "on": False})

    async def test_switching_on_removes_the_file(self):
        await self.post("/api/audio", {"on": False})
        response = await self.post("/api/audio", {"on": True})
        self.assertEqual(await response.json(), {"available": True, "on": True})
        self.assertFalse(AUDIO_FILE.exists())

    async def test_a_file_created_outside_is_seen(self):
        AUDIO_FILE.touch()
        state = await (await self.get("/api/state")).json()
        self.assertEqual(state["audio"], {"available": True, "on": False})

    async def test_it_does_not_depend_on_the_pedal_link(self):
        await self.post("/api/release")
        await wait_until(lambda: not self.server.connected)
        self.assertEqual((await self.post("/api/audio", {"on": False})).status, 200)

    async def test_a_body_without_a_boolean_is_refused(self):
        for body in ({}, {"on": "no"}, {"on": 1}, [True]):
            response = await self.post("/api/audio", body)
            self.assertEqual(response.status, 400, body)
        self.assertFalse(AUDIO_FILE.exists())

    async def test_it_needs_the_token(self):
        self.assertEqual((await self.client.post("/api/audio", json={"on": False})).status, 401)
        self.assertFalse(AUDIO_FILE.exists())


class NoAudioSwitchTest(ServerTestCase):
    async def test_without_the_option_the_switch_is_not_available(self):
        state = await (await self.get("/api/state")).json()
        self.assertEqual(state["audio"], {"available": False, "on": False})
        response = await self.post("/api/audio", {"on": False})
        self.assertEqual(response.status, 409)
        self.assertEqual((await response.json())["error"]["code"], "not_available")
