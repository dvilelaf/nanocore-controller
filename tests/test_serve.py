import asyncio
import contextlib
import dataclasses
import json
import unittest
from unittest import mock

from aiohttp import WSMsgType, WSServerHandshakeError

from nanocore_controller import serve
from nanocore_controller.errors import (
    BackupError,
    DeviceDisconnected,
    DeviceNotFound,
    DeviceStatusError,
    DeviceTimeout,
    PartialApplyError,
    ProtocolError,
    SlotChangedError,
    ValidationError,
    VerificationError,
)
from support_server import TOKEN, ServerTestCase, settle, wait_until


def param(effect=0, index=0, value=0.5):
    return {"op": "param", "effect": effect, "index": index, "value": value}


class SecurityTest(ServerTestCase):
    def url(self, host="127.0.0.1"):
        return f"{host}:{self.port}"

    async def test_unknown_host_is_forbidden_even_for_health(self):
        for host in ("evil.example", f"evil.example:{self.port}", "127.0.0.1", "127.0.0.1:1", f"[::1]:{self.port}"):
            response = await self.client.get("/api/health", headers={"Host": host})
            self.assertEqual(response.status, 403, host)
            self.assertEqual((await response.json())["error"]["code"], "forbidden")

    async def test_localhost_and_loopback_hosts_are_allowed(self):
        for host in (self.url(), self.url("localhost")):
            response = await self.client.get("/api/health", headers={"Host": host})
            self.assertEqual(response.status, 200, host)

    async def test_origin_must_equal_the_host(self):
        bad = ("http://evil.example", "null", f"http://localhost:{self.port}", f"https://127.0.0.1:{self.port}")
        for origin in bad:
            response = await self.get("/api/state", headers={**self.auth, "Origin": origin})
            self.assertEqual(response.status, 403, origin)
        good = await self.get("/api/state", headers={**self.auth, "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(good.status, 200)

    async def test_a_foreign_origin_cannot_write_even_with_the_token(self):
        response = await self.post(
            "/api/edit",
            {"ops": [{"op": "volume", "value": 10}]},
            headers={**self.auth, "Origin": "http://evil.example"},
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(self.pedal.live_writes, [])

    async def test_token_is_required_on_every_api_route_but_health(self):
        routes = [
            ("GET", "/api/state"),
            ("GET", "/api/presets"),
            ("GET", "/api/assets"),
            ("GET", "/api/models"),
            ("GET", "/api/models/ir/0"),
            ("POST", "/api/models/ir/0"),
            ("GET", "/api/baselines"),
            ("POST", "/api/preset"),
            ("POST", "/api/edit"),
            ("POST", "/api/baselines/restore"),
            ("POST", "/api/save-now"),
            ("GET", "/api/settings"),
            ("POST", "/api/settings"),
            ("GET", "/api/preset-file"),
            ("POST", "/api/preset-file"),
            ("GET", "/api/nothing-here"),
        ]
        for headers in ({}, {"X-Nanocore-Token": "wrong"}, {"X-Nanocore-Token": TOKEN + "x"}, {"X-Nanocore-Token": "é"}):
            for method, path in routes:
                response = await self.client.request(method, path, headers=headers, json={})
                self.assertEqual(response.status, 401, (method, path, headers))
                self.assertEqual((await response.json())["error"]["code"], "unauthorized")
        self.assertEqual((await self.client.get("/api/health")).status, 200)
        self.assertEqual(self.pedal.live_writes, [])

    async def test_the_token_is_compared_in_constant_time(self):
        with mock.patch.object(serve.hmac, "compare_digest", wraps=serve.hmac.compare_digest) as compare:
            await self.get("/api/state")
        compare.assert_called()

    async def test_every_response_names_the_api_version(self):
        responses = [
            await self.client.get("/api/health"),
            await self.client.get("/api/state"),  # 401
            await self.client.get("/api/health", headers={"Host": "evil.example"}),  # 403
            await self.get("/api/state"),
            await self.client.get("/"),
            await self.client.get("/missing.js"),  # 404
            await self.client.delete("/api/state", headers=self.auth),  # 405
        ]
        for response in responses:
            self.assertEqual(response.headers.get("X-Nanocore-Api"), "1", response.status)

    async def test_no_cors_headers_and_no_preflight(self):
        response = await self.get("/api/state", headers={**self.auth, "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertFalse([h for h in response.headers if h.lower().startswith("access-control")])
        preflight = await self.client.options("/api/edit", headers={"Origin": "http://evil.example"})
        self.assertIn(preflight.status, (403, 405))
        self.assertFalse([h for h in preflight.headers if h.lower().startswith("access-control")])

    async def test_static_page_needs_no_token_and_leaks_nothing(self):
        response = await self.client.get("/")
        text = await response.text()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "text/html")
        for secret in (TOKEN, "Preset9", "volume"):
            self.assertNotIn(secret, text)
        self.assertIn("Content-Security-Policy", response.headers)

    async def test_static_dir_is_served_and_cannot_be_escaped(self):
        site = self.tmp / "site"
        site.mkdir()
        (site / "index.html").write_text("<p>editor</p>")
        (site / "app.js").write_text("1")
        (site / ".secret").write_text("no")
        (self.tmp / "outside.txt").write_text("outside")
        self.options.static_dir = site
        self.assertEqual(await (await self.client.get("/")).text(), "<p>editor</p>")
        self.assertEqual(await (await self.client.get("/app.js")).text(), "1")
        self.assertEqual(await (await self.client.get("/some/route")).text(), "<p>editor</p>")
        for path in ("/.secret", "/%2e%2e/outside.txt", "/..%2foutside.txt", "/missing.png"):
            response = await self.client.get(path)
            self.assertEqual(response.status, 404, path)
            self.assertNotIn("outside", await response.text())

    async def test_only_json_bodies_are_accepted(self):
        response = await self.client.post(
            "/api/edit", data=b'{"ops": []}', headers={**self.auth, "Content-Type": "text/plain"}
        )
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"]["code"], "bad_request")

    async def test_malformed_json_and_non_finite_numbers_are_rejected(self):
        for body in (b"{", b"[]", b"null", b'{"ops": [{"op": "volume", "value": NaN}]}', b"[" * 5000):
            response = await self.client.post(
                "/api/edit", data=body, headers={**self.auth, "Content-Type": "application/json"}
            )
            self.assertEqual(response.status, 400, body[:30])
        self.assertEqual(self.pedal.live_writes, [])

    async def test_oversized_bodies_are_rejected_with_413(self):
        body = b'{"ops": "' + b"x" * 70_000 + b'"}'
        response = await self.client.post(
            "/api/edit", data=body, headers={**self.auth, "Content-Type": "application/json"}
        )
        self.assertEqual(response.status, 413)
        self.assertEqual((await response.json())["error"]["code"], "too_large")

    async def test_at_most_64_operations_per_edit(self):
        ok = await self.edit(*[{"op": "volume", "value": 5}] * 64)
        self.assertEqual(ok.status, 202)
        response = await self.edit(*[{"op": "volume", "value": 5}] * 65)
        self.assertEqual(response.status, 400)

    async def test_errors_never_leak_tracebacks_or_paths(self):
        with mock.patch.object(
            self.device, "read_live", side_effect=RuntimeError("boom in /home/david/secret/file.py")
        ):
            response = await self.get("/api/state")
        body = await response.json()
        self.assertEqual(response.status, 500)
        self.assertEqual(body, {"error": {"code": "internal", "message": "internal error"}})

    def test_typed_errors_map_to_the_documented_statuses(self):
        cases = [
            (ValidationError("bad"), 400, "validation", None),
            (SlotChangedError(1, 2), 409, "slot_changed", None),
            (DeviceStatusError(0x6D, 3), 502, "device_status", None),
            (DeviceDisconnected("x"), 503, "disconnected", None),
            (DeviceNotFound("no /dev/snd/midiC1D0"), 503, "disconnected", None),
            (DeviceTimeout(0x6D, maybe_applied=True), 504, "device_timeout", True),
            (DeviceTimeout(0x63, maybe_applied=False), 504, "device_timeout", False),
            (VerificationError("mismatch"), 502, "verification_failed", None),
            (ProtocolError("junk"), 502, "protocol", None),
        ]
        for exc, status, code, maybe in cases:
            error = serve.api_error_for(exc)
            self.assertEqual((error.status, error.code, error.maybe_applied), (status, code, maybe), exc)

    def test_paths_are_redacted_from_messages(self):
        partial = PartialApplyError(1, 4, serve.Path("/home/david/.local/share/x/backup.json"))
        body = serve.api_error_for(partial).body()
        self.assertNotIn("/home", json.dumps(body))
        body = serve.api_error_for(BackupError("cannot write /home/david/x/y.json")).body()
        self.assertNotIn("/home", json.dumps(body))
        self.assertNotIn("/dev/snd", serve.ApiError(500, "x", "open /dev/snd/midiC1D0 failed").body()["error"]["message"])

    def test_device_text_is_cleaned_and_truncated(self):
        self.assertEqual(serve.clean_text("A\x1b[31mB\x00\n‮C"), "A[31mBC")
        self.assertEqual(len(serve.clean_text("x" * 100)), 32)
        self.assertEqual(serve.clean_text("  pad "), "pad")

    async def test_preset_names_leave_the_server_without_control_characters(self):
        self.pedal.names[9] = "A\x1bB\x07C"
        await self.get("/api/presets")  # cached already: force a refresh
        self.server._names = {}
        presets = (await (await self.get("/api/presets")).json())["presets"]
        names = {p["slot"]: p["name"] for p in presets}
        self.assertEqual(names[9], "ABC")


class ReadOnlyTest(ServerTestCase):
    options_kwargs = {"read_only": True}

    async def test_every_write_is_rejected_even_when_malformed(self):
        for path in ("/api/edit", "/api/preset", "/api/baselines/restore", "/api/save-now", "/api/settings", "/api/preset-file", "/api/models/ir/0", "/api/models/amp/0"):
            for payload in ({"ops": [{"op": "volume", "value": 1}]}, {}, {"junk": 1}):
                response = await self.post(path, payload)
                self.assertEqual(response.status, 403, path)
                self.assertEqual((await response.json())["error"]["code"], "read_only")
        self.assertEqual(self.pedal.live_writes, [])
        self.assertEqual(self.pedal.sent, [])

    async def test_reads_still_work(self):
        state = await (await self.get("/api/state")).json()
        self.assertTrue(state["read_only"])
        self.assertEqual((await self.get("/api/presets")).status, 200)

    async def test_websocket_edits_are_rejected(self):
        ws = await self.connect_ws()
        await ws.send_json({"type": "edit", "client_seq": 3, "ops": [{"op": "volume", "value": 1}]})
        message = await ws.receive_json(timeout=2)
        self.assertEqual(message["type"], "error")
        self.assertEqual(message["error"]["code"], "read_only")
        self.assertEqual(self.pedal.live_writes, [])


class EndpointTest(ServerTestCase):
    async def test_health(self):
        self.assertEqual(await (await self.client.get("/api/health")).json(), {"ok": True, "token_required": True})

    async def test_state_document(self):
        state = await (await self.get("/api/state")).json()
        self.assertEqual(state["preset"], {"slot": 8, "display_number": 9, "name": "Preset9"})
        self.assertTrue(state["connected"])
        self.assertEqual(state["transport"], "bluetooth")
        self.assertFalse(state["read_only"])
        self.assertEqual(state["live"]["volume"], 84)
        self.assertEqual(state["live"]["chain_order"], list(range(8)))
        self.assertEqual(len(state["live"]["effects"]), 8)
        effect = state["live"]["effects"][0]
        self.assertEqual(effect, {"index": 0, "effect_id": 7, "enabled": True, "variant": 0, "params": [0.5, 0.25]})
        self.assertEqual(state["autosave"]["state"], "saved")

    async def test_the_catalog_and_assets_are_cached(self):
        await self.get("/api/presets")
        await self.get("/api/state")
        catalog_reads = [q for q in self.pedal.queries if q[0] == 0x40]
        presets = (await (await self.get("/api/presets")).json())["presets"]
        self.assertEqual(presets[0], {"slot": 0, "display_number": 1, "name": "Preset1"})
        assets = await (await self.get("/api/assets")).json()
        self.assertEqual(assets["amp"]["slot"], 4)
        self.assertEqual(assets["ir"]["slot"], 5)
        before = len(self.pedal.queries)
        await self.get("/api/assets")
        await self.get("/api/presets")
        self.assertEqual(len(self.pedal.queries), before)
        self.assertEqual([q for q in self.pedal.queries if q[0] == 0x40][: len(catalog_reads)], catalog_reads)

    async def test_state_reads_live_state_but_not_the_catalog(self):
        before = len([q for q in self.pedal.queries if q[0] == 0x40])
        await self.get("/api/state")
        await self.get("/api/state")
        self.assertEqual(len([q for q in self.pedal.queries if q[0] == 0x40]), before)

    async def test_recall_preset_returns_the_new_state_and_broadcasts_it(self):
        ws = await self.connect_ws()
        response = await self.post("/api/preset", {"display_number": 4})
        state = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(state["preset"]["slot"], 3)
        self.assertEqual(state["preset"]["name"], "Preset4")
        message = await ws.receive_json(timeout=2)
        self.assertEqual((message["type"], message["state"]["preset"]["slot"]), ("state", 3))

    async def test_recall_preset_validation(self):
        for payload in ({"display_number": 0}, {"display_number": 129}, {"display_number": True},
                        {"display_number": "4"}, {"display_number": 4.0}, {}, {"display_number": 4, "x": 1}):
            response = await self.post("/api/preset", payload)
            self.assertEqual(response.status, 400, payload)
        self.assertEqual(self.pedal.live_writes, [])

    async def test_baselines_list_and_restore(self):
        await self.edit({"op": "volume", "value": 10})
        listing = await (await self.get("/api/baselines?slot=8")).json()
        self.assertEqual(len(listing), 1)
        self.assertEqual(set(listing[0]), {"id", "captured_at", "name"})
        self.assertEqual((await self.get("/api/baselines")).status, 200)  # defaults to the active slot
        response = await self.post("/api/baselines/restore", {"slot": 8, "id": listing[0]["id"]})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["live"]["volume"], 84)
        self.assertEqual(self.server.autosaver.state, "dirty")

    async def test_baseline_endpoints_reject_bad_input(self):
        for query in ("slot=x", "slot=-1", "slot=128", "slot=1.5", "slot="):
            self.assertEqual((await self.get("/api/baselines?" + query)).status, 400, query)
        for payload in ({"slot": 8, "id": "../../etc/passwd"}, {"slot": "8", "id": "x"}, {"slot": 8}, {"id": "20260101T000000000000Z"}):
            self.assertEqual((await self.post("/api/baselines/restore", payload)).status, 400, payload)
        missing = await self.post("/api/baselines/restore", {"slot": 8, "id": "20260101T000000000000Z"})
        self.assertEqual(missing.status, 404)
        other = await self.post("/api/baselines/restore", {"slot": 3, "id": "20260101T000000000000Z"})
        self.assertEqual(other.status, 409)


class EditValidationTest(ServerTestCase):
    async def assert_rejected(self, *ops, **extra):
        response = await self.edit(*ops, **extra)
        self.assertEqual(response.status, 400, (ops, extra))
        self.assertEqual(self.pedal.live_writes, [], (ops, extra))
        self.assertEqual(self.pedal.sent, [])

    async def test_bad_values_for_every_operation(self):
        nan = float("nan")
        bad = [
            {"op": "nope"},
            {"op": 3},
            {},
            {"op": "param", "effect": 8, "index": 0, "value": 0.5},
            {"op": "param", "effect": -1, "index": 0, "value": 0.5},
            {"op": "param", "effect": True, "index": 0, "value": 0.5},
            {"op": "param", "effect": 0.0, "index": 0, "value": 0.5},
            {"op": "param", "effect": "0", "index": 0, "value": 0.5},
            {"op": "param", "effect": 0, "index": 24, "value": 0.5},
            {"op": "param", "effect": 0, "index": None, "value": 0.5},
            {"op": "param", "effect": 0, "index": 0, "value": 1.5},
            {"op": "param", "effect": 0, "index": 0, "value": -0.1},
            {"op": "param", "effect": 0, "index": 0, "value": True},
            {"op": "param", "effect": 0, "index": 0, "value": "0.5"},
            {"op": "param", "effect": 0, "index": 0, "value": None},
            {"op": "param", "effect": 0, "index": 0, "value": [0.5]},
            {"op": "param", "effect": 0, "index": 0, "value": 1e999},
            {"op": "param", "effect": 0, "index": 0},
            {"op": "param", "effect": 0, "index": 0, "value": 0.5, "extra": 1},
            {"op": "enabled", "effect": 0, "enabled": 1},
            {"op": "enabled", "effect": 0, "enabled": "true"},
            {"op": "enabled", "effect": 9, "enabled": True},
            {"op": "variant", "effect": 0, "variant": 256, "params": []},
            {"op": "variant", "effect": 0, "variant": -1, "params": []},
            {"op": "variant", "effect": 0, "variant": 1, "params": [0.1] * 25},
            {"op": "variant", "effect": 0, "variant": 1, "params": [2.0]},
            {"op": "variant", "effect": 0, "variant": 1, "params": ["0.1"]},
            {"op": "variant", "effect": 0, "variant": 1, "params": [True]},
            {"op": "variant", "effect": 0, "variant": 1, "params": "0.1"},
            {"op": "variant", "effect": 0, "variant": 1},
            {"op": "volume", "value": 101},
            {"op": "volume", "value": -1},
            {"op": "volume", "value": 50.0},
            {"op": "volume", "value": True},
            {"op": "volume", "value": "50"},
            {"op": "chain_order", "order": [0, 1, 2, 3, 4, 5, 6]},
            {"op": "chain_order", "order": [0, 1, 2, 3, 4, 5, 6, 6]},
            {"op": "chain_order", "order": [0, 1, 2, 3, 4, 5, 6, 8]},
            {"op": "chain_order", "order": [0, 1, 2, 3, 4, 5, 6, True]},
            {"op": "chain_order", "order": [0.0, 1, 2, 3, 4, 5, 6, 7]},
            {"op": "chain_order", "order": "01234567"},
            {"op": "amp", "slot": 30},
            {"op": "amp", "slot": -1},
            {"op": "amp", "slot": True},
            {"op": "ir", "slot": "1"},
            {"op": "ir", "slot": 1.5},
        ]
        for op in bad:
            await self.assert_rejected(op)
        raw = b'{"ops": [{"op": "param", "effect": 0, "index": 0, "value": %s}]}'
        for literal in (b"NaN", b"Infinity", b"-Infinity"):
            response = await self.client.post(
                "/api/edit", data=raw % literal, headers={**self.auth, "Content-Type": "application/json"}
            )
            self.assertEqual(response.status, 400, literal)
        del nan
        self.assertEqual(self.pedal.live_writes, [])

    async def test_bad_envelope(self):
        await self.assert_rejected()
        for body in (
            {"ops": "x"},
            {"ops": {}},
            {"ops": [[]]},
            {"ops": [{"op": "volume", "value": 1}], "client_seq": "1"},
            {"ops": [{"op": "volume", "value": 1}], "client_seq": -1},
            {"ops": [{"op": "volume", "value": 1}], "client_seq": True},
            {"ops": [{"op": "volume", "value": 1}], "slot": 128},
            {"ops": [{"op": "volume", "value": 1}], "unknown": 1},
        ):
            response = await self.post("/api/edit", body)
            self.assertEqual(response.status, 400, body)
        self.assertEqual(self.pedal.live_writes, [])

    async def test_one_bad_operation_means_none_are_sent(self):
        await self.assert_rejected({"op": "volume", "value": 20}, {"op": "volume", "value": 999})

    async def test_every_valid_operation_reaches_the_pedal(self):
        response = await self.edit(
            param(1, 2, 0.75),
            {"op": "enabled", "effect": 3, "enabled": True},
            {"op": "variant", "effect": 2, "variant": 5, "params": [0.1, 0.2]},
            {"op": "volume", "value": 42},
            {"op": "chain_order", "order": [7, 6, 5, 4, 3, 2, 1, 0]},
            {"op": "amp", "slot": 11},
            {"op": "ir", "slot": 12},
            client_seq=17,
        )
        self.assertEqual(response.status, 202)
        body = await response.json()
        self.assertEqual(body["applied"], 7)
        self.assertEqual([w[0] for w in self.pedal.live_writes], [1, 3, 2, 4, 5, 6, 7])
        state = await (await self.get("/api/state")).json()
        self.assertEqual(state["live"]["effects"][2], {"index": 2, "effect_id": 1, "enabled": True, "variant": 5, "params": [0.1, 0.2]})
        self.assertEqual(state["live"]["volume"], 42)
        self.assertEqual(state["live"]["chain_order"], [7, 6, 5, 4, 3, 2, 1, 0])
        self.assertTrue(state["live"]["effects"][3]["enabled"])
        self.assertEqual(state["rev"], body["rev"])

    async def test_a_slot_guard_protects_against_editing_the_wrong_preset(self):
        response = await self.edit({"op": "volume", "value": 1}, slot=3)
        self.assertEqual(response.status, 409)
        self.assertEqual((await response.json())["error"]["code"], "slot_changed")
        self.assertEqual(self.pedal.live_writes, [])
        self.assertEqual((await self.edit({"op": "volume", "value": 1}, slot=8)).status, 202)

    async def test_a_failure_stops_the_request_and_reports_how_many_were_applied(self):
        self.pedal.fail_live_write[2] = DeviceTimeout(0x6D, maybe_applied=True)
        response = await self.edit(*[{"op": "volume", "value": v} for v in (1, 2, 3)])
        body = await response.json()
        self.assertEqual(response.status, 504)
        self.assertEqual(body["error"]["code"], "device_timeout")
        self.assertTrue(body["error"]["maybe_applied"])
        self.assertEqual(body["error"]["applied"], 1)
        self.assertEqual(len(self.pedal.live_writes), 2)  # the third was never sent

    async def test_device_status_errors_are_bad_gateway(self):
        self.pedal.fail_live_write[1] = DeviceStatusError(0x6D, 3)
        response = await self.edit({"op": "volume", "value": 1})
        self.assertEqual(response.status, 502)
        self.assertEqual((await response.json())["error"]["code"], "device_status")


class RateAndCoalescingTest(ServerTestCase):
    async def hold_writer(self):
        release = asyncio.Event()

        async def hold():
            await release.wait()

        blocker = asyncio.ensure_future(self.server.writer.submit(hold))
        await asyncio.sleep(0.01)
        return release, blocker

    async def test_thirty_param_edits_per_second_per_client(self):
        for i in range(30):
            response = await self.edit(param(i % 8, i // 8, 0.5))
            self.assertEqual(response.status, 202, i)
        response = await self.edit(param(0, 5, 0.5))
        self.assertEqual(response.status, 429)
        self.assertEqual((await response.json())["error"]["code"], "rate_limited")
        await self.clock.advance(1.0)
        self.assertEqual((await self.edit(param(0, 5, 0.5))).status, 202)

    async def test_a_request_bigger_than_the_burst_uses_up_the_whole_budget(self):
        response = await self.edit(*[param(i % 8, i // 8, 0.1) for i in range(40)])
        self.assertEqual(response.status, 202)  # a full patch must be able to go through once
        again = await self.edit(param(0, 5, 0.5))
        self.assertEqual(again.status, 429)
        await self.clock.advance(1.0)
        self.assertEqual((await self.edit(param(0, 5, 0.5))).status, 202)

    async def test_operations_that_are_not_parameters_are_not_free(self):
        for i in range(30):
            self.assertEqual((await self.edit({"op": "volume", "value": i})).status, 202, i)
        response = await self.edit({"op": "volume", "value": 31})
        self.assertEqual(response.status, 429)

    async def test_websocket_edits_are_rate_limited_per_connection(self):
        ws = await self.connect_ws()
        for i in range(31):
            await ws.send_json({"type": "edit", "client_seq": i, "ops": [param(i % 8, i // 8, 0.5)]})
        errors = []
        while len(errors) < 1:
            message = await ws.receive_json(timeout=3)
            if message["type"] == "error":
                errors.append(message)
        self.assertEqual(errors[0]["error"]["code"], "rate_limited")
        self.assertEqual(errors[0]["client_seq"], 30)

    async def test_queued_values_for_the_same_parameter_collapse_to_the_latest(self):
        release, blocker = await self.hold_writer()
        tasks = [asyncio.ensure_future(self.edit(param(2, 1, v / 10))) for v in range(1, 6)]
        await wait_until(lambda: len(self.server.writer.queued()) == 5)
        last = self.server.writer.queued()[-1].edit.ops[0]["value"]
        release.set()
        await blocker
        responses = await asyncio.gather(*tasks)
        self.assertEqual([r.status for r in responses], [202] * 5)
        writes = self.param_writes()
        self.assertEqual(len(writes), 1)
        import struct

        self.assertAlmostEqual(struct.unpack("<f", writes[0][3:7])[0], last, places=5)

    async def test_different_parameters_are_not_collapsed(self):
        release, blocker = await self.hold_writer()
        tasks = [asyncio.ensure_future(self.edit(param(0, i, 0.5))) for i in range(3)]
        await wait_until(lambda: len(self.server.writer.queued()) == 3)
        release.set()
        await blocker
        await asyncio.gather(*tasks)
        self.assertEqual(len(self.param_writes()), 3)

    async def test_a_preset_change_is_a_barrier_for_coalescing(self):
        release, blocker = await self.hold_writer()
        first = asyncio.ensure_future(self.edit(param(0, 0, 0.1)))
        await wait_until(lambda: len(self.server.writer.queued()) == 1)
        recall = asyncio.ensure_future(self.post("/api/preset", {"display_number": 4}))
        await wait_until(lambda: len(self.server.writer.queued()) >= 2)
        second = asyncio.ensure_future(self.edit(param(0, 0, 0.9)))
        await wait_until(lambda: len(self.server.writer.queued()) >= 3)
        release.set()
        await wait_until(lambda: bool(self.clock.sleepers))
        await self.clock.advance(0.05)
        await asyncio.gather(blocker, first, recall, second)
        self.assertEqual(len(self.param_writes()), 2)

    async def test_consecutive_params_in_one_request_collapse(self):
        response = await self.edit(param(0, 0, 0.1), param(0, 0, 0.2), param(0, 0, 0.3))
        self.assertEqual((await response.json())["applied"], 3)
        self.assertEqual(len(self.param_writes()), 1)

    async def test_a_parameter_is_sent_at_most_about_thirty_times_per_second(self):
        self.assertEqual((await self.edit(param(0, 0, 0.1))).status, 202)
        second = asyncio.ensure_future(self.edit(param(0, 0, 0.2)))
        await wait_until(lambda: bool(self.clock.sleepers))
        self.assertEqual(len(self.param_writes()), 1)
        await self.clock.advance(0.05)
        self.assertEqual((await second).status, 202)
        self.assertEqual(len(self.param_writes()), 2)


class CcTest(ServerTestCase):
    def cc(self, number, value=64):
        return {"op": "cc", "cc": number, "value": value}

    async def test_allow_listed_controllers_are_forwarded_on_channel_one(self):
        for number in (20, 28, 40, 48, 50, 60, 80, 93):
            await self.clock.advance(1)
            response = await self.edit(self.cc(number, 127))
            self.assertEqual(response.status, 202, number)
        self.assertEqual(
            self.pedal.sent,
            [bytes((0xB0, n, 127)) for n in (20, 28, 40, 48, 50, 60, 80, 93)],
        )

    def test_the_allow_list_is_built_from_the_documented_tables(self):
        self.assertIn(80, serve.ALLOWED_CC)
        self.assertEqual(
            serve.ALLOWED_CC & {0, 1, 7, 19, 32, 79, 81, 82, 94, 100, 127}, set()
        )

    async def test_other_controllers_are_rejected(self):
        for number in (0, 1, 19, 32, 79, 81, 82, 94, 127, 128, -1, 20.0, True, "20", None):
            response = await self.edit(self.cc(number))
            self.assertEqual(response.status, 400, number)
            self.assertEqual((await response.json())["error"]["code"], "validation")
        self.assertEqual(self.pedal.sent, [])

    async def test_values_and_fields_are_strict(self):
        for bad in (128, -1, 1.5, 64.0, True, "64", None):
            self.assertEqual((await self.edit(self.cc(20, bad))).status, 400, bad)
        for op in ({"op": "cc", "cc": 20}, {"op": "cc", "value": 1}, {"op": "cc", "cc": 20, "value": 1, "channel": 2},
                   {"op": "cc", "cc": 20, "value": 1, "raw": "b0 14 01"}):
            self.assertEqual((await self.edit(op)).status, 400, op)
        self.assertEqual(self.pedal.sent, [])

    async def test_cc_counts_towards_the_rate_limit(self):
        numbers = sorted(serve.ALLOWED_CC)
        for number in numbers[:30]:
            self.assertEqual((await self.edit(self.cc(number))).status, 202, number)
        limited = await self.edit(self.cc(numbers[30]))
        self.assertEqual(limited.status, 429)

    async def test_queued_values_for_the_same_controller_collapse(self):
        release = asyncio.Event()

        async def hold():
            await release.wait()

        blocker = asyncio.ensure_future(self.server.writer.submit(hold))
        await asyncio.sleep(0.01)
        tasks = [asyncio.ensure_future(self.edit(self.cc(60, v))) for v in (10, 20, 30)]
        await wait_until(lambda: len(self.server.writer.queued()) == 3)
        last = self.server.writer.queued()[-1].edit.ops[0]["value"]
        release.set()
        await blocker
        await asyncio.gather(*tasks)
        self.assertEqual(self.pedal.sent, [bytes((0xB0, 60, last))])

    async def test_cc_edits_record_a_baseline_and_make_the_state_dirty(self):
        seen = []
        original = self.pedal.send

        async def spy(message):
            seen.append(len(self.server.baselines.list(8)))
            await original(message)

        self.pedal.send = spy
        await self.edit(self.cc(60))
        self.assertEqual(seen, [1])
        self.assertEqual(self.server.autosaver.state, "dirty")

    async def test_cc_is_rejected_in_a_read_only_server(self):
        self.options.read_only = True
        response = await self.edit(self.cc(60))
        self.assertEqual(response.status, 403)
        self.assertEqual(self.pedal.sent, [])


class WebSocketTest(ServerTestCase):
    async def test_the_first_message_must_authenticate_within_the_deadline(self):
        self.server.auth_timeout = 0.05
        ws = await self.client.ws_connect("/ws")
        message = await ws.receive(timeout=2)
        self.assertEqual(message.type, WSMsgType.CLOSE)
        self.assertEqual(message.data, 4401)

    async def test_wrong_token_or_wrong_first_message_closes_with_4401(self):
        for first in ({"type": "auth", "token": "nope"}, {"type": "auth"}, {"type": "edit", "ops": []},
                      {"type": "auth", "token": 5}, [1, 2]):
            ws = await self.client.ws_connect("/ws")
            await ws.send_json(first)
            message = await ws.receive(timeout=2)
            self.assertEqual((message.type, message.data), (WSMsgType.CLOSE, 4401), first)
        ws = await self.client.ws_connect("/ws")
        await ws.send_bytes(b"\x00")
        self.assertEqual((await ws.receive(timeout=2)).data, 4401)

    async def test_unauthenticated_sockets_get_no_state(self):
        ws = await self.client.ws_connect("/ws")
        await ws.send_json({"type": "auth", "token": "wrong"})
        message = await ws.receive(timeout=2)
        self.assertNotEqual(message.type, WSMsgType.TEXT)

    async def test_host_and_origin_are_checked_before_the_upgrade(self):
        with self.assertRaises(WSServerHandshakeError) as caught:
            await self.client.ws_connect("/ws", headers={"Origin": "http://evil.example"})
        self.assertEqual(caught.exception.status, 403)
        with self.assertRaises(WSServerHandshakeError):
            await self.client.ws_connect("/ws", headers={"Host": "evil.example"})

    async def test_authenticated_clients_get_the_full_state_first(self):
        ws = await self.client.ws_connect("/ws")
        await ws.send_json({"type": "auth", "token": TOKEN})
        message = await ws.receive_json(timeout=2)
        self.assertEqual(message["type"], "state")
        self.assertEqual(message["rev"], message["state"]["rev"])
        self.assertEqual(message["state"]["preset"]["slot"], 8)

    async def test_edits_from_http_become_patches_with_increasing_revisions(self):
        ws = await self.connect_ws()
        response = await self.edit({"op": "volume", "value": 33}, client_seq=5)
        rev = (await response.json())["rev"]
        message = await ws.receive_json(timeout=2)
        self.assertEqual(message["type"], "patch")
        self.assertEqual(message["rev"], rev)
        self.assertEqual(message["ops"], [{"op": "volume", "value": 33}])

    async def test_edits_over_the_socket_are_applied_and_reach_other_clients_only(self):
        sender = await self.connect_ws()
        other = await self.connect_ws()
        await sender.send_json({"type": "edit", "client_seq": 1, "ops": [{"op": "volume", "value": 12}]})
        message = await other.receive_json(timeout=2)
        self.assertEqual((message["type"], message["ops"]), ("patch", [{"op": "volume", "value": 12}]))
        self.assertEqual([w for w in self.pedal.live_writes if w[0] == 4], [bytes((4, 12))])
        await sender.send_json({"type": "edit", "client_seq": 2, "ops": [{"op": "volume", "value": 999}]})
        error = await sender.receive_json(timeout=2)
        while error["type"] != "error":
            error = await sender.receive_json(timeout=2)
        self.assertEqual((error["type"], error["client_seq"], error["error"]["code"]), ("error", 2, "validation"))

    async def test_unknown_messages_get_an_error_not_a_crash(self):
        ws = await self.connect_ws()
        for message in ({"type": "unknown"}, [1], "text"):
            await ws.send_json(message)
            reply = await ws.receive_json(timeout=2)
            self.assertEqual(reply["type"], "error")
        await ws.send_str("{not json")
        self.assertEqual((await ws.receive_json(timeout=2))["error"]["code"], "bad_request")

    async def test_oversized_messages_close_the_socket(self):
        ws = await self.connect_ws()
        await ws.send_str(json.dumps({"type": "edit", "pad": "x" * 70_000}))
        message = await ws.receive(timeout=2)
        self.assertEqual(message.type, WSMsgType.CLOSE)
        self.assertEqual(message.data, 1009)

    async def test_autosave_messages(self):
        ws = await self.connect_ws()
        await self.edit({"op": "volume", "value": 33})
        await ws.receive_json(timeout=2)  # patch
        dirty = await ws.receive_json(timeout=2)
        self.assertEqual(dirty, {"type": "autosave", "state": "dirty", "detail": None})
        await self.clock.advance(3.5)
        states = []
        while "saved" not in states:
            message = await ws.receive_json(timeout=2)
            if message["type"] == "autosave":
                states.append(message["state"])
        self.assertEqual(states, ["saving", "saved"])


class ConnectionTest(ServerTestCase):
    async def drop(self):
        self.pedal.drop()
        await wait_until(lambda: not self.server.connected)

    async def test_a_dropped_link_is_reported_and_writes_get_503(self):
        ws = await self.connect_ws()
        await self.drop()
        message = await ws.receive_json(timeout=2)
        self.assertEqual((message["type"], message["connected"]), ("connection", False))
        state = await (await self.get("/api/state")).json()
        self.assertFalse(state["connected"])
        response = await self.edit({"op": "volume", "value": 1})
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())["error"]["code"], "disconnected")
        self.assertEqual((await self.post("/api/preset", {"display_number": 2})).status, 503)

    async def test_it_reconnects_with_exponential_backoff_capped_at_30_seconds(self):
        await self.drop()
        self.pedal.fail_connect = DeviceNotFound("gone")
        waits = []
        for _ in range(8):
            await wait_until(lambda: bool(self.clock.sleepers))
            deadline, _future = self.clock.sleepers[0]
            waits.append(round(deadline - self.clock.now, 3))
            await self.clock.advance(waits[-1])
        self.assertEqual(waits, [1, 2, 4, 8, 16, 30, 30, 30])
        self.assertFalse(self.server.connected)

    async def test_after_reconnecting_a_full_state_is_broadcast(self):
        ws = await self.connect_ws()
        await self.drop()
        await ws.receive_json(timeout=2)
        self.pedal.switch_to_slot(2)
        await wait_until(lambda: bool(self.clock.sleepers))
        await self.clock.advance(1.0)
        await wait_until(lambda: self.server.connected)
        kinds = []
        while "state" not in kinds:
            message = await ws.receive_json(timeout=2)
            kinds.append(message["type"])
        self.assertEqual(kinds, ["connection", "state"])
        self.assertEqual(message["state"]["preset"]["slot"], 2)
        self.assertEqual((await self.edit({"op": "volume", "value": 1})).status, 202)

    async def test_a_failing_write_marks_the_link_down(self):
        self.pedal.fail_all_live_writes_from = 1
        response = await self.edit({"op": "volume", "value": 1})
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())["error"]["applied"], 0)
        await wait_until(lambda: not self.server.connected)

    async def test_knob_turns_on_the_pedal_are_pushed_as_patches(self):
        ws = await self.connect_ws()
        self.pedal.turn_knob(0, 1, 0.9)
        self.pedal.push_pedal_event()
        message = await ws.receive_json(timeout=2)
        self.assertEqual(message["type"], "patch")
        self.assertEqual(message["ops"], [{"op": "param", "effect": 0, "index": 1, "value": 0.9}])
        await wait_until(lambda: self.server.autosaver.state == "dirty")
        self.assertEqual(len(self.server.baselines.list(8)), 1)

    async def test_pedal_changes_do_not_dirty_the_state_in_read_only_mode(self):
        self.server.stores = False  # what a read-only server does: nothing is tracked or stored
        self.pedal.turn_knob(0, 1, 0.9)
        self.pedal.push_pedal_event()
        await wait_until(lambda: self.server._live["effects"][0]["params"][1] == 0.9)
        self.assertEqual(self.server.autosaver.state, "saved")

    async def test_a_preset_switch_on_the_pedal_sends_the_new_state(self):
        ws = await self.connect_ws()
        self.pedal.switch_to_slot(3)
        self.pedal.push_pedal_event()
        message = await ws.receive_json(timeout=2)
        self.assertEqual(message["type"], "state")
        self.assertEqual(message["state"]["preset"], {"slot": 3, "display_number": 4, "name": "Preset4"})


class ManualSaveTest(ServerTestCase):
    """The default: edits are live (in the pedal's RAM) until the user asks to store them."""

    options_kwargs = {"autosave": False}

    async def volume(self) -> int:
        return (await (await self.get("/api/state")).json())["live"]["volume"]

    async def test_edits_are_live_and_never_stored_by_themselves(self):
        await self.edit({"op": "volume", "value": 7})
        await self.clock.advance(100)
        self.assertEqual(self.pedal.saves, 0)
        self.assertEqual(await self.volume(), 7)

    async def test_the_first_edit_keeps_a_baseline_and_marks_the_preset_unsaved(self):
        await self.edit({"op": "volume", "value": 7})
        self.assertEqual(len(self.server.baselines.list(8)), 1)
        state = await (await self.get("/api/state")).json()
        self.assertFalse(state["autosave"]["enabled"])
        self.assertEqual(state["autosave"]["state"], "dirty")

    async def test_save_now_stores_the_edits(self):
        await self.edit({"op": "volume", "value": 7})
        response = await self.post("/api/save-now")
        body = await response.json()
        self.assertEqual(response.status, 200)
        self.assertTrue(body["saved"])
        self.assertEqual(body["autosave"]["state"], "saved")
        self.assertEqual(self.pedal.saves, 1)

    async def test_save_now_with_nothing_to_store_does_nothing(self):
        body = await (await self.post("/api/save-now")).json()
        self.assertFalse(body["saved"])
        self.assertEqual(self.pedal.saves, 0)

    async def test_save_now_is_refused_in_read_only_mode(self):
        self.server.options = dataclasses.replace(self.server.options, read_only=True)
        response = await self.post("/api/save-now")
        self.assertEqual(response.status, 403)
        self.assertEqual(self.pedal.saves, 0)

    async def test_recalling_another_preset_with_unsaved_edits_needs_the_discard_flag(self):
        await self.edit({"op": "volume", "value": 7})
        refused = await self.post("/api/preset", {"display_number": 4})
        self.assertEqual(refused.status, 409)
        self.assertEqual((await refused.json())["error"]["code"], "unsaved_changes")
        self.assertEqual(self.pedal.runtime[1], 8)
        self.assertEqual(self.pedal.saves, 0)

        accepted = await self.post("/api/preset", {"display_number": 4, "discard": True})
        self.assertEqual(accepted.status, 200)
        self.assertEqual(self.pedal.runtime[1], 3)
        self.assertEqual(self.pedal.saves, 0)  # discarded, never stored
        state = await (await self.get("/api/state")).json()
        self.assertEqual(state["autosave"]["state"], "saved")

    async def test_recalling_with_nothing_unsaved_needs_no_flag(self):
        response = await self.post("/api/preset", {"display_number": 4})
        self.assertEqual(response.status, 200)

    async def test_revert_puts_back_what_is_stored(self):
        await self.edit({"op": "volume", "value": 7})
        self.assertEqual(await self.volume(), 7)
        response = await self.post("/api/revert")
        body = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(body["live"]["volume"], 84)  # the stored value
        self.assertEqual(await self.volume(), 84)
        self.assertEqual(self.pedal.saves, 0)
        self.assertEqual(body["autosave"]["state"], "saved")

    async def test_revert_tells_connected_pages_with_a_full_state(self):
        ws = await self.connect_ws()
        await self.edit({"op": "volume", "value": 7})
        await self.post("/api/revert")
        seen = []
        for _ in range(10):
            try:
                message = await ws.receive_json(timeout=0.5)
            except TimeoutError:
                break
            seen.append(message)
        states = [m for m in seen if m["type"] == "state"]
        self.assertTrue(states)
        self.assertEqual(states[-1]["state"]["live"]["volume"], 84)

    async def test_revert_is_refused_in_read_only_mode(self):
        self.server.options = dataclasses.replace(self.server.options, read_only=True)
        self.assertEqual((await self.post("/api/revert")).status, 403)

    async def test_revert_needs_a_link_to_the_pedal(self):
        self.pedal.drop()
        await wait_until(lambda: not self.server.connected)
        self.assertEqual((await self.post("/api/revert")).status, 503)

    async def test_a_change_made_on_the_pedal_marks_the_preset_unsaved(self):
        self.pedal.turn_knob(0, 0, 0.9)
        self.pedal.push_pedal_event()
        await wait_until(lambda: self.server.autosaver.unsaved_slot == 8)
        self.assertEqual(self.pedal.saves, 0)


class AutosaveModeSaveTest(ServerTestCase):
    options_kwargs = {"autosave": True}

    async def test_autosave_mode_still_stores_after_the_idle_delay(self):
        await self.edit({"op": "volume", "value": 7})
        await self.clock.advance(5)
        await wait_until(lambda: self.pedal.saves == 1)


class NoTokenServerTest(ServerTestCase):
    options_kwargs = {"require_token": False}

    async def test_api_routes_work_without_a_token(self):
        for headers in ({}, {"X-Nanocore-Token": "anything"}):
            response = await self.client.get("/api/state", headers=headers)
            self.assertEqual(response.status, 200, headers)
        response = await self.client.post(
            "/api/edit", json={"ops": [{"op": "volume", "value": 11}]}
        )
        self.assertEqual(response.status, 202)

    async def test_host_and_origin_are_still_checked(self):
        evil_origin = await self.client.get("/api/state", headers={"Origin": "http://evil.example"})
        self.assertEqual(evil_origin.status, 403)
        evil_host = await self.client.get("/api/state", headers={"Host": "evil.example"})
        self.assertEqual(evil_host.status, 403)
        self.assertEqual(self.pedal.live_writes, [])

    async def test_the_websocket_needs_no_token_but_still_the_auth_message(self):
        ws = await self.connect_ws(token="")
        await ws.close()
        raw = await self.client.ws_connect("/ws")
        await raw.send_json({"type": "hello"})
        message = await raw.receive(timeout=2)
        self.assertEqual(message.type, WSMsgType.CLOSE)

    async def test_health_tells_the_page_that_no_token_is_needed(self):
        body = await (await self.client.get("/api/health")).json()
        self.assertEqual(body, {"ok": True, "token_required": False})


class TokenRequiredByDefaultTest(ServerTestCase):
    async def test_health_says_a_token_is_required(self):
        body = await (await self.client.get("/api/health")).json()
        self.assertEqual(body, {"ok": True, "token_required": True})


class NoTokenOptionTest(unittest.TestCase):
    def test_the_flag_is_parsed(self):
        self.assertFalse(serve.build_parser().parse_args([]).no_token)
        self.assertTrue(serve.build_parser().parse_args(["--no-token"]).no_token)

    def test_it_is_refused_when_the_server_is_not_bound_to_the_loopback(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit) as raised:
            serve.main(["--no-token", "--host", "0.0.0.0"])
        self.assertEqual(raised.exception.code, 2)

    def test_the_address_carries_the_token_only_when_one_is_required(self):
        self.assertEqual(serve.page_url("127.0.0.1", 8765, "abc", True), "http://127.0.0.1:8765/#token=abc")
        self.assertEqual(serve.page_url("127.0.0.1", 8765, "abc", False), "http://127.0.0.1:8765/")


class MainTest(unittest.TestCase):
    def test_parser_defaults_and_flags(self):
        args = serve.build_parser().parse_args([])
        self.assertEqual((args.host, args.port, args.transport), ("127.0.0.1", 0, "bluetooth"))
        args = serve.build_parser().parse_args(
            ["--transport", "usb", "--alsa-port", "hw:1,0,0", "--no-autosave", "--read-only", "--autosave-delay", "5"]
        )
        self.assertTrue(args.no_autosave and args.read_only)
        self.assertEqual(args.autosave_delay, 5)

    def test_missing_aiohttp_is_a_clear_error(self):
        with mock.patch.object(serve, "web", None):
            with mock.patch("sys.stderr") as stderr:
                self.assertEqual(serve.main([]), 2)
            self.assertIn("aiohttp", "".join(c.args[0] for c in stderr.write.call_args_list))
            with self.assertRaises(RuntimeError):
                asyncio.run(serve.serve(serve.ServeOptions()))


class ServeLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def run_server(self, tmp, **kwargs):
        from nanocore_controller.device import NanocoreDevice
        from support_server import LivePedal

        pedal = LivePedal()
        urls = []
        stop = asyncio.Event()
        options = serve.ServeOptions(baseline_dir=tmp / "b", backup_dir=tmp / "k", **kwargs)
        task = asyncio.ensure_future(
            serve.serve(options, device_factory=lambda o: NanocoreDevice(pedal), ready=urls.append, stop=stop)
        )
        await wait_until(lambda: bool(urls))
        return pedal, urls[0], stop, task

    async def test_prints_the_token_url_and_shuts_down_cleanly(self):
        import io
        import tempfile
        from contextlib import redirect_stdout
        from pathlib import Path

        import aiohttp

        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()) as out:
            pedal, url, stop, task = await self.run_server(Path(tmp), autosave=True)
            self.assertRegex(url, r"^http://127\.0\.0\.1:\d+/#token=[\w-]{43}$")
            self.assertEqual(out.getvalue().strip(), url)
            base, token = url.split("/#token=")
            async with aiohttp.ClientSession() as session:
                response = await session.post(
                    base + "/api/edit",
                    json={"ops": [{"op": "volume", "value": 9}]},
                    headers={"X-Nanocore-Token": token},
                )
                for _ in range(200):
                    if response.status == 202:
                        break
                    await asyncio.sleep(0.01)
                    response = await session.post(
                        base + "/api/edit",
                        json={"ops": [{"op": "volume", "value": 9}]},
                        headers={"X-Nanocore-Token": token},
                    )
                self.assertEqual(response.status, 202)
            stop.set()
            await asyncio.wait_for(task, 5)
            self.assertEqual(pedal.saves, 1)  # autosave mode: the pending edit was flushed
            self.assertEqual(pedal.close_count, 1)

    async def test_manual_mode_never_stores_on_shutdown(self):
        import io
        import tempfile
        from contextlib import redirect_stdout
        from pathlib import Path

        import aiohttp

        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            pedal, url, stop, task = await self.run_server(Path(tmp))
            base, token = url.split("/#token=")
            async with aiohttp.ClientSession() as session:
                for _ in range(200):
                    response = await session.post(
                        base + "/api/edit",
                        json={"ops": [{"op": "volume", "value": 9}]},
                        headers={"X-Nanocore-Token": token},
                    )
                    if response.status == 202:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(response.status, 202)
            stop.set()
            await asyncio.wait_for(task, 5)
            self.assertEqual(pedal.saves, 0)  # unsaved edits are not written behind the user's back

    async def test_shutdown_does_not_wait_for_an_open_websocket(self):
        import io
        import tempfile
        from contextlib import redirect_stdout
        from pathlib import Path

        import aiohttp

        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            pedal, url, stop, task = await self.run_server(Path(tmp))
            base, token = url.split("/#token=")
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(base.replace("http", "ws") + "/ws") as ws:
                    await ws.send_json({"type": "auth", "token": token})
                    first = await ws.receive_json(timeout=5)
                    self.assertEqual(first["type"], "state")
                    stop.set()
                    await asyncio.wait_for(task, 5)  # used to wait for the 60 s connection timeout
                    closing = await ws.receive(timeout=2)
                    self.assertIn(closing.type, (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED))

    async def test_sigterm_stops_the_server(self):
        import io
        import signal
        import tempfile
        from contextlib import redirect_stdout
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            pedal, _url, _stop, task = await self.run_server(Path(tmp))
            signal.raise_signal(signal.SIGTERM)
            await asyncio.wait_for(task, 5)
            self.assertEqual(pedal.close_count, 1)

    async def test_each_run_has_a_new_token(self):
        import io
        import tempfile
        from contextlib import redirect_stdout
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            tokens = []
            for _ in range(2):
                _p, url, stop, task = await self.run_server(Path(tmp))
                tokens.append(url.split("=")[1])
                stop.set()
                await task
            self.assertNotEqual(*tokens)

    async def test_binding_beyond_loopback_prints_a_warning(self):
        import io
        import tempfile
        from contextlib import redirect_stderr, redirect_stdout
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            _p, url, stop, task = await self.run_server(Path(tmp), host="127.0.0.2")
            stop.set()
            await task
        self.assertIn("warning", err.getvalue())
        self.assertIn("127.0.0.2", url)


if __name__ == "__main__":
    unittest.main()


class HugeNumberTest(unittest.TestCase):
    def test_integers_too_large_for_a_float_are_validation_errors_not_crashes(self):
        huge = 10**400
        for op in (
            {"op": "param", "effect": 0, "index": 0, "value": huge},
            {"op": "variant", "effect": 0, "variant": 0, "params": [huge]},
        ):
            with self.subTest(op=op["op"]), self.assertRaises(serve.ApiError) as caught:
                serve.parse_op(op)
            self.assertEqual((caught.exception.status, caught.exception.code), (400, "validation"))


class DefaultStaticDirTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        self.package = self.root / "src" / "nanocore_controller"
        self.package.mkdir(parents=True)

    def build(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "index.html").write_text("<!doctype html>")

    def make_checkout(self):
        (self.root / "pyproject.toml").write_text("")
        (self.root / "web").mkdir()
        (self.root / "web" / "package.json").write_text("{}")

    def test_the_directory_shipped_in_the_package_wins(self):
        self.make_checkout()
        self.build(self.package / "web_dist")
        self.build(self.root / "web" / "dist")

        self.assertEqual(serve.default_static_dir(self.package), self.package / "web_dist")

    def test_a_source_checkout_falls_back_to_web_dist(self):
        self.make_checkout()
        self.build(self.root / "web" / "dist")

        self.assertEqual(serve.default_static_dir(self.package), self.root / "web" / "dist")

    def test_an_installed_package_never_serves_a_neighbouring_directory(self):
        self.build(self.root / "web" / "dist")  # no pyproject.toml: not a source tree

        self.assertIsNone(serve.default_static_dir(self.package))

    def test_a_build_without_index_html_is_ignored(self):
        self.make_checkout()
        (self.package / "web_dist").mkdir()

        self.assertIsNone(serve.default_static_dir(self.package))


class SlowWriteTest(ServerTestCase):
    """The real pedal shows a write in its snapshot about 20 ms after acknowledging it."""

    options_kwargs = {"apply_delay": 0.03}

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.pedal.clock = self.clock
        self.pedal.lag = 0.02
        self.pedal.cc_params = {60: (2, 0)}

    async def patches(self, ws, count=1):
        messages = []
        while len(messages) < count:
            message = await ws.receive_json(timeout=2)
            if message["type"] == "patch":
                messages.append(message)
        return messages

    async def run_edit(self, *ops):
        """Run an edit, moving the manual clock until the server has waited for the pedal."""

        task = asyncio.create_task(self.edit(*ops))
        while not task.done():
            await asyncio.sleep(0.005)  # real time: the baseline is written on a thread
            await self.clock.advance(0.03)
        return task.result()

    async def test_a_raw_controller_shows_up_as_a_patch_right_after_its_own_edit(self):
        ws = await self.connect_ws()

        response = await self.run_edit({"op": "cc", "cc": 60, "value": 100})

        self.assertEqual(response.status, 202)
        (patch,) = await self.patches(ws)
        (op,) = patch["ops"]
        self.assertEqual((op["op"], op["effect"], op["index"]), ("param", 2, 0))
        self.assertAlmostEqual(op["value"], 100 / 127, places=3)

    async def test_the_next_edit_does_not_rediscover_the_previous_one_as_a_change_on_the_pedal(self):
        ws = await self.connect_ws()
        await self.run_edit({"op": "cc", "cc": 60, "value": 100})
        await self.patches(ws)
        revision = self.server.rev

        await self.run_edit({"op": "cc", "cc": 60, "value": 40})
        (patch,) = await self.patches(ws)

        self.assertAlmostEqual(patch["ops"][0]["value"], 40 / 127, places=3)
        self.assertEqual(self.server.rev, revision + 1)  # one patch per edit, none replayed

    async def test_without_waiting_the_stale_read_would_miss_the_change(self):
        self.server.options.apply_delay = 0.0  # the old behaviour
        ws = await self.connect_ws()

        await self.edit({"op": "cc", "cc": 60, "value": 100})
        await settle()

        seen = []
        with contextlib.suppress(TimeoutError):
            while True:
                seen.append((await ws.receive_json(timeout=0.2))["type"])
        self.assertNotIn("patch", seen)  # the read came too early to see the change


class ReadOnlyNeverStoresTest(ServerTestCase):
    """Changes made on the pedal must not reach its flash through a read-only server."""

    options_kwargs = {"read_only": True}

    async def test_a_knob_turn_on_the_pedal_is_never_stored(self):
        self.assertFalse(self.server.options.autosave)
        self.pedal.turn_knob(2, 0, 0.9)
        self.pedal.push_pedal_event()
        await settle()
        await self.clock.advance(30)
        await settle()

        self.assertEqual(self.pedal.saves, 0)
        self.assertEqual(self.server.autosaver.state, "saved")
        self.assertEqual(self.pedal.live_writes, [])


class UnsavedChangesTest(ServerTestCase):
    async def test_recalling_is_refused_when_storing_the_edits_fails(self):
        await self.edit({"op": "volume", "value": 40})
        self.pedal.save_error = DeviceStatusError(0x46, 0x03)

        response = await self.post("/api/preset", {"display_number": 4})

        self.assertEqual(response.status, 502)  # the failed store is reported, nothing is recalled
        self.assertEqual(self.pedal.runtime[1], 8)  # still on the edited preset

    async def test_recalling_is_refused_when_the_store_is_skipped_silently(self):
        self.pedal.switch_to_slot(3)  # a preset without a baseline: the store skips it
        self.pedal.push_pedal_event()
        await wait_until(lambda: self.server._slot == 3)
        self.server.autosaver._baselined.discard(3)
        self.server.autosaver.mark_dirty(3)

        response = await self.post("/api/preset", {"display_number": 6})

        self.assertEqual(response.status, 409)
        self.assertEqual((await response.json())["error"]["code"], "unsaved_changes")
        self.assertEqual(self.pedal.runtime[1], 3)

    async def test_edits_of_a_preset_that_is_no_longer_active_do_not_block_a_recall(self):
        await self.edit({"op": "volume", "value": 40})
        self.pedal.switch_to_slot(3)
        self.pedal.push_pedal_event()
        await wait_until(lambda: self.server._slot == 3)

        response = await self.post("/api/preset", {"display_number": 6})

        self.assertEqual(response.status, 200)


class ConnectionLimitsTest(ServerTestCase):
    async def test_unknown_api_routes_are_not_found_rather_than_the_web_page(self):
        response = await self.get("/api/does-not-exist")
        self.assertEqual(response.status, 404)
        self.assertEqual((await response.json())["error"]["code"], "not_found")

    async def test_a_token_with_a_lone_surrogate_is_simply_wrong(self):
        self.assertFalse(serve._token_matches(self.server, "\ud800abc"))
        self.assertTrue(serve._token_matches(self.server, TOKEN))

    async def test_the_number_of_sockets_is_bounded(self):
        with mock.patch.object(serve, "MAX_SOCKETS", 1):
            first = await self.connect_ws()
            with self.assertRaises(WSServerHandshakeError) as caught:
                await self.client.ws_connect("/ws")
            self.assertEqual(caught.exception.status, 503)
            await first.close()

    async def test_three_timeouts_in_a_row_count_as_a_dropped_link(self):
        for _ in range(2):
            self.server._on_writer_error(DeviceTimeout(0x63, maybe_applied=False))
        self.assertTrue(self.server.connected)
        self.server._on_writer_error(DeviceTimeout(0x63, maybe_applied=False))
        self.assertFalse(self.server.connected)

    async def test_a_successful_read_resets_the_timeout_count(self):
        for _ in range(2):
            self.server._on_writer_error(DeviceTimeout(0x63, maybe_applied=False))
        await self.server.writer.submit(self.server._read_and_track)
        self.server._on_writer_error(DeviceTimeout(0x63, maybe_applied=False))
        self.assertTrue(self.server.connected)


class BoundedWorkTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_writer_queue_is_bounded(self):
        writer = serve.Writer(lambda exc: None)
        gate = asyncio.Event()

        async def blocked():
            await gate.wait()

        with mock.patch.object(serve, "MAX_QUEUED_JOBS", 2):
            tasks = [asyncio.create_task(writer.submit(blocked)) for _ in range(2)]
            await asyncio.sleep(0)
            with self.assertRaises(serve.ApiError) as caught:
                await writer.submit(blocked)
        self.assertEqual((caught.exception.status, caught.exception.code), (503, "busy"))
        writer.start()
        gate.set()
        await asyncio.gather(*tasks)
        await writer.close()

    def test_rate_limit_buckets_of_idle_clients_are_evicted_but_busy_ones_keep_their_state(self):
        now = [0.0]
        limiter = serve.RateLimiter(30, lambda: now[0])
        for i in range(serve.MAX_BUCKETS):
            limiter.allow(f"idle-{i}", 1)
        now[0] = 0.001
        self.assertTrue(limiter.allow("busy", 30))  # the newest client empties its bucket
        now[0] = 0.05  # long enough for every idle client's bucket to be full again
        limiter.allow("one-more", 1)  # crosses the limit and triggers the eviction

        self.assertFalse(limiter.allow("busy", 30))  # its empty bucket survived the eviction
        self.assertLessEqual(len(limiter._buckets), serve.MAX_BUCKETS + 1)


class RestoreBaselineFailureTest(ServerTestCase):
    async def baseline_id(self):
        listing = await (await self.get("/api/baselines?slot=8")).json()
        return listing[0]["id"]

    async def test_a_restore_that_could_not_be_rolled_back_is_never_stored(self):
        await self.edit({"op": "volume", "value": 40})
        await self.clock.advance(0.1)
        entry = await self.baseline_id()
        self.pedal.fail_all_live_writes_from = len(self.pedal.live_writes) + 7

        response = await self.post("/api/baselines/restore", {"slot": 8, "id": entry})

        self.assertGreaterEqual(response.status, 500)
        self.assertEqual(self.server.autosaver.state, "error")
        self.assertIsNone(self.server.autosaver.unsaved_slot)
        await self.clock.advance(60)
        self.assertEqual(self.pedal.saves, 0)

    async def test_a_restore_that_was_rolled_back_changes_nothing_and_stores_nothing(self):
        await self.edit({"op": "volume", "value": 40})
        await self.clock.advance(60)
        await self.wait_for_state("saved")
        saves = self.pedal.saves
        entry = await self.baseline_id()
        self.pedal.fail_live_write = {len(self.pedal.live_writes) + 7: DeviceStatusError(0x6D, 0x03)}

        response = await self.post("/api/baselines/restore", {"slot": 8, "id": entry})

        self.assertGreaterEqual(response.status, 500)
        self.assertEqual(self.server.autosaver.state, "saved")  # the pedal is as it was: nothing to store
        await self.clock.advance(60)
        self.assertEqual(self.pedal.saves, saves)
        self.assertEqual((await (await self.get("/api/state")).json())["live"]["volume"], 40)


class FirstSightBaselineTest(ServerTestCase):
    async def test_the_preset_active_at_connection_has_a_baseline_before_any_edit(self):
        self.assertEqual(len(self.server.baselines.list(8)), 1)

    async def test_a_preset_selected_on_the_pedal_gets_its_baseline_when_it_is_first_seen(self):
        self.assertEqual(self.server.baselines.list(3), [])

        self.pedal.switch_to_slot(3)
        self.pedal.push_pedal_event()
        await wait_until(lambda: len(self.server.baselines.list(3)) == 1)

        self.assertEqual(self.server.baselines.list(3)[0].slot, 3)
