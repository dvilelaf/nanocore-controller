"""The amplifier and IR model routes: list, download, upload (docs/api.md)."""

import dataclasses
import io
import stat
import struct
import wave
import zlib
from pathlib import Path

from nanocore_controller.ir_import import wav_to_ir
from nanocore_controller.serve import MAX_MODEL_BODY
from support_server import ServerTestCase
from test_assets_io import AMP_SIZE
from test_assets_write import WritablePedal, ir_blob


def wav_bytes(samples: int = 300) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes(struct.pack(f"<{samples}h", *[(i * 37) % 2000 - 1000 for i in range(samples)]))
    return out.getvalue()


def amp_blob(seed: int) -> bytes:
    return b"DDPB" + bytes((seed + i) % 256 for i in range(AMP_SIZE - 4))


class ModelTestCase(ServerTestCase):
    pedal_factory = WritablePedal

    @property
    def model_dir(self) -> Path:
        return self.tmp / "model-backups"

    def backups(self) -> list[Path]:
        return sorted(self.model_dir.glob("*.bin")) if self.model_dir.exists() else []

    async def put(self, path: str, body: bytes, **kwargs):
        return await self.post(path, data=body, **kwargs)


class ListTest(ModelTestCase):
    async def test_lists_both_storages(self):
        response = await self.get("/api/models")
        self.assertEqual(response.status, 200)
        document = await response.json()
        self.assertEqual(set(document), {"amp", "ir", "ead"})
        self.assertEqual((len(document["amp"]), len(document["ir"])), (38, 30))
        entry = document["amp"][12]
        data = self.pedal.data[("amp", 12)]
        self.assertEqual(
            entry, {"slot": 12, "name": "AMP12", "size": len(data), "crc32": zlib.crc32(data), "active": True}
        )
        self.assertEqual([e["slot"] for e in document["ir"] if e["active"]], [2])
        self.assertEqual(document["ir"][11]["size"], 2780)

    async def test_it_is_not_cached_across_a_write(self):
        before = await (await self.get("/api/models")).json()
        new = ir_blob(1)
        self.assertEqual((await self.put("/api/models/ir/29", new)).status, 200)
        after = await (await self.get("/api/models")).json()
        self.assertNotEqual(before["ir"][29]["crc32"], after["ir"][29]["crc32"])
        self.assertEqual(after["ir"][29]["crc32"], zlib.crc32(new))
        asked = []
        original = self.pedal.query

        async def counting(command, payload=b"", **kwargs):
            asked.append(command)
            return await original(command, payload, **kwargs)

        self.pedal.query = counting
        await self.get("/api/models")
        await self.get("/api/models")
        self.assertEqual(asked.count(0x30), 2)  # every call asks the pedal again

    async def test_device_text_is_cleaned(self):
        self.pedal.names[("amp", 3)] = "A\x07B"
        document = await (await self.get("/api/models")).json()
        self.assertNotIn("\x07", document["amp"][3]["name"])

    async def test_disconnected_is_503(self):
        self.server.connected = False
        response = await self.get("/api/models")
        self.assertEqual((response.status, (await response.json())["error"]["code"]), (503, "disconnected"))


class DownloadTest(ModelTestCase):
    async def test_returns_the_verified_data_as_a_download(self):
        response = await self.get("/api/models/amp/12")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"], "application/octet-stream")
        self.assertEqual(response.headers["Content-Disposition"], 'attachment; filename="amp-12-AMP12.bin"')
        self.assertEqual(response.headers["X-Nanocore-Api"], "1")
        self.assertEqual(await response.read(), self.pedal.data[("amp", 12)])

    async def test_ir_download(self):
        response = await self.get("/api/models/ir/11")
        self.assertEqual(await response.read(), self.pedal.data[("ir", 11)])
        self.assertIn('filename="ir-11-IR11.bin"', response.headers["Content-Disposition"])

    async def test_the_file_name_is_reduced_to_safe_characters(self):
        self.pedal.names[("ir", 4)] = 'a b/../"c;d'
        response = await self.get("/api/models/ir/4")
        disposition = response.headers["Content-Disposition"]
        self.assertRegex(disposition, r'^attachment; filename="ir-4-[A-Za-z0-9_.-]+\.bin"$')
        self.assertNotIn("/", disposition.split("filename=")[1])
        self.pedal.names[("ir", 5)] = '/;"'
        response = await self.get("/api/models/ir/5")
        self.assertEqual(response.headers["Content-Disposition"], 'attachment; filename="ir-5.bin"')

    async def test_unknown_routes_are_404(self):
        for path in ("/api/models/cab/1", "/api/models/amp/x", "/api/models/amp/-1", "/api/models/amp/1.5",
                     "/api/models/amp/٣", "/api/models/AMP/1", "/api/models/amp/"):
            response = await self.get(path)
            self.assertEqual(response.status, 404, path)
            self.assertEqual((await response.json())["error"]["code"], "not_found")

    async def test_a_slot_past_the_storage_is_a_validation_error(self):
        for path in ("/api/models/amp/38", "/api/models/ir/30", "/api/models/ir/999999999999"):
            response = await self.get(path)
            self.assertEqual(response.status, 400, path)
            self.assertEqual((await response.json())["error"]["code"], "validation")

    async def test_corrupt_data_is_a_502_not_a_download(self):
        self.pedal.corrupt.add(("amp", 5))
        response = await self.get("/api/models/amp/5")
        self.assertEqual((response.status, (await response.json())["error"]["code"]), (502, "protocol"))

    async def test_disconnected_is_503(self):
        self.server.connected = False
        self.assertEqual((await self.get("/api/models/amp/1")).status, 503)

    async def test_allowed_in_read_only_mode(self):
        self.server.options.read_only = True
        self.assertEqual((await self.get("/api/models")).status, 200)
        self.assertEqual((await self.get("/api/models/ir/3")).status, 200)


