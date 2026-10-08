import math
import struct
import unittest

from nanocore_controller import assets_protocol as ap
from nanocore_controller.errors import ValidationError
from nanocore_controller.ir_import import convert_ir, load_wav, wav_to_ir
from test_assets_write import WritablePedal
from test_cli import CliTestCase


def wav_bytes(samples, rate=48000, channels=1, bits=16, fmt=1, extensible=False):
    """A RIFF/WAVE file with the given interleaved float samples in -1..1."""

    if fmt == 3:
        frames = b"".join(struct.pack("<f", x) for x in samples)
    elif bits == 16:
        frames = b"".join(struct.pack("<h", round(max(-1, min(1, x)) * 32767)) for x in samples)
    elif bits == 24:
        frames = b"".join(round(max(-1, min(1, x)) * 8388607).to_bytes(3, "little", signed=True) for x in samples)
    elif bits == 32:
        frames = b"".join(struct.pack("<i", round(max(-1, min(1, x)) * 2147483647)) for x in samples)
    else:
        frames = bytes(round((x * 127) + 128) for x in samples)
    block = channels * bits // 8
    if extensible:
        tag = 0xFFFE
        extra = struct.pack("<HHI16s", 22, bits, 0, struct.pack("<HH", fmt, 0) + bytes.fromhex("00001000800000aa00389b71"))
    else:
        tag, extra = fmt, b""
    body = struct.pack("<HHIIHH", tag, channels, rate, rate * block, block, bits) + extra
    chunks = b"fmt " + struct.pack("<I", len(body)) + body + b"data" + struct.pack("<I", len(frames)) + frames
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


def impulse(length, at=0, value=0.5):
    out = [0.0] * length
    out[at] = value
    return out


class LoadTest(unittest.TestCase):
    def test_the_common_sample_formats_are_read(self):
        original = [0.0, 0.5, -0.25, 0.125]
        for kwargs in ({"bits": 16}, {"bits": 24}, {"bits": 32}, {"bits": 32, "fmt": 3}, {"bits": 16, "extensible": True}):
            with self.subTest(kwargs):
                samples, rate, channels = load_wav(wav_bytes(original, **kwargs))
                self.assertEqual((rate, channels), (48000, 1))
                for got, want in zip(samples, original, strict=True):
                    self.assertAlmostEqual(got, want, places=3)

    def test_channels_are_mixed_down_to_mono(self):
        data = wav_bytes([0.5, -0.5, 0.25, 0.75], channels=2, bits=32, fmt=3)
        samples, _, channels = load_wav(data)
        self.assertEqual(channels, 2)
        self.assertEqual(samples, [0.0, 0.5])

    def test_files_that_are_not_wav_or_are_broken_are_refused(self):
        good = wav_bytes([0.1, 0.2])
        for bad in (b"", b"RIFF", b"not a wav file at all", good[:30], b"RIFX" + good[4:], good.replace(b"data", b"junk")):
            with self.subTest(bad[:12]), self.assertRaises(ValidationError):
                load_wav(bad)

    def test_unsupported_encodings_are_refused(self):
        with self.assertRaises(ValidationError):
            load_wav(wav_bytes([0.1, 0.2], fmt=2))  # ADPCM


class ConvertTest(unittest.TestCase):
    def test_the_result_is_1024_finite_samples_normalised_to_a_peak_of_one(self):
        out = convert_ir(impulse(2000, 3, 0.25), 48000)
        self.assertEqual(len(out), 1024)
        self.assertTrue(all(math.isfinite(x) for x in out))
        self.assertAlmostEqual(max(abs(x) for x in out), 1.0, places=6)

    def test_a_short_ir_keeps_its_length(self):
        out = convert_ir(impulse(300, 2), 48000)
        self.assertEqual(len(out), 300)

    def test_the_rate_is_converted_so_that_time_is_kept(self):
        # An impulse 441 samples in at 44.1 kHz is 10 ms in: 480 samples in at 48 kHz.
        out = convert_ir(impulse(1500, 441), 44100)
        peak = max(range(len(out)), key=lambda i: abs(out[i]))
        self.assertAlmostEqual(peak, 480, delta=1)

    def test_the_tail_is_faded_out_when_the_file_is_cut(self):
        out = convert_ir([1.0] * 4000, 48000)
        self.assertLess(abs(out[-1]), 0.05)
        self.assertGreater(abs(out[-200]), 0.9)

    def test_normalising_can_be_turned_off(self):
        out = convert_ir(impulse(100, 0, 0.25), 48000, normalize=False)
        self.assertAlmostEqual(max(abs(x) for x in out), 0.25, places=6)

    def test_silence_is_refused(self):
        with self.assertRaises(ValidationError):
            convert_ir([0.0] * 100, 48000)

    def test_a_rate_that_makes_no_sense_is_refused(self):
        for rate in (0, -1, 1000, 1_000_000):
            with self.subTest(rate), self.assertRaises(ValidationError):
                convert_ir(impulse(100), rate)

    def test_the_bytes_are_a_valid_pedal_ir(self):
        data = wav_to_ir(wav_bytes(impulse(2000, 0, 0.5), rate=44100))
        self.assertEqual(ap.validate_ir(data), 1024)


class CliTest(CliTestCase):
    def setUp(self):
        super().setUp()
        self.session = WritablePedal()
        self.wav = self.directory / "cab.wav"
        self.wav.write_bytes(wav_bytes(impulse(3000, 5, 0.4), rate=44100))

    def test_a_wav_is_converted_and_written_like_any_ir_file(self):
        safety = self.directory / "old.bin"
        code, output = self.run_usb("assets", "write", "ir", "29", str(self.wav), "--name", "MyCab", "--apply", "--safety-backup", str(safety))
        self.assertEqual(code, 0)
        written = self.session.data[("ir", 29)]
        self.assertEqual(len(written), 4096)
        self.assertEqual(ap.validate_ir(written), 1024)
        self.assertIn("verified", output)

    def test_the_plan_says_the_file_is_converted(self):
        code, output = self.run_usb("assets", "write", "ir", "29", str(self.wav))
        self.assertEqual(code, 0)
        self.assertIn("converted from WAV", output)
        self.assertEqual(self.session.writes, [])


if __name__ == "__main__":
    unittest.main()
