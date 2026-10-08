"""Turn a WAV file into the impulse response the pedal stores.

The pedal's IR slots hold raw little-endian float32 samples, up to 1024 of them (the factory files are
that and nothing else, with peaks between 0.4 and 1.9). The conversion the official app applies to a
WAV is not known, so this one is a plain, documented choice: mix the channels to mono, resample to
48 kHz with a windowed sinc, keep the first 1024 samples with a short fade-out when the file is cut,
and scale the peak to 1.0. The sample rate the pedal expects is also unverified; a slot written from
this can always be put back from a backup.
"""

import math
import struct

from . import assets_protocol as assets
from .errors import ValidationError

TARGET_RATE = 48000
IR_LENGTH = assets.IR_MAX_BYTES // assets.IR_SAMPLE_SIZE  # 1024
FADE_SAMPLES = 64
SINC_HALF_WIDTH = 16
MIN_RATE, MAX_RATE = 8000, 192000
MAX_WAV_BYTES = 64 * 1024 * 1024

_PCM, _FLOAT, _EXTENSIBLE = 1, 3, 0xFFFE


def load_wav(data: bytes) -> tuple[list[float], int, int]:
    """Decode a WAV file to ``(mono samples in -1..1, sample rate, channel count)``."""

    if not isinstance(data, (bytes, bytearray)) or len(data) < 44 or len(data) > MAX_WAV_BYTES:
        raise ValidationError("not a usable WAV file")
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValidationError("not a WAV file (no RIFF/WAVE header)")
    fmt: tuple[int, int, int, int] | None = None
    payload: bytes | None = None
    offset = 12
    while offset + 8 <= len(data):
        tag, size = data[offset : offset + 4], struct.unpack_from("<I", data, offset + 4)[0]
        body = data[offset + 8 : offset + 8 + size]
        if tag == b"fmt " and size >= 16:
            code, channels, rate, _, _, bits = struct.unpack_from("<HHIIHH", body)
            if code == _EXTENSIBLE and size >= 40:
                code = struct.unpack_from("<H", body, 24)[0]
            fmt = (code, channels, rate, bits)
        elif tag == b"data":
            payload = body
        offset += 8 + size + (size & 1)
    if fmt is None or payload is None:
        raise ValidationError("the WAV file has no fmt or data chunk")
    code, channels, rate, bits = fmt
    if channels < 1 or channels > 8 or not MIN_RATE <= rate <= MAX_RATE:
        raise ValidationError("the WAV file has an unsupported channel count or sample rate")
    samples = _decode(payload, code, bits)
    frames = len(samples) // channels
    if frames == 0:
        raise ValidationError("the WAV file holds no audio")
    mono = [sum(samples[i * channels : (i + 1) * channels]) / channels for i in range(frames)]
    return mono, rate, channels


def _decode(payload: bytes, code: int, bits: int) -> list[float]:
    if code == _PCM and bits == 8:
        return [(b - 128) / 128 for b in payload]
    if code == _PCM and bits == 16:
        count = len(payload) // 2
        return [v / 32768 for v in struct.unpack(f"<{count}h", payload[: count * 2])]
    if code == _PCM and bits == 24:
        return [int.from_bytes(payload[i : i + 3], "little", signed=True) / 8388608 for i in range(0, len(payload) - 2, 3)]
    if code == _PCM and bits == 32:
        count = len(payload) // 4
        return [v / 2147483648 for v in struct.unpack(f"<{count}i", payload[: count * 4])]
    if code == _FLOAT and bits == 32:
        count = len(payload) // 4
        values = struct.unpack(f"<{count}f", payload[: count * 4])
        if not all(math.isfinite(v) for v in values):
            raise ValidationError("the WAV file contains values that are not numbers")
        return list(values)
    raise ValidationError(f"unsupported WAV encoding (format {code}, {bits} bits)")


def _resample(samples: list[float], rate: int, target: int, count: int) -> list[float]:
    if rate == target:
        return samples[:count]
    ratio = rate / target
    cutoff = min(1.0, target / rate)
    out = []
    for n in range(count):
        centre = n * ratio
        first = max(0, math.ceil(centre - SINC_HALF_WIDTH / cutoff))
        last = min(len(samples) - 1, math.floor(centre + SINC_HALF_WIDTH / cutoff))
        total = 0.0
        for k in range(first, last + 1):
            x = (k - centre) * cutoff
            sinc = 1.0 if x == 0 else math.sin(math.pi * x) / (math.pi * x)
            window = 0.5 + 0.5 * math.cos(math.pi * (k - centre) / (SINC_HALF_WIDTH / cutoff))
            total += samples[k] * sinc * window * cutoff
        out.append(total)
    return out


def convert_ir(
    samples: list[float],
    rate: int,
    *,
    target_rate: int = TARGET_RATE,
    length: int = IR_LENGTH,
    normalize: bool = True,
) -> list[float]:
    """Mono samples at ``rate`` to the samples of an IR slot (at most ``length``)."""

    if type(rate) is not int or not MIN_RATE <= rate <= MAX_RATE:
        raise ValidationError(f"sample rate must be between {MIN_RATE} and {MAX_RATE} Hz")
    if not any(samples):
        raise ValidationError("the impulse response is silent")
    available = int(len(samples) * target_rate / rate)
    count = min(length, available)
    out = _resample(samples, rate, target_rate, count)
    if available > length:  # the file was cut: avoid a click at the end
        fade = min(FADE_SAMPLES, count)
        for i in range(fade):
            out[count - fade + i] *= 0.5 + 0.5 * math.cos(math.pi * (i + 1) / fade)
    peak = max(abs(x) for x in out)
    if peak == 0:
        raise ValidationError("the impulse response is silent after conversion")
    if normalize:
        out = [x / peak for x in out]
    return out


def wav_to_ir(data: bytes, *, normalize: bool = True) -> bytes:
    """The bytes of an IR slot made from a WAV file."""

    samples, rate, _ = load_wav(data)
    return assets.ir_from_samples(convert_ir(samples, rate, normalize=normalize))
