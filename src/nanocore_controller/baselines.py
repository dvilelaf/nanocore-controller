"""Per-slot history of what a preset held before it was edited.

``NanocoreDevice.save_active`` can only back up the live state it is about to
store. To undo an unwanted permanent save, a copy of the stored preset has to
exist from before the first edit. A caller records one with ``record`` at that
moment; each slot keeps its newest ``keep`` entries.
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .backup_io import load_backup, write_backup
from .ble_restore import validate_backup
from .errors import BackupError, ValidationError
from .nanocore_protocol import MAX_PRESET_SLOT

_ENTRY_ID = re.compile(r"\d{8}T\d{12}Z(?:-\d{1,3})?")
_SLOT_DIRECTORY = re.compile(r"slot-(\d{3})")


@dataclass(frozen=True)
class BaselineEntry:
    slot: int
    id: str
    path: Path
    captured_at: str
    name: str | None


def default_baseline_root() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    root = Path(data_home) if data_home else None
    if root is None or not root.is_absolute():
        root = Path.home() / ".local" / "share"
    return root / "nanocore-controller" / "baselines"


class BaselineStore:
    """A directory of validated backups, grouped by preset slot."""

    def __init__(self, root: Path | str | None = None, *, keep: int = 20) -> None:
        if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
            raise ValidationError("keep must be a positive integer")
        self.root = Path(root) if root is not None else default_baseline_root()
        self.keep = keep

    def record(self, document: Mapping[str, Any]) -> BaselineEntry:
        """Store ``document`` (a full state document) as the newest entry of its slot."""

        validate_backup(document)
        slot = _slot_of(document)
        directory = self._directory(slot)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        for suffix in range(100):
            entry_id = stamp if suffix == 0 else f"{stamp}-{suffix}"
            path = directory / f"{entry_id}.json"
            try:
                write_backup(path, document)
            except BackupError:
                if path.exists():
                    continue  # two records in the same microsecond
                raise
            break
        else:
            raise BackupError(f"cannot find a free baseline name in {directory}")
        self._prune(directory)
        return _entry(slot, path, document)

    def list(self, slot: int) -> list[BaselineEntry]:
        """The entries of ``slot``, newest first. Unreadable files are skipped."""

        directory = self._directory(slot)
        if not directory.is_dir():
            return []
        entries = []
        for path in sorted(directory.glob("*.json"), reverse=True):
            if not _ENTRY_ID.fullmatch(path.stem):
                continue
            try:
                entries.append(_entry(slot, path, load_backup(path)))
            except (BackupError, ValidationError):
                continue
        return entries

    def load(self, slot: int, entry_id: str) -> dict[str, Any]:
        """The validated document of one entry."""

        if not isinstance(entry_id, str) or not _ENTRY_ID.fullmatch(entry_id):
            raise ValidationError("malformed baseline id")
        return load_backup(self._directory(slot) / f"{entry_id}.json")

    def _directory(self, slot: int) -> Path:
        if isinstance(slot, bool) or not isinstance(slot, int) or not 0 <= slot <= MAX_PRESET_SLOT:
            raise ValidationError(f"preset slot must be an integer from 0 to {MAX_PRESET_SLOT}")
        return self.root / f"slot-{slot:03d}"

    def _prune(self, directory: Path) -> None:
        files = sorted(p for p in directory.glob("*.json") if _ENTRY_ID.fullmatch(p.stem))
        for stale in files[: max(0, len(files) - self.keep)]:
            stale.unlink(missing_ok=True)


def _slot_of(document: Mapping[str, Any]) -> int:
    preset = document.get("preset") if isinstance(document, Mapping) else None
    slot = preset.get("slot") if isinstance(preset, Mapping) else None
    if isinstance(slot, bool) or not isinstance(slot, int):
        raise ValidationError("baseline document has no preset slot")
    return slot


def _entry(slot: int, path: Path, document: Mapping[str, Any]) -> BaselineEntry:
    name = document["preset"].get("name")
    return BaselineEntry(
        slot=slot,
        id=path.stem,
        path=path,
        captured_at=str(document.get("captured_at", "")),
        name=name if isinstance(name, str) else None,
    )
