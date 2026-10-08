"""Server tests for renaming the active preset as a live edit (op ``name``)."""


from support_server import ServerTestCase, wait_until


def name_op(name):
    return {"op": "name", "name": name}


async def drain(ws, limit=10, timeout=0.5):
    seen = []
    for _ in range(limit):
        try:
            seen.append(await ws.receive_json(timeout=timeout))
        except TimeoutError:
            break
    return seen


class RenameTest(ServerTestCase):
    options_kwargs = {"autosave": False}

    async def state(self):
        return await (await self.get("/api/state")).json()

    async def catalog_name(self, slot=8):
        presets = (await (await self.get("/api/presets")).json())["presets"]
        return next(p["name"] for p in presets if p["slot"] == slot)

    async def test_the_name_is_sent_live_and_shown_by_the_model(self):
        response = await self.edit(name_op("Rock"))
        self.assertEqual(response.status, 202)
        self.assertEqual((await response.json())["applied"], 1)
        self.assertIn(b"\x08\x04Rock", self.pedal.live_writes)
        self.assertEqual((await self.state())["preset"]["name"], "Rock")
        self.assertEqual(await self.catalog_name(), "Rock")
        self.assertEqual(await self.catalog_name(3), "Preset4")

    async def test_trailing_spaces_are_dropped(self):
        await self.edit(name_op("Abc   "))
        self.assertEqual((await self.state())["preset"]["name"], "Abc")

    async def test_the_preset_becomes_unsaved_and_nothing_is_stored(self):
        await self.edit(name_op("Rock"))
        self.assertEqual(self.server.autosaver.unsaved_slot, 8)
        self.assertEqual((await self.state())["autosave"]["state"], "dirty")
        self.assertEqual(self.pedal.saves, 0)
        self.assertEqual(len(self.server.baselines.list(8)), 1)  # a baseline before the first edit

    async def test_invalid_names_are_rejected_before_anything_is_sent(self):
        for bad in ("", "   ", "123456789", "café", "tab\t", "☃", None, 5, ["a"], True):
            response = await self.edit(name_op(bad))
            self.assertEqual(response.status, 400, bad)
            self.assertEqual((await response.json())["error"]["code"], "validation", bad)
        for payload in ({"op": "name"}, {"op": "name", "name": "ok", "extra": 1}):
            response = await self.edit(payload)
            self.assertEqual(response.status, 400, payload)
        self.assertEqual(self.pedal.live_writes, [])
        self.assertEqual((await self.state())["preset"]["name"], "Preset9")

    async def test_a_name_with_eight_characters_and_punctuation_is_fine(self):
        response = await self.edit(name_op("A-b_c 1!"))
        self.assertEqual(response.status, 202)
        self.assertEqual((await self.state())["preset"]["name"], "A-b_c 1!")

    async def test_a_pedal_that_reports_another_name_fails_the_edit_and_the_model_keeps_the_old_one(self):
        self.pedal.name_echo = b"\x08\x03abc"
        response = await self.edit(name_op("Rock"))
        self.assertEqual(response.status, 502)
        self.assertEqual((await response.json())["error"]["code"], "verification_failed")
        self.assertEqual((await self.state())["preset"]["name"], "Preset9")

    async def test_other_pages_are_told_with_a_patch_and_the_sender_is_not(self):
        sender = await self.connect_ws()
        other = await self.connect_ws()
        await sender.send_json({"type": "edit", "client_seq": 1, "ops": [name_op("Rock")]})
        patches = [m for m in await drain(other) if m["type"] == "patch"]
        self.assertEqual(patches[-1]["ops"], [name_op("Rock")])
        sent = [m for m in await drain(sender) if m["type"] == "patch"]
        self.assertEqual(sent, [])
        # a page that connects later gets the new name in the full state
        late = await self.client.ws_connect("/ws")
        await late.send_json({"type": "auth", "token": self.auth["X-Nanocore-Token"]})
        self.assertEqual((await late.receive_json(timeout=2))["state"]["preset"]["name"], "Rock")

    async def test_save_now_stores_the_name_and_reads_it_back_from_the_catalog(self):
        await self.edit(name_op("Rock"))
        before = sum(1 for command, _ in self.pedal.queries if command == 0x40)
        body = await (await self.post("/api/save-now")).json()
        self.assertTrue(body["saved"])
        self.assertGreater(sum(1 for command, _ in self.pedal.queries if command == 0x40), before)
        self.assertEqual(self.pedal.names[8], "Rock")
        self.assertEqual(await self.catalog_name(), "Rock")
        self.assertEqual(self.server._renamed, set())

    async def test_revert_shows_the_stored_name_again(self):
        ws = await self.connect_ws()
        await self.edit(name_op("Rock"))
        body = await (await self.post("/api/revert")).json()
        self.assertEqual(body["preset"]["name"], "Preset9")
        self.assertEqual(await self.catalog_name(), "Preset9")
        states = [m for m in await drain(ws) if m["type"] == "state"]
        self.assertEqual(states[-1]["state"]["preset"]["name"], "Preset9")
        self.assertEqual(self.pedal.saves, 0)

    async def test_recall_with_discard_shows_the_stored_name_again(self):
        await self.edit(name_op("Rock"))
        refused = await self.post("/api/preset", {"display_number": 4})
        self.assertEqual(refused.status, 409)
        self.assertEqual((await self.state())["preset"]["name"], "Rock")
        accepted = await self.post("/api/preset", {"display_number": 4, "discard": True})
        self.assertEqual(accepted.status, 200)
        self.assertEqual((await accepted.json())["preset"]["name"], "Preset4")
        self.assertEqual(await self.catalog_name(8), "Preset9")
        self.assertEqual(self.pedal.saves, 0)

    async def test_recall_with_discard_of_the_same_preset_tells_pages_the_stored_name(self):
        ws = await self.connect_ws()
        await self.edit(name_op("Rock"))
        await self.post("/api/preset", {"display_number": 9, "discard": True})
        names = [m["ops"][0]["name"] for m in await drain(ws) if m["type"] == "patch" and m["ops"][0]["op"] == "name"]
        self.assertEqual(names[-1], "Preset9")

    async def test_switching_preset_on_the_pedal_drops_the_unstored_name(self):
        await self.edit(name_op("Rock"))
        self.pedal.live_name = None  # the pedal forgets RAM edits of the old preset
        self.pedal.switch_to_slot(3)
        self.pedal.push_pedal_event()
        await wait_until(lambda: self.server._slot == 3)
        await wait_until(lambda: not self.server._renamed)
        self.assertEqual(await self.catalog_name(8), "Preset9")

    async def test_a_second_rename_replaces_the_first(self):
        await self.edit(name_op("One"))
        await self.edit(name_op("Two"))
        self.assertEqual((await self.state())["preset"]["name"], "Two")
        await self.post("/api/save-now")
        self.assertEqual(self.pedal.names[8], "Two")

    async def test_renaming_is_refused_when_the_link_is_down(self):
        self.pedal.drop()
        await wait_until(lambda: not self.server.connected)
        self.assertEqual((await self.edit(name_op("Rock"))).status, 503)


class RenameAutosaveTest(ServerTestCase):
    options_kwargs = {"autosave": True}

    async def test_the_timer_stores_the_renamed_preset_and_the_catalog_is_read_again(self):
        await self.edit(name_op("Rock"))
        await self.clock.advance(5)
        await self.wait_for_state("saved")
        await wait_until(lambda: not self.server._renamed)
        self.assertEqual(self.pedal.names[8], "Rock")


class RenameReadOnlyTest(ServerTestCase):
    options_kwargs = {"read_only": True}

    async def test_renaming_is_refused(self):
        response = await self.edit(name_op("Rock"))
        self.assertEqual(response.status, 403)
        self.assertEqual((await response.json())["error"]["code"], "read_only")
        self.assertEqual(self.pedal.live_writes, [])

    async def test_renaming_over_the_websocket_is_refused(self):
        ws = await self.connect_ws()
        await ws.send_json({"type": "edit", "client_seq": 4, "ops": [name_op("Rock")]})
        error = [m for m in await drain(ws) if m["type"] == "error"][0]
        self.assertEqual(error["error"]["code"], "read_only")
        self.assertEqual(self.pedal.live_writes, [])