class UploadTest(ModelTestCase):
    async def test_a_raw_ir_is_written_verified_and_the_old_content_kept(self):
        old = self.pedal.data[("ir", 29)]
        new = ir_blob(2)
        response = await self.put("/api/models/ir/29?name=MyIR", new)
        self.assertEqual(response.status, 200)
        self.assertEqual(
            await response.json(),
            {"slot": 29, "name": "MyIR", "size": len(new), "crc32": zlib.crc32(new), "active": False},
        )
        self.assertEqual(self.pedal.data[("ir", 29)], new)
        files = self.backups()
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r"^ir-29-\d{8}T\d{12}Z\.bin$")
        self.assertEqual(files[0].read_bytes(), old)

    async def test_the_backup_directory_and_files_are_private(self):
        await self.put("/api/models/ir/29", ir_blob(3))
        self.assertEqual(stat.S_IMODE(self.model_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.backups()[0].stat().st_mode), 0o600)

    async def test_every_write_gets_its_own_backup(self):
        for seed in (4, 5, 6):
            self.assertEqual((await self.put("/api/models/ir/29", ir_blob(seed))).status, 200)
        names = [f.name for f in self.backups()]
        self.assertEqual(len(set(names)), 3)

    async def test_a_wav_is_converted(self):
        wav = wav_bytes()
        response = await self.put("/api/models/ir/7", wav)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.pedal.data[("ir", 7)], wav_to_ir(wav))

    async def test_an_amplifier_blob(self):
        blob = amp_blob(1)
        response = await self.put("/api/models/amp/36", blob)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.pedal.data[("amp", 36)], blob)
        self.assertEqual((await response.json())["size"], AMP_SIZE)

    async def test_the_selection_of_the_preset_is_kept(self):
        await self.put("/api/models/ir/29", ir_blob(7))
        self.assertEqual(self.pedal.active, {"amp": 12, "ir": 2})
        response = await self.put("/api/models/ir/2", ir_blob(8))
        self.assertTrue((await response.json())["active"])

    async def test_invalid_data_is_400_and_nothing_is_written(self):
        cases = [
            ("ir", 1, b"", None),
            ("ir", 1, ir_blob(1, 1025), None),
            ("ir", 1, b"\x00\x00\x00", None),
            ("ir", 1, struct.pack("<2f", 1.0, float("nan")), None),
            ("ir", 1, b"RIFF" + b"\x00" * 60, None),  # a broken WAV
            ("amp", 1, b"XXXX" + bytes(AMP_SIZE - 4), None),
            ("amp", 1, ir_blob(1), None),
            ("ir", 1, ir_blob(1), "x" * 40),
            ("ir", 30, ir_blob(1), None),
            ("amp", 38, amp_blob(1), None),
        ]
        for kind, slot, body, name in cases:
            query = f"?name={name}" if name else ""
            response = await self.put(f"/api/models/{kind}/{slot}{query}", body)
            self.assertEqual(response.status, 400, (kind, slot, len(body)))
            self.assertEqual((await response.json())["error"]["code"], "validation")
        self.assertEqual(self.pedal.writes, [])
        self.assertEqual(self.backups(), [])

    async def test_an_encrypted_ead_file_is_not_accepted(self):
        response = await self.put("/api/models/amp/1", bytes(range(256)) * 60)
        self.assertEqual(response.status, 400)
        self.assertEqual(self.pedal.writes, [])

    async def test_unknown_kind_or_slot_is_404(self):
        for path in ("/api/models/cab/1", "/api/models/ir/x"):
            self.assertEqual((await self.put(path, ir_blob(1))).status, 404, path)
        self.assertEqual(self.pedal.writes, [])

    async def test_a_device_failure_is_mapped_and_the_old_content_is_in_the_backup(self):
        old = self.pedal.data[("ir", 29)]
        self.pedal.fail_chunk = 2
        response = await self.put("/api/models/ir/29", ir_blob(9))
        self.assertEqual((response.status, (await response.json())["error"]["code"]), (502, "device_status"))
        self.assertEqual(self.pedal.data[("ir", 29)], old)
        self.assertEqual(self.backups()[0].read_bytes(), old)

    async def test_disconnected_is_503_and_nothing_is_sent(self):
        self.server.connected = False
        response = await self.put("/api/models/ir/29", ir_blob(1))
        self.assertEqual((response.status, (await response.json())["error"]["code"]), (503, "disconnected"))
        self.assertEqual(self.pedal.writes, [])

    async def test_read_only_is_403_even_for_junk(self):
        self.server.options.read_only = True
        for body in (ir_blob(1), b"", b"junk"):
            response = await self.put("/api/models/ir/29", body)
            self.assertEqual((response.status, (await response.json())["error"]["code"]), (403, "read_only"))
        self.assertEqual(self.pedal.writes, [])
        self.assertEqual(self.backups(), [])

    async def test_it_costs_one_rate_limit_token(self):
        bucket = self.server.limiter
        for _ in range(30):
            self.assertTrue(bucket.allow("127.0.0.1", 1))
        response = await self.put("/api/models/ir/29", ir_blob(1))
        self.assertEqual((response.status, (await response.json())["error"]["code"]), (429, "rate_limited"))
        self.assertEqual(self.pedal.writes, [])
        self.clock.now += 1.0
        self.assertEqual((await self.put("/api/models/ir/29", ir_blob(1))).status, 200)
        self.assertFalse(bucket.allow("127.0.0.1", 30))  # the upload took a token, not the whole bucket...

    async def test_the_preset_is_not_marked_unsaved(self):
        await self.put("/api/models/ir/29", ir_blob(1))
        await self.put("/api/models/amp/36", amp_blob(1))
        self.assertIsNone(self.server.autosaver.unsaved_slot)
        state = await (await self.get("/api/state")).json()
        self.assertEqual(state["autosave"]["state"], "saved")
        self.assertEqual(self.pedal.saved_slots, [])

    async def test_the_assets_cache_is_refreshed(self):
        before = await (await self.get("/api/assets")).json()
        slot = before["ir"]["slot"]
        self.pedal.names[("ir", slot)] = "Old"
        await self.server.assets()  # cached under the old name
        response = await self.put(f"/api/models/ir/{slot}?name=NewName", ir_blob(1))
        self.assertEqual(response.status, 200)
        after = await (await self.get("/api/assets")).json()
        self.assertEqual(after["ir"]["name"], "NewName")

    async def test_a_failed_write_also_drops_the_assets_cache(self):
        await self.get("/api/assets")
        self.assertIsNotNone(self.server._assets)
        self.pedal.fail_chunk = 1
        await self.put("/api/models/ir/29", ir_blob(1))
        self.assertIsNone(self.server._assets)

    async def test_the_backup_directory_follows_the_option(self):
        self.server.options.model_backup_dir = self.tmp / "elsewhere"
        await self.put("/api/models/ir/29", ir_blob(1))
        self.assertEqual(len(list((self.tmp / "elsewhere").glob("ir-29-*.bin"))), 1)
        self.assertEqual(self.backups(), [])


