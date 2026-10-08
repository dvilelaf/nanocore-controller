"""Debounced, flash-friendly permanent saving for a long-lived editor.

Live edits only change the working state of the pedal. ``AutoSaver`` decides
when that state is stored in the flash: after the user has been idle for
``delay`` seconds, never more often than ``min_interval``, and only for the
preset that was edited. Before the first edit of a slot it keeps a baseline
(see ``baselines.py``) so that an unwanted save can be undone.

The save is a job for the caller's single writer (``submit``); the methods
``ensure_baseline`` and ``mark_dirty`` are meant to be called from inside it.
"""

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from .baselines import BaselineStore
from .device import NanocoreDevice
from .errors import NanocoreError, SlotChangedError

T = TypeVar("T")

log = logging.getLogger("nanocore.autosave")

SAVED, DIRTY, SAVING, ERROR = "saved", "dirty", "saving", "error"


def default_backup_dir() -> Path:
    data_home = os.environ.get("XDG_DATA_HOME")
    root = Path(data_home) if data_home else None
    if root is None or not root.is_absolute():
        root = Path.home() / ".local" / "share"
    return root / "nanocore-controller" / "autosave-backups"


async def _run_directly(job: Callable[[], Awaitable[T]]) -> T:
    return await job()


class AutoSaver:
    def __init__(
        self,
        device: NanocoreDevice,
        baselines: BaselineStore,
        backup_dir: Path | str | None = None,
        *,
        enabled: bool = True,
        delay: float = 3.0,
        min_interval: float = 10.0,
        keep: int = 20,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        submit: Callable[[Callable[[], Awaitable[Any]]], Awaitable[Any]] = _run_directly,
        on_change: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._device = device
        self._baselines = baselines
        self._backup_dir = Path(backup_dir) if backup_dir is not None else default_backup_dir()
        self.enabled = enabled
        self._delay = delay
        self._min_interval = min_interval
        self._keep = keep
        self._clock = clock
        self._sleep = sleep
        self._submit = submit
        self._on_change = on_change
        self._state = SAVED
        self._error: str | None = None
        self._last_saved_at: str | None = None
        self._dirty_slot: int | None = None
        self._pending = False
        self._blocked = False
        self._last_edit = 0.0
        self._last_flash: float | None = None
        self._baselined: set[int] = set()
        self._wake = asyncio.Event()
        self._closing = False
        self._task: asyncio.Task[None] | None = None
        self._save_task: asyncio.Task[bool] | None = None

    @property
    def state(self) -> str:
        return self._state

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "state": self._state,
            "last_saved_at": self._last_saved_at,
            "error": self._error,
        }

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def ensure_baseline(self, slot: int) -> None:
        """Record the stored state of ``slot`` once per run, before its first edit."""

        if slot in self._baselined:
            return
        document = await self._device.read_state()
        actual = document["preset"]["slot"]
        if actual != slot:
            raise SlotChangedError(slot, actual)
        await asyncio.to_thread(self._baselines.record, document)
        self._baselined.add(slot)

    def mark_dirty(self, slot: int) -> None:
        self._dirty_slot = slot
        self._pending = True
        self._blocked = False
        self._last_edit = self._clock()
        self._set(DIRTY, None)
        if self.enabled:
            self._wake.set()

    @property
    def unsaved_slot(self) -> int | None:
        """The preset whose edits have not been stored yet, if any."""

        return self._dirty_slot if self._pending else None

    def suspend(self, message: str) -> None:
        """Drop what is pending and stop until the next edit: the live state cannot be trusted."""

        self._pending = False
        self._blocked = True
        self._set(ERROR, message)

    def slot_changed(self, slot: int) -> None:
        """The active preset is ``slot`` now (a footswitch or a recall)."""

        if self._pending and self._dirty_slot is not None and self._dirty_slot != slot:
            self._set(
                DIRTY,
                f"preset {self._dirty_slot + 1} was edited but is no longer active; "
                "its changes were not stored",
            )
        elif self._blocked:
            self._blocked = False
            self._wake.set()

    def discard(self) -> None:
        """The edits in RAM were thrown away (a recall or a revert): nothing is unsaved any more."""

        self._pending = False
        self._blocked = False
        self._dirty_slot = None
        self._set(SAVED, None)

    def reconnected(self) -> None:
        if self._pending and self.enabled:
            self._blocked = False
            self._set(DIRTY, None)
            self._wake.set()

    async def flush(self) -> bool:
        """Store pending edits now, ignoring the idle delay and the minimum interval.

        Returns whether anything was stored. A save that fails raises.
        """

        if not self._pending:
            return False
        return await self._save(raise_errors=True)

    async def close(self) -> None:
        """Stop the timer, let a save in flight finish, store what is pending."""

        self._closing = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._save_task is not None:
            await asyncio.gather(self._save_task, return_exceptions=True)
        if self.enabled and self._pending:
            try:
                await self._save(raise_errors=False)
            except Exception:
                log.exception("final autosave failed")

    async def _loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self.enabled and self._pending and not self._blocked:
                now = self._clock()
                wait = self._last_edit + self._delay - now
                if self._last_flash is not None:
                    wait = max(wait, self._last_flash + self._min_interval - now)
                if wait > 0:
                    await self._sleep(wait)
                    continue
                await self._save(raise_errors=False)
                break

    async def _save(self, *, raise_errors: bool) -> bool:
        if self._save_task is not None:
            await asyncio.shield(self._save_task)
            return False
        task = asyncio.get_running_loop().create_task(self._save_job(raise_errors))
        self._save_task = task
        return await asyncio.shield(task)

    async def _save_job(self, raise_errors: bool) -> bool:
        try:
            return await self._submit(self._store)
        except Exception as exc:
            if isinstance(exc, _Skipped):
                return False
            log.warning("autosave failed: %s", exc)
            self._set(ERROR, _describe(exc))
            if raise_errors:
                raise
            return False
        finally:
            self._save_task = None

    async def _store(self) -> bool:
        if not self._pending:
            return False
        slot = self._dirty_slot
        assert slot is not None
        active = (await self._device.read_live()).active_preset
        if active != slot:
            self._blocked = True
            self._set(
                DIRTY,
                f"preset {slot + 1} was edited but preset {active + 1} is active now; "
                "nothing was stored",
            )
            raise _Skipped
        if slot not in self._baselined:
            self._blocked = True
            self._set(ERROR, f"no baseline of preset {slot + 1}; nothing was stored")
            raise _Skipped
        path = await asyncio.to_thread(self._new_backup_path, slot)
        self._set(SAVING, None)
        self._last_flash = self._clock()
        await self._device.save_active(path)
        await asyncio.to_thread(self._prune, path.parent)
        self._pending = False
        self._last_saved_at = datetime.now(UTC).isoformat(timespec="seconds")
        self._set(SAVED, None)
        return True

    def new_backup_path(self, slot: int) -> Path:
        """A fresh, unused file name in the autosave backup directory of ``slot``."""

        return self._new_backup_path(slot)

    def _new_backup_path(self, slot: int) -> Path:
        directory = self._backup_dir / f"slot-{slot:03d}"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        path = directory / f"{stamp}.json"
        suffix = 0
        while path.exists():
            suffix += 1
            path = directory / f"{stamp}-{suffix}.json"
        return path

    def _prune(self, directory: Path) -> None:
        files = sorted(directory.glob("*.json"))
        for stale in files[: max(0, len(files) - self._keep)]:
            stale.unlink(missing_ok=True)

    def _set(self, state: str, error: str | None) -> None:
        if (state, error) == (self._state, self._error):
            return
        self._state = state
        self._error = error
        if self._on_change is not None:
            self._on_change(self.snapshot())


class _Skipped(Exception):
    """A save that was deliberately not attempted; the state is already set."""


def _describe(exc: Exception) -> str:
    return str(exc) if isinstance(exc, NanocoreError) else "the preset could not be stored"
