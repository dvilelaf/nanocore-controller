"""Install an ``.ead`` amplifier file through a decryptor that the user provides.

The ``.ead`` files of Livtra's app and tone catalog are encrypted. This project contains no key and no
decryption: it checks the framing of the file, hands the container to a program the user configures,
checks what comes back and writes it with the same safeguards as any other slot write. See
``docs/ead-decryptor.md`` for the contract of that program.
"""

import os
import shlex
import subprocess
from collections.abc import Sequence

from . import assets_protocol as assets
from .errors import ProtocolError, ValidationError

DECRYPTOR_ENV = "NANOCORE_EAD_DECRYPTOR"
DEFAULT_TIMEOUT = 20.0
MAX_OUTPUT_BYTES = 64 * 1024
MAX_MESSAGE = 300


def resolve_decryptor(command: str | None) -> list[str]:
    """The decryptor command from the argument or from ``NANOCORE_EAD_DECRYPTOR`` (no shell is used)."""

    text = command if command else os.environ.get(DECRYPTOR_ENV, "")
    parts = shlex.split(text) if text.strip() else []
    if not parts:
        raise ValidationError(
            "installing an .ead file needs a decryptor that you provide: pass --decryptor 'PROGRAM ARGS' or "
            f"set {DECRYPTOR_ENV}. This project ships no key and no decryption; see docs/ead-decryptor.md"
        )
    return parts


def unwrap_container(data: bytes) -> tuple[bytes, assets.SapfContainer]:
    """The ``SAPF`` container of an ``.ead`` file (an ``EADL`` wrapper is removed) and its parsed framing."""

    try:
        inner = assets.unwrap_eadl(data)
    except ProtocolError as exc:
        raise ValidationError(str(exc)) from exc
    return inner, assets.parse_sapf(inner)


def decrypt_ead(data: bytes, command: Sequence[str], *, timeout: float = DEFAULT_TIMEOUT) -> bytes:
    """Run the user's decryptor on the container of ``data`` and return the model it produces.

    The container goes to the program's standard input and the model comes from its standard output.
    The output must be a pedal model: it starts with ``DDPB`` and has exactly the payload size the
    container declares.
    """

    container_bytes, container = unwrap_container(data)  # the framing is checked before anything runs
    try:
        completed = subprocess.run(  # noqa: S603 - the command is the user's own, never run through a shell
            list(command),
            input=container_bytes,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValidationError(f"the decryptor did not answer within {timeout:g} seconds") from exc
    except OSError as exc:
        raise ValidationError(f"the decryptor could not be started: {exc}") from exc
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", "replace").strip()[-MAX_MESSAGE:] or "no message"
        raise ValidationError(f"the decryptor failed (exit status {completed.returncode}): {message}")
    output = completed.stdout
    if len(output) > MAX_OUTPUT_BYTES:
        raise ValidationError("the decryptor produced far more data than a model")
    try:
        assets.validate_stored_amp(output)
    except ValidationError as exc:
        raise ValidationError(f"the decryptor did not return a pedal model: {exc}") from exc
    if len(output) != container.payload_size:
        raise ValidationError(
            f"the decryptor returned {len(output)} bytes but the file declares {container.payload_size}"
        )
    return output
