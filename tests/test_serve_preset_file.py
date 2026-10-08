"""Server tests for downloading and loading a preset file (GET and POST /api/preset-file)."""

import copy
import json
from unittest import mock

from nanocore_controller.errors import PartialApplyError
from support_server import ServerTestCase, wait_until


def with_volume(document, volume, slot=None):
    """A copy of a preset file with another preset volume (and optionally another slot)."""

    changed = copy.deepcopy(document)
    changed["preset"]["preset_volume"] = volume
    if slot is not None:
        changed["preset"]["slot"] = slot
        changed["preset"]["display_number"] = slot + 1
    raw = bytearray.fromhex(changed["raw_snapshot"])
    raw[2] = volume
    if slot is not None:
        raw[1] = slot
    changed["raw_snapshot"] = raw.hex()
    return changed


class PresetFileTest(ServerTestCase):
    options_kwargs = {"autosave": False}

    async def download(self):
        response = await self.get("/api/preset-file")
        self.assertEqual(response.status, 200)
        return response, await response.json()

    async def state(self):
        return await (await self.get("/api/state")).json()

    async def test_download_is_a_backup_document_with_an_attachment_name(self):
        response, document = await self.download()
        self.assertEqual(response.content_type, "application/json")
        self.assertEqual(
            response.headers["Content-Disposition"], 'attachment; filename="preset-09-Preset9.json"'
        )
        self.assertEqual(document["format"], "nanocore-controller-backup")
        self.assertEqual(document["preset"]["slot"], 8)
        self.assertEqual(document["preset"]["name"], "Preset9")
        self.assertEqual(document["preset"]["preset_volume"], 84)
        self.assertEqual(self.pedal.live_writes, [])

    async def test_download_name_is_sanitised(self):
        await self.edit({"op": "name", "name": "a/b ..\"'"})
        response, document = await self.download()
        self.assertEqual(response.headers["Content-Disposition"], 'attachment; filename="preset-09-ab.json"')
        self.assertEqual(document["preset"]["name"], "a/b ..\"'")  # the live name, as the page shows it

    async def test_download_name_without_any_safe_character(self):
        await self.edit({"op": "name", "name": "!!! ???"})
        response, _ = await self.download()
        self.assertEqual(response.headers["Content-Disposition"], 'attachment; filename="preset-09.json"')

    async def test_download_needs_a_link(self):
        self.pedal.drop()
        await wait_until(lambda: not self.server.connected)
        self.assertEqual((await self.get("/api/preset-file")).status, 503)

    async def test_download_is_allowed_in_read_only_mode(self):
        self.server.options.read_only = True
        self.assertEqual((await self.get("/api/preset-file")).status, 200)

    async def test_a_file_is_applied_live_and_the_preset_becomes_unsaved(self):
        _, document = await self.download()
        response = await self.post("/api/preset-file", with_volume(document, 33))
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual(body["live"]["volume"], 33)
        self.assertEqual(body["preset"]["slot"], 8)
        self.assertEqual(body["autosave"]["state"], "dirty")
        self.assertEqual(self.server.autosaver.unsaved_slot, 8)
        self.assertEqual(self.pedal.saves, 0)
        self.assertEqual(len(self.server.baselines.list(8)), 1)  # a baseline of the stored preset first
        self.assertEqual(len(list((self.tmp / "backups" / "slot-008").glob("*.json"))), 1)  # the safety backup

    async def test_a_file_of_another_slot_is_applied_to_the_active_preset(self):
        _, document = await self.download()
        response = await self.post("/api/preset-file", with_volume(document, 21, slot=3))
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertEqual((body["preset"]["slot"], body["live"]["volume"]), (8, 21))
        self.assertEqual(self.pedal.runtime[1], 8)
        self.assertEqual(self.pedal.saves, 0)

    async def test_the_name_of_the_file_is_applied_too(self):
        _, document = await self.download()
        document["preset"]["name"] = "FromFile"
        body = await (await self.post("/api/preset-file", with_volume(document, 40))).json()
        self.assertEqual(body["preset"]["name"], "FromFile")
        self.assertIn(b"\x08\x08FromFile", self.pedal.live_writes)
        presets = (await (await self.get("/api/presets")).json())["presets"]
        self.assertEqual(next(p["name"] for p in presets if p["slot"] == 8), "FromFile")
        self.assertEqual(self.pedal.names[8], "Preset9")  # still not stored

    async def test_an_unusable_name_keeps_the_current_one(self):
        _, document = await self.download()
        for bad in (None, "", "   ", "far too long a name", "café", 7):
            changed = with_volume(document, 40)
            changed["preset"]["name"] = bad
            response = await self.post("/api/preset-file", changed)
            self.assertEqual(response.status, 200, bad)
            self.assertEqual((await response.json())["preset"]["name"], "Preset9", bad)
        self.assertFalse([w for w in self.pedal.live_writes if w[:1] == b"\x08"])

    async def test_other_pages_hear_about_the_new_content_and_name(self):
        _, document = await self.download()
        ws = await self.connect_ws()
        document["preset"]["name"] = "FromFile"
        await self.post("/api/preset-file", with_volume(document, 12))
        ops = []
        for _ in range(10):
            try:
                message = await ws.receive_json(timeout=0.5)
            except TimeoutError:
                break
            if message["type"] == "patch":
                ops.extend(message["ops"])
        self.assertIn({"op": "volume", "value": 12}, ops)
        self.assertIn({"op": "name", "name": "FromFile"}, ops)

    async def test_save_now_then_stores_the_file(self):
        _, document = await self.download()
        document["preset"]["name"] = "FromFile"
        await self.post("/api/preset-file", with_volume(document, 33))
        self.assertTrue((await (await self.post("/api/save-now")).json())["saved"])
        self.assertEqual(self.pedal.saves, 1)
        self.assertEqual(self.pedal.names[8], "FromFile")

    async def test_revert_drops_a_loaded_file(self):
        _, document = await self.download()
        document["preset"]["name"] = "FromFile"
        await self.post("/api/preset-file", with_volume(document, 33))
        body = await (await self.post("/api/revert")).json()
        self.assertEqual((body["live"]["volume"], body["preset"]["name"]), (84, "Preset9"))

    async def test_invalid_documents_are_refused_and_nothing_is_written(self):
        _, document = await self.download()
        broken = []
        for mutate in (
            lambda d: d.pop("format"),
            lambda d: d.update(version=2),
            lambda d: d.update(version=True),
            lambda d: d.pop("preset"),
            lambda d: d.update(preset=[]),
            lambda d: d["preset"].update(preset_volume=101),
            lambda d: d["preset"].update(preset_volume="5"),
            lambda d: d["preset"].update(slot=200),
            lambda d: d["preset"].update(chain_order=[0, 0, 1, 2, 3, 4, 5, 6]),
            lambda d: d["preset"].update(effects=d["preset"]["effects"][:7]),
            lambda d: d["preset"]["effects"][0].update(params=[2.0]),
            lambda d: d["preset"]["effects"][0].update(variant=-1),
            lambda d: d.update(raw_snapshot="zz"),
            lambda d: d.update(raw_snapshot=d["raw_snapshot"][:-2]),
            lambda d: d.update(raw_snapshot=1),
            lambda d: d.update(assets=None),
            lambda d: d["assets"]["amp"].update(raw="00"),
            lambda d: d["assets"]["amp"].update(slot=99),
        ):
            changed = copy.deepcopy(document)
            mutate(changed)
            broken.append(changed)
        broken += [[], "x", 5, None, {}, {"preset": {}}]
        for payload in broken:
            response = await self.client.post("/api/preset-file", data=json.dumps(payload), headers={**self.auth, "Content-Type": "application/json"})
            self.assertEqual(response.status, 400, str(payload)[:80])
            self.assertIn((await response.json())["error"]["code"], ("validation", "bad_request"))
        self.assertEqual(self.pedal.live_writes, [])
        self.assertEqual(self.server.autosaver.unsaved_slot, None)

    async def test_only_json_and_a_limited_size_are_accepted(self):
        _, document = await self.download()
        text = json.dumps(with_volume(document, 3))
        response = await self.client.post(
            "/api/preset-file", data=text, headers={**self.auth, "Content-Type": "text/plain"}
        )
        self.assertEqual(response.status, 400)
        response = await self.client.post(
            "/api/preset-file", data=b"{", headers={**self.auth, "Content-Type": "application/json"}
        )
        self.assertEqual(response.status, 400)
        huge = copy.deepcopy(document)
        huge["padding"] = "x" * 70_000
        response = await self.client.post(
            "/api/preset-file", data=json.dumps(huge), headers={**self.auth, "Content-Type": "application/json"}
        )
        self.assertEqual(response.status, 413)
        self.assertEqual(self.pedal.live_writes, [])

    async def test_a_foreign_origin_cannot_load_a_file(self):
        _, document = await self.download()
        response = await self.post(
            "/api/preset-file", document, headers={**self.auth, "Origin": "http://evil.example"}
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(self.pedal.live_writes, [])

    async def test_a_failed_application_is_rolled_back_and_reported(self):
        _, document = await self.download()
        changed = with_volume(document, 33)
        error = PartialApplyError(1, 5, "safety", "boom")
        error.rolled_back = True
        with mock.patch.object(self.device, "restore_active", side_effect=error):
            response = await self.post("/api/preset-file", changed)
        self.assertEqual(response.status, 502)
        self.assertEqual((await response.json())["error"]["code"], "verification_failed")
        self.assertEqual(self.pedal.saves, 0)

    async def test_a_failed_application_that_was_not_rolled_back_stops_the_saving(self):
        _, document = await self.download()
        error = PartialApplyError(1, 5, "safety", "boom")
        error.rolled_back = False
        with mock.patch.object(self.device, "restore_active", side_effect=error):
            response = await self.post("/api/preset-file", with_volume(document, 33))
        self.assertEqual(response.status, 502)
        state = await self.state()
        self.assertEqual(state["autosave"]["state"], "error")
        self.assertEqual(self.pedal.saves, 0)

    async def test_a_link_is_needed(self):
        _, document = await self.download()
        self.pedal.drop()
        await wait_until(lambda: not self.server.connected)
        self.assertEqual((await self.post("/api/preset-file", document)).status, 503)


class PresetFileAutosaveTest(ServerTestCase):
    options_kwargs = {"autosave": True}

    async def test_with_the_timer_the_file_is_stored_after_the_idle_delay(self):
        response = await self.get("/api/preset-file")
        document = with_volume(await response.json(), 33)
        await self.post("/api/preset-file", document)
        await self.clock.advance(5)
        await self.wait_for_state("saved")
        self.assertEqual(self.pedal.saves, 1)


class PresetFileReadOnlyTest(ServerTestCase):
    options_kwargs = {"read_only": True}

    async def test_loading_is_refused_even_for_junk(self):
        for payload in ({}, {"junk": 1}, []):
            response = await self.post("/api/preset-file", payload)
            self.assertEqual(response.status, 403)
            self.assertEqual((await response.json())["error"]["code"], "read_only")
        self.assertEqual(self.pedal.live_writes, [])

    async def test_download_still_works(self):
        self.assertEqual((await self.get("/api/preset-file")).status, 200)
