import asyncio
import stat

from nanocore_controller.errors import BackupError, DeviceNotFound, DeviceStatusError
from support_server import ServerTestCase, settle, wait_until

VOLUME = {"op": "volume", "value": 40}


class AutosaveTest(ServerTestCase):
    def backups(self, slot=8):
        return sorted((self.tmp / "backups" / f"slot-{slot:03d}").glob("*.json"))

    async def test_saves_only_after_the_idle_delay(self):
        await self.edit(VOLUME)
        self.assertEqual(self.server.autosaver.state, "dirty")
        await self.clock.advance(2.9)
        self.assertEqual(self.pedal.saves, 0)
        await self.clock.advance(0.2)
        await self.wait_for_state("saved")
        self.assertEqual(self.pedal.saved_slots, [8])
        self.assertIsNotNone(self.server.autosaver.snapshot()["last_saved_at"])

    async def test_a_new_edit_restarts_the_idle_timer(self):
        await self.edit(VOLUME)
        await self.clock.advance(2)
        await self.edit({"op": "volume", "value": 41})
        await self.clock.advance(2)
        self.assertEqual(self.pedal.saves, 0)
        await self.clock.advance(1.1)
        await self.wait_for_state("saved")
        self.assertEqual(self.pedal.saves, 1)

    async def test_flash_writes_are_at_least_ten_seconds_apart(self):
        await self.edit(VOLUME)
        await self.clock.advance(3.1)
        await self.wait_for_state("saved")
        await self.clock.advance(4)
        await self.edit({"op": "volume", "value": 41})
        await self.clock.advance(3.1)  # idle for 3 s, but only 7.1 s after the save
        self.assertEqual(self.pedal.saves, 1)
        self.assertEqual(self.server.autosaver.state, "dirty")
        await self.clock.advance(3.0)
        await self.wait_for_state("saved")
        self.assertEqual(self.pedal.saves, 2)

    async def test_every_save_writes_a_new_private_backup_and_the_newest_20_are_kept(self):
        for value in range(25):
            await self.edit({"op": "volume", "value": value})
            self.assertEqual((await self.post("/api/save-now")).status, 200)
        files = self.backups()
        self.assertEqual(len(files), 20)
        self.assertEqual(len({f.name for f in files}), 20)
        self.assertEqual(stat.S_IMODE(files[-1].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(files[-1].parent.stat().st_mode), 0o700)
        self.assertEqual(self.pedal.saves, 25)

    async def test_the_baseline_exists_before_the_first_live_write(self):
        seen = []
        self.pedal.before_command[0x6D] = lambda session: seen.append(len(self.server.baselines.list(8)))
        await self.edit(VOLUME)
        await self.edit({"op": "volume", "value": 41})
        self.assertEqual(seen, [1, 1])
        self.assertEqual(len(self.server.baselines.list(8)), 1)  # once per slot and run
        document = self.server.baselines.load(8, self.server.baselines.list(8)[0].id)
        self.assertEqual(document["preset"]["preset_volume"], 84)  # the state before the edit

    async def test_each_slot_gets_its_own_baseline(self):
        await self.edit(VOLUME)
        await self.post("/api/preset", {"display_number": 4})
        await self.edit(VOLUME)
        self.assertEqual(len(self.server.baselines.list(8)), 1)
        self.assertEqual(len(self.server.baselines.list(3)), 1)

    async def test_a_failing_baseline_blocks_the_edit_and_reports_the_error(self):
        def fail(document):
            raise BackupError("disk full at /var/lib/x/y")

        self.assertEqual(len(self.server.baselines.list(8)), 1)  # taken when the server connected
        self.server.baselines.record = fail
        await self.post("/api/preset", {"display_number": 4})  # a preset with no baseline yet
        response = await self.edit(VOLUME)
        body = await response.json()
        self.assertEqual(response.status, 500)
        self.assertEqual(body["error"]["code"], "backup_failed")
        self.assertNotIn("/var", str(body))
        self.assertEqual(body["error"]["applied"], 0)
        self.assertEqual(self.pedal.live_writes, [b"\t\x03"])  # only the preset recall itself
        self.assertEqual(self.server.autosaver.state, "saved")
        del self.server.baselines.record  # recovers once recording works
        self.assertEqual((await self.edit(VOLUME)).status, 202)

    async def test_a_preset_change_before_the_save_skips_it_and_keeps_the_state_dirty(self):
        ws = await self.connect_ws()
        await self.edit(VOLUME)
        self.pedal.switch_to_slot(3)
        await self.clock.advance(3.1)
        await wait_until(lambda: self.server.autosaver.snapshot()["error"] is not None)
        view = self.server.autosaver.snapshot()
        self.assertEqual(view["state"], "dirty")
        self.assertIn("preset 4 is active", view["error"])
        self.assertEqual(self.pedal.saves, 0)
        details = []
        while not details:
            message = await ws.receive_json(timeout=2)
            if message["type"] == "autosave" and message["detail"]:
                details.append(message["detail"])
        self.assertIn("nothing was stored", details[0])
        await self.clock.advance(60)
        self.assertEqual(self.pedal.saves, 0)  # no retry loop

    async def test_a_new_edit_on_another_preset_takes_over(self):
        await self.edit(VOLUME)
        self.pedal.switch_to_slot(3)
        await self.get("/api/state")  # the editor notices the switch
        self.assertEqual(self.server.autosaver.state, "dirty")
        self.assertIn("no longer active", self.server.autosaver.snapshot()["error"])
        await self.edit(VOLUME)
        self.assertIsNone(self.server.autosaver.snapshot()["error"])
        await self.clock.advance(3.1)
        await self.wait_for_state("saved")
        self.assertEqual(self.pedal.saved_slots, [3])

    async def test_a_save_error_is_reported_and_the_next_edit_recovers(self):
        self.pedal.save_error = DeviceStatusError(0x46, 3)
        await self.edit(VOLUME)
        await self.clock.advance(3.1)
        await self.wait_for_state("error")
        self.assertIn("0x46", self.server.autosaver.snapshot()["error"])
        await self.clock.advance(60)
        self.assertEqual(self.server.autosaver.state, "error")  # not retried blindly
        self.pedal.save_error = None
        await self.edit({"op": "volume", "value": 41})
        self.assertEqual(self.server.autosaver.state, "dirty")
        await self.clock.advance(3.1)
        await self.wait_for_state("saved")
        self.assertIsNone(self.server.autosaver.snapshot()["error"])

    async def test_save_now_flushes_immediately_and_reports_whether_it_saved(self):
        await self.edit(VOLUME)
        first = await (await self.post("/api/save-now")).json()
        self.assertTrue(first["saved"])
        self.assertEqual(first["autosave"]["state"], "saved")
        second = await (await self.post("/api/save-now")).json()
        self.assertFalse(second["saved"])
        self.assertEqual(self.pedal.saves, 1)

    async def test_save_now_reports_a_failing_save(self):
        self.pedal.save_error = DeviceStatusError(0x46, 3)
        await self.edit(VOLUME)
        response = await self.post("/api/save-now")
        self.assertEqual(response.status, 502)
        self.assertEqual(self.server.autosaver.state, "error")

    async def test_recalling_a_preset_stores_the_pending_edit_of_the_previous_one_first(self):
        await self.edit(VOLUME)
        response = await self.post("/api/preset", {"display_number": 4})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.pedal.saved_slots, [8])
        self.assertEqual((await response.json())["preset"]["slot"], 3)
        order = [c for c in self.pedal.log if c in (0x46,)]
        self.assertEqual(order, [0x46])

    async def test_a_failed_save_stops_the_recall_so_no_edit_is_lost(self):
        self.pedal.save_error = DeviceStatusError(0x46, 3)
        await self.edit(VOLUME)
        response = await self.post("/api/preset", {"display_number": 4})
        self.assertEqual(response.status, 502)
        self.assertEqual((await self.get("/api/state")).status, 200)
        self.assertEqual(self.pedal.runtime[1], 8)

    async def test_pending_edits_are_stored_on_shutdown(self):
        await self.edit(VOLUME)
        await self.client.close()
        self.assertEqual(self.pedal.saved_slots, [8])
        self.assertEqual(self.pedal.close_count, 1)

    async def test_shutdown_waits_for_a_save_in_flight(self):
        self.pedal.save_gate = asyncio.Event()
        await self.edit(VOLUME)
        await self.clock.advance(3.1)
        await asyncio.wait_for(self.pedal.save_started.wait(), 2)
        closing = asyncio.ensure_future(self.client.close())
        await settle()
        await asyncio.sleep(0.02)
        self.assertFalse(closing.done())
        self.assertEqual(self.pedal.close_count, 0)  # the link stays up until the write is done
        self.pedal.save_gate.set()
        await asyncio.wait_for(closing, 5)
        self.assertEqual(self.pedal.saves, 1)  # saved once, not twice
        self.assertEqual(self.server.autosaver.state, "saved")
        self.assertEqual(self.pedal.close_count, 1)

    async def test_a_link_lost_during_a_save_is_an_error_and_recovers_after_reconnecting(self):
        await self.edit(VOLUME)
        self.pedal.fail_connect = DeviceNotFound("pedal is off")
        self.pedal.drop()
        await wait_until(lambda: not self.server.connected)
        await self.clock.advance(3.1)
        await self.wait_for_state("error")
        self.pedal.fail_connect = None
        await self.clock.advance(30)  # the reconnect backoff
        await wait_until(lambda: self.server.connected)
        await self.clock.advance(3.1)
        await self.wait_for_state("saved")
        self.assertEqual(self.pedal.saved_slots, [8])


class AutosaveDisabledTest(ServerTestCase):
    options_kwargs = {"autosave": False}

    async def test_edits_stay_live_and_dirty(self):
        await self.edit(VOLUME)
        await self.clock.advance(1000)
        self.assertEqual(self.pedal.saves, 0)
        self.assertEqual(self.server.autosaver.state, "dirty")
        await self.client.close()
        self.assertEqual(self.pedal.saves, 0)


class BaselineGuardTest(ServerTestCase):
    async def test_a_dirty_slot_without_a_baseline_is_never_written_to_flash(self):
        self.pedal.switch_to_slot(3)  # a preset the server has not recorded a baseline for
        self.server.autosaver.mark_dirty(3)  # an edit path that skipped ensure_baseline

        saved = await self.server.autosaver.flush()

        self.assertFalse(saved)
        self.assertEqual(self.pedal.saves, 0)
        self.assertEqual(self.server.autosaver.state, "error")
        self.assertIn("no baseline", self.server.autosaver.snapshot()["error"])
