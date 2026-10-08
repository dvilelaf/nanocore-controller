"""Atomic, private backup files and a strict loader for them."""

import contextlib
import json
import os
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn

from .errors import BackupError, ValidationError


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_backup(path: Path, document: Mapping[str, Any]) -> None:
    """Write ``document`` to a new file at ``path`` or leave nothing behind.

    The file is complete and flushed before it becomes visible under its final
    name, is created with mode 0600 and never replaces an existing path.
    """

    path = Path(path)
    if os.path.lexists(path):
        raise BackupError(f"refusing to overwrite existing path {path}")
    try:
        text = json.dumps(document, allow_nan=False, indent=2, ensure_ascii=False) + "\n"
        data = text.encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BackupError(f"backup cannot be serialised: {exc}") from exc

    published = False
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            handle = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            raise
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        published = True
        _fsync_directory(path.parent)
    except OSError as exc:
        if published:
            with contextlib.suppress(OSError):
                path.unlink()
        raise BackupError(f"cannot write backup {path}: {exc}") from exc
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                os.unlink(temporary)


def _reject_constant(name: str) -> NoReturn:
    raise ValueError(f"{name} is not valid JSON")


def load_backup(path: Path, *, max_bytes: int = 1_000_000) -> dict[str, Any]:
    """Read and validate a backup file written by ``write_backup``."""

    from .ble_restore import validate_backup

    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise BackupError(f"{path} is not a regular file")
            data = handle.read(max_bytes + 1)
    except OSError as exc:
        raise BackupError(f"cannot read backup {path}: {exc}") from exc
    if len(data) > max_bytes:
        raise BackupError(f"backup {path} is too large (more than {max_bytes} bytes)")
    try:
        document = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        raise BackupError(f"backup {path} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ValidationError("backup root must be an object")
    validate_backup(document)
    return document
