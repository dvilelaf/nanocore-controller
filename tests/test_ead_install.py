import os
import struct
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from nanocore_controller import assets_protocol as ap
from nanocore_controller.device import NanocoreDevice
from nanocore_controller.ead_install import DECRYPTOR_ENV, decrypt_ead, resolve_decryptor
from nanocore_controller.errors import ValidationError
from test_assets_io import AMP_SIZE
from test_assets_write import WritablePedal
from test_cli import CliTestCase

# A toy "protection" used only to test the plumbing: XOR with 0x5A. It is not the vendor's scheme.
TOY_DECRYPTOR = textwrap.dedent(
    """
    import sys
    data = sys.stdin.buffer.read()
    payload = data[28:-32]
    sys.stdout.buffer.write(bytes(b ^ 0x5A for b in payload))
    """
)


def model(seed: int = 1) -> bytes:
    return b"DDPB" + bytes((seed * 7 + i) % 251 for i in range(AMP_SIZE - 4))


def container(blob: bytes, *, version: int = 1, size: int | None = None, tail: int = 32) -> bytes:
    payload = bytes(b ^ 0x5A for b in blob)
    declared = len(payload) if size is None else size
    return b"SAPF" + struct.pack("<I", version) + bytes(range(16)) + struct.pack("<I", declared) + payload + bytes(tail)


def eadl(inner: bytes) -> bytes:
    header = b"meta"
    return b"EADL" + struct.pack("<HI", 1, len(header)) + header + inner


class Workdir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.script = self.dir / "decrypt.py"
        self.script.write_text(TOY_DECRYPTOR)
        self.command = [sys.executable, str(self.script)]

    def script_with(self, body: str) -> list[str]:
        path = self.dir / "other.py"
        path.write_text(textwrap.dedent(body))
        return [sys.executable, str(path)]


class DecryptTest(Workdir):
    def test_a_container_is_decrypted_by_the_external_program(self):
        self.assertEqual(decrypt_ead(container(model(3)), self.command), model(3))

    def test_an_eadl_wrapper_is_removed_before_the_program_sees_the_data(self):
        self.assertEqual(decrypt_ead(eadl(container(model(4))), self.command), model(4))

    def test_a_file_that_is_not_an_ead_is_refused_before_anything_runs(self):
        for bad in (b"", b"not an ead file", container(model(), version=2), container(model(), size=5), b"SAPF" + bytes(10)):
            with self.subTest(bad[:8]), self.assertRaises(ValidationError):
                decrypt_ead(bad, ["/nonexistent/never-run"])

    def test_a_program_that_fails_is_reported_with_its_message(self):
        command = self.script_with("import sys\nsys.stderr.write('bad key')\nsys.exit(3)\n")
        with self.assertRaises(ValidationError) as raised:
            decrypt_ead(container(model()), command)
        self.assertIn("bad key", str(raised.exception))

    def test_output_that_is_not_a_model_is_refused(self):
        wrong_magic = self.script_with("import sys\nsys.stdout.buffer.write(b'XXXX' + bytes(12238))\n")
        wrong_length = self.script_with("import sys\nsys.stdout.buffer.write(b'DDPB' + bytes(100))\n")
        for command in (wrong_magic, wrong_length):
            with self.subTest(command[-1][-8:]), self.assertRaises(ValidationError):
                decrypt_ead(container(model()), command)

    def test_a_program_that_never_answers_is_stopped(self):
        command = self.script_with("import time\ntime.sleep(30)\n")
        with self.assertRaises(ValidationError) as raised:
            decrypt_ead(container(model()), command, timeout=0.5)
        self.assertIn("did not answer", str(raised.exception))

    def test_a_program_that_cannot_be_started_is_reported(self):
        with self.assertRaises(ValidationError):
            decrypt_ead(container(model()), ["/nonexistent/decryptor"])


class ResolveTest(unittest.TestCase):
    def test_the_argument_wins_over_the_environment(self):
        with mock.patch.dict(os.environ, {DECRYPTOR_ENV: "from-env --x"}):
            self.assertEqual(resolve_decryptor("from-arg --y"), ["from-arg", "--y"])
            self.assertEqual(resolve_decryptor(None), ["from-env", "--x"])

    def test_without_one_the_message_says_how_to_provide_it(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(DECRYPTOR_ENV, None)
            with self.assertRaises(ValidationError) as raised:
                resolve_decryptor(None)
        self.assertIn(DECRYPTOR_ENV, str(raised.exception))
        self.assertIn("docs/ead-decryptor.md", str(raised.exception))


class InstallTest(Workdir, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pedal = WritablePedal()
        self.device = NanocoreDevice(self.pedal)
        await self.device.connect()
        self.addAsyncCleanup(self.device.close)
        self.safety = self.dir / "old-amp.bin"

    async def test_an_ead_is_decrypted_and_written_with_a_backup(self):
        blob = model(5)
        info = await self.device.install_ead(
            1, container(blob), decryptor=self.command, safety_backup=self.safety, name="Fresh"
        )
        self.assertEqual(self.pedal.data[("amp", 1)], blob)
        self.assertEqual(info.name, "Fresh")
        self.assertTrue(self.safety.exists())

    async def test_nothing_is_written_when_the_decryption_fails(self):
        before = dict(self.pedal.data)
        command = self.script_with("import sys\nsys.exit(1)\n")
        with self.assertRaises(ValidationError):
            await self.device.install_ead(1, container(model()), decryptor=command, safety_backup=self.safety)
        self.assertEqual(self.pedal.writes, [])
        self.assertEqual(self.pedal.data, before)
        self.assertFalse(self.safety.exists())


class CliTest(CliTestCase):
    def setUp(self):
        super().setUp()
        self.session = WritablePedal()
        self.script = self.directory / "decrypt.py"
        self.script.write_text(TOY_DECRYPTOR)
        self.ead = self.directory / "tone.ead"
        self.ead.write_bytes(container(model(6)))
        self.decryptor = f"{sys.executable} {self.script}"

    def tearDown(self):
        self.script.unlink(missing_ok=True)
        self.ead.unlink(missing_ok=True)

    def test_the_plan_is_printed_without_touching_the_pedal(self):
        code, output = self.run_usb("assets", "write", "amp", "1", str(self.ead), "--decryptor", self.decryptor)
        self.assertEqual(code, 0)
        self.assertIn("decrypted", output)
        self.assertEqual(self.session.writes, [])
        self.assertNothingOpened()

    def test_apply_installs_the_model(self):
        safety = self.directory / "old.bin"
        code, output = self.run_usb(
            "assets", "write", "amp", "1", str(self.ead), "--decryptor", self.decryptor, "--apply", "--safety-backup", str(safety)
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.session.data[("amp", 1)], model(6))
        self.assertIn("verified", output)
        safety.unlink()

    def test_the_decryptor_can_come_from_the_environment(self):
        with mock.patch.dict(os.environ, {DECRYPTOR_ENV: self.decryptor}):
            code, output = self.run_usb("assets", "write", "amp", "1", str(self.ead))
        self.assertEqual(code, 0)
        self.assertIn("decrypted", output)

    def test_without_a_decryptor_an_ead_is_refused_with_guidance(self):
        os.environ.pop(DECRYPTOR_ENV, None)
        code, _ = self.run_usb("assets", "write", "amp", "1", str(self.ead))
        self.assertNotEqual(code, 0)
        self.assertNothingOpened()
        self.assertIsNotNone(ap)


if __name__ == "__main__":
    unittest.main()
