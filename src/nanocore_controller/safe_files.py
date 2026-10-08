"""Files that are created once, privately, and never replaced."""

import os
from pathlib import Path

from .errors import ValidationError


def write_new(path: Path | str, data: bytes) -> None:
    """Create ``path`` with mode 0600 and write ``data``; an existing file is an error, never replaced."""

    target = Path(path)
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValidationError(f"{target} already exists; nothing was overwritten") from exc
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