class BodyLimitTest(ModelTestCase):
    async def test_the_model_limit_is_4_mib(self):
        self.assertEqual(MAX_MODEL_BODY, 4 * 1024 * 1024)

    async def test_a_body_over_the_limit_is_413_by_content_length(self):
        response = await self.put("/api/models/ir/29", bytes(MAX_MODEL_BODY + 1))
        self.assertEqual((response.status, (await response.json())["error"]["code"]), (413, "too_large"))
        self.assertEqual(self.pedal.writes, [])

    async def test_a_chunked_body_over_the_limit_is_413(self):
        async def body():
            for _ in range(5):
                yield bytes(1024 * 1024)

        response = await self.post("/api/models/ir/29", data=body())
        self.assertEqual(response.status, 413)
        self.assertEqual(self.pedal.writes, [])

    async def test_a_body_beyond_the_old_global_limit_reaches_validation(self):
        response = await self.put("/api/models/ir/29", bytes(200 * 1024))
        self.assertEqual((response.status, (await response.json())["error"]["code"]), (400, "validation"))

    async def test_other_routes_keep_the_64_kib_limit(self):
        for path in ("/api/edit", "/api/preset", "/api/settings", "/api/preset-file"):
            response = await self.post(path, data=b" " * (64 * 1024 + 1), headers={**self.auth, "Content-Type": "application/json"})
            self.assertEqual(response.status, 413, path)


class RoutesTest(ModelTestCase):
    async def test_the_token_is_required(self):
        for method, path in (("GET", "/api/models"), ("GET", "/api/models/ir/0"), ("POST", "/api/models/ir/0")):
            for headers in ({}, {"X-Nanocore-Token": "wrong"}):
                response = await self.client.request(method, path, headers=headers, data=ir_blob(1))
                self.assertEqual(response.status, 401, (method, path))
        self.assertEqual(self.pedal.writes, [])

    async def test_a_foreign_origin_cannot_upload(self):
        response = await self.put("/api/models/ir/29", ir_blob(1), headers={**self.auth, "Origin": "http://evil.example"})
        self.assertEqual(response.status, 403)
        response = await self.put("/api/models/ir/29", ir_blob(1), headers={**self.auth, "Host": "evil.example"})
        self.assertEqual(response.status, 403)
        self.assertEqual(self.pedal.writes, [])

    async def test_other_methods_are_405(self):
        for method in ("PUT", "DELETE"):
            response = await self.client.request(method, "/api/models/ir/0", headers=self.auth)
            self.assertEqual(response.status, 405, method)

    async def test_errors_never_contain_paths(self):
        self.pedal.fail_chunk = 1
        response = await self.put("/api/models/ir/29", ir_blob(1))
        self.assertNotIn(str(self.tmp), await response.text())



class EadUploadTest(ModelTestCase):
    """An .ead file is installed only through the decryptor the user configures (docs/ead-decryptor.md)."""

    @staticmethod
    def container(blob: bytes) -> bytes:
        payload = bytes(b ^ 0x5A for b in blob)
        return b"SAPF" + struct.pack("<I", 1) + bytes(16) + struct.pack("<I", len(payload)) + payload + bytes(32)

    def decryptor(self) -> str:
        import sys

        script = self.tmp / "decrypt.py"
        script.write_text(
            "import sys\ndata = sys.stdin.buffer.read()\n"
            "sys.stdout.buffer.write(bytes(b ^ 0x5A for b in data[28:-32]))\n"
        )
        return f"{sys.executable} {script}"

    async def test_without_a_decryptor_the_answer_says_how_to_configure_one(self):
        response = await self.put("/api/models/amp/1", self.container(amp_blob(2)))
        self.assertEqual(response.status, 400)
        error = (await response.json())["error"]
        self.assertEqual(error["code"], "validation")
        self.assertIn("ead-decryptor", error["message"])
        self.assertEqual(self.pedal.writes, [])
        self.assertEqual(self.backups(), [])

    async def test_the_list_says_whether_a_decryptor_is_configured(self):
        self.assertFalse((await (await self.get("/api/models")).json())["ead"])
        self.server.options = dataclasses.replace(self.server.options, ead_decryptor=self.decryptor())
        self.assertTrue((await (await self.get("/api/models")).json())["ead"])

    async def test_with_a_decryptor_the_model_is_installed_like_any_other(self):
        self.server.options = dataclasses.replace(self.server.options, ead_decryptor=self.decryptor())
        blob = amp_blob(3)
        response = await self.put("/api/models/amp/1", self.container(blob))
        self.assertEqual(response.status, 200)
        self.assertEqual(self.pedal.data[("amp", 1)], blob)
        self.assertEqual(len(self.backups()), 1)

    async def test_a_decryptor_that_fails_writes_nothing(self):
        import sys

        self.server.options = dataclasses.replace(self.server.options, ead_decryptor=f"{sys.executable} -c 'import sys; sys.exit(4)'")
        response = await self.put("/api/models/amp/1", self.container(amp_blob(4)))
        self.assertEqual(response.status, 400)
        self.assertEqual(self.pedal.writes, [])
        self.assertEqual(self.backups(), [])

    async def test_an_ead_is_not_accepted_for_an_ir_slot(self):
        self.server.options = dataclasses.replace(self.server.options, ead_decryptor=self.decryptor())
        response = await self.put("/api/models/ir/1", self.container(amp_blob(5)))
        self.assertEqual(response.status, 400)
        self.assertEqual(self.pedal.writes, [])

    async def test_it_needs_the_token_like_every_write(self):
        response = await self.client.post("/api/models/amp/1", data=self.container(amp_blob(6)))
        self.assertEqual(response.status, 401)
