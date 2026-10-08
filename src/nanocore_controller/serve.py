"""Local HTTP and WebSocket server for the web editor (contract: docs/api.md).

A thin adapter over ``NanocoreDevice``. It binds to 127.0.0.1, authenticates
every API call with a per-run token, funnels every write through one worker
and keeps the permanent saving to ``AutoSaver``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import dataclasses
import hmac
import inspect
import json
import logging
import math
import os
import re
import secrets
import signal
import struct
import sys
import time
import unicodedata
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from . import assets_protocol as assets
from .autosave import AutoSaver, default_backup_dir
from .baselines import BaselineStore
from .ble_restore import build_restore_plan
from .ble_state import EFFECT_IDS
from .device import NanocoreDevice
from .ead_install import DECRYPTOR_ENV, decrypt_ead, resolve_decryptor
from .errors import (
    BackupError,
    DeviceDisconnected,
    DeviceNotFound,
    DeviceStatusError,
    DeviceTimeout,
    NanocoreError,
    PartialApplyError,
    ProtocolError,
    SlotChangedError,
    ValidationError,
    VerificationError,
)
from .ir_import import wav_to_ir
from .mappings import BLOCKS, PARAMETERS
from .midi import control_change
from .nanocore_protocol import (
    EFFECT_COUNT,
    MAX_ASSET_SLOT,
    MAX_EFFECT_INDEX,
    MAX_EFFECT_PARAMS,
    MAX_INPUT_GAIN_DB,
    MAX_MIDI_CHANNEL,
    MAX_PARAM_INDEX,
    MAX_PRESET_SLOT,
    MAX_PRESET_VOLUME,
    MAX_SETTING_VOLUME,
    MAX_VARIANT,
    AssetSlot,
    GlobalSettings,
    RuntimeSnapshot,
    preset_name_payload,
)

try:
    from aiohttp import WSMsgType, web
except ImportError:  # pragma: no cover - exercised only without the extra
    web = None  # type: ignore[assignment]
    WSMsgType = None  # type: ignore[assignment,misc]

T = TypeVar("T")
SERVER_KEY: Any = web.AppKey("server", object) if web is not None else "server"

log = logging.getLogger("nanocore.serve")

API_VERSION = "1"
MAX_BODY = 64 * 1024
MAX_MODEL_BODY = 4 * 1024 * 1024  # a model upload; only that route may exceed MAX_BODY
MAX_OPS = 64
MAX_NAME = 32
PARAM_RATE = 30
AUTH_TIMEOUT = 3.0
MAX_BACKOFF = 30.0
MAX_BUCKETS = 256
SHUTDOWN_TIMEOUT = 3.0
MAX_QUEUED_JOBS = 256
MAX_SOCKETS = 16
MAX_TIMEOUTS = 3
WS_AUTH_FAILED = 4401
LOOPBACK = "127.0.0.1"

TUNER_CC = 80
ALLOWED_CC = frozenset(
    {TUNER_CC}
    | {block.on_cc for block in BLOCKS.values()}
    | {block.type_cc for block in BLOCKS.values()}
    | {parameter.cc for parameter in PARAMETERS.values()}
)

_PATH = re.compile(r"(?:~|\.{1,2})?(?:/[^\s'\",;:()\[\]]+){2,}")
_BASELINE_ID = re.compile(r"\d{8}T\d{12}Z(?:-\d{1,3})?")
MODEL_KINDS = {"amp": assets.AMP, "ir": assets.IR}


def default_static_dir(package_dir: Path | None = None) -> Path | None:
    """The built web editor: shipped inside the package, or ``web/dist`` of a source checkout.

    The checkout location counts only when the package sits in a real source
    tree (it has a ``pyproject.toml`` and ``web/package.json`` next to ``src``),
    so an installed package never serves an unrelated directory.
    """

    package = package_dir if package_dir is not None else Path(__file__).resolve().parent
    candidates = [package / "web_dist"]
    root = package.parent.parent
    if (root / "pyproject.toml").is_file() and (root / "web" / "package.json").is_file():
        candidates.append(root / "web" / "dist")
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


@dataclass
class ServeOptions:
    host: str = LOOPBACK
    port: int = 0
    transport: str = "bluetooth"
    address: str | None = None
    adapter: str | None = None
    alsa_port: str | None = None
    autosave: bool = False
    autosave_delay: float = 3.0
    min_save_interval: float = 10.0
    read_only: bool = False
    require_token: bool = True
    apply_delay: float = 0.03
    baseline_dir: Path | None = None
    backup_dir: Path | None = None
    model_backup_dir: Path | None = None
    ead_decryptor: str | None = None  # command of the user's own .ead decryptor (docs/ead-decryptor.md)
    static_dir: Path | None = None
    verbose: bool = False


class ApiError(Exception):
    def __init__(
        self, status: int, code: str, message: str, *, maybe_applied: bool | None = None, **extra: Any
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.maybe_applied = maybe_applied
        self.extra = extra

    def body(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": redact(self.message)}
        if self.maybe_applied is not None:
            error["maybe_applied"] = self.maybe_applied
        error.update(self.extra)
        return {"error": error}


def redact(text: str) -> str:
    return _PATH.sub("<path>", text)


def clean_text(value: object) -> str:
    """Device-provided text as safe data: no control characters, at most 32 characters."""

    text = "".join(
        ch for ch in str(value) if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")
    )
    return text[:MAX_NAME].strip()


def api_error_for(exc: BaseException) -> ApiError:
    """Map an exception to the documented HTTP status and code."""

    if isinstance(exc, ApiError):
        return exc
    if isinstance(exc, SlotChangedError):
        return ApiError(409, "slot_changed", str(exc))
    if isinstance(exc, ValidationError):
        return ApiError(400, "validation", str(exc))
    if isinstance(exc, DeviceStatusError):
        return ApiError(502, "device_status", str(exc))
    if isinstance(exc, (DeviceDisconnected, DeviceNotFound)):
        return ApiError(503, "disconnected", "no link to the pedal")
    if isinstance(exc, DeviceTimeout):
        return ApiError(504, "device_timeout", str(exc), maybe_applied=exc.maybe_applied)
    if isinstance(exc, PartialApplyError):
        return ApiError(502, "verification_failed", f"only {exc.applied} of {exc.total} operations were applied")
    if isinstance(exc, VerificationError):
        return ApiError(502, "verification_failed", str(exc))
    if isinstance(exc, ProtocolError):
        return ApiError(502, "protocol", str(exc))
    if isinstance(exc, BackupError):
        return ApiError(500, "backup_failed", "a backup file could not be written or read")
    log.exception("unexpected error", exc_info=exc)
    return ApiError(500, "internal", "internal error")


# ---------------------------------------------------------------- edit requests


@dataclass(frozen=True)
class EditRequest:
    client_seq: int | None
    slot: int | None
    ops: tuple[dict[str, Any], ...]


def _is_int(value: object, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _is_unit(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(value) and 0 <= value <= 1
    except OverflowError:  # an integer too large to compare as a float
        return False


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ApiError(400, "validation", message)


def _exact_keys(op: dict[str, Any], keys: set[str]) -> None:
    _require(set(op) == keys | {"op"}, f"{op['op']} operation takes exactly: {', '.join(sorted(keys))}")


def parse_op(raw: object) -> dict[str, Any]:
    _require(isinstance(raw, dict) and isinstance(raw.get("op"), str), "every operation needs an op name")
    assert isinstance(raw, dict)
    kind = raw["op"]
    if kind == "param":
        _exact_keys(raw, {"effect", "index", "value"})
        _require(_is_int(raw["effect"], 0, MAX_EFFECT_INDEX), f"effect must be an integer from 0 to {MAX_EFFECT_INDEX}")
        _require(_is_int(raw["index"], 0, MAX_PARAM_INDEX), f"index must be an integer from 0 to {MAX_PARAM_INDEX}")
        _require(_is_unit(raw["value"]), "value must be a number from 0 to 1")
        return {"op": kind, "effect": raw["effect"], "index": raw["index"], "value": float(raw["value"])}
    if kind == "enabled":
        _exact_keys(raw, {"effect", "enabled"})
        _require(_is_int(raw["effect"], 0, MAX_EFFECT_INDEX), f"effect must be an integer from 0 to {MAX_EFFECT_INDEX}")
        _require(type(raw["enabled"]) is bool, "enabled must be a boolean")
        return {"op": kind, "effect": raw["effect"], "enabled": raw["enabled"]}
    if kind == "variant":
        _exact_keys(raw, {"effect", "variant", "params"})
        _require(_is_int(raw["effect"], 0, MAX_EFFECT_INDEX), f"effect must be an integer from 0 to {MAX_EFFECT_INDEX}")
        _require(_is_int(raw["variant"], 0, MAX_VARIANT), f"variant must be an integer from 0 to {MAX_VARIANT}")
        params = raw["params"]
        _require(
            isinstance(params, list) and len(params) <= MAX_EFFECT_PARAMS and all(_is_unit(p) for p in params),
            f"params must be a list of at most {MAX_EFFECT_PARAMS} numbers from 0 to 1",
        )
        return {
            "op": kind,
            "effect": raw["effect"],
            "variant": raw["variant"],
            "params": [float(p) for p in params],
        }
    if kind == "volume":
        _exact_keys(raw, {"value"})
        _require(_is_int(raw["value"], 0, MAX_PRESET_VOLUME), f"value must be an integer from 0 to {MAX_PRESET_VOLUME}")
        return {"op": kind, "value": raw["value"]}
    if kind == "name":
        _exact_keys(raw, {"name"})
        try:
            payload = preset_name_payload(raw["name"])  # the library's rule: 1 to 8 printable ASCII characters
        except ValidationError as exc:
            raise ApiError(400, "validation", str(exc)) from exc
        return {"op": kind, "name": payload[2:].decode("ascii")}
    if kind == "chain_order":
        _exact_keys(raw, {"order"})
        order = raw["order"]
        _require(
            isinstance(order, list)
            and all(type(entry) is int for entry in order)
            and sorted(order) == list(range(EFFECT_COUNT)),
            "order must be a permutation of 0 through 7",
        )
        return {"op": kind, "order": list(order)}
    if kind in ("amp", "ir"):
        _exact_keys(raw, {"slot"})
        _require(_is_int(raw["slot"], 0, MAX_ASSET_SLOT), f"slot must be an integer from 0 to {MAX_ASSET_SLOT}")
        return {"op": kind, "slot": raw["slot"]}
    if kind == "cc":
        _exact_keys(raw, {"cc", "value"})
        _require(type(raw["cc"]) is int and raw["cc"] in ALLOWED_CC, "cc is not an allowed controller number")
        _require(_is_int(raw["value"], 0, 127), "value must be an integer from 0 to 127")
        return {"op": kind, "cc": raw["cc"], "value": raw["value"]}
    raise ApiError(400, "bad_request", "unknown operation")


def parse_edit(body: object) -> EditRequest:
    """Validate a whole edit request. Nothing is sent to the pedal before this passes."""

    _require(isinstance(body, dict), "the body must be a JSON object")
    assert isinstance(body, dict)
    _require(set(body) <= {"client_seq", "ops", "slot"}, "unknown field in the edit request")
    seq = body.get("client_seq")
    _require(seq is None or _is_int(seq, 0, 2**53), "client_seq must be a non-negative integer")
    slot = body.get("slot")
    _require(slot is None or _is_int(slot, 0, MAX_PRESET_SLOT), f"slot must be an integer from 0 to {MAX_PRESET_SLOT}")
    ops = body.get("ops")
    _require(isinstance(ops, list) and 1 <= len(ops) <= MAX_OPS, f"ops must be a list of 1 to {MAX_OPS} operations")
    assert isinstance(ops, list)
    return EditRequest(seq, slot, tuple(parse_op(op) for op in ops))


def coalesce_key(op: dict[str, Any]) -> tuple[Any, ...] | None:
    """Operations with the same key replace each other: only the latest is sent."""

    if op["op"] == "param":
        return ("param", op["effect"], op["index"])
    if op["op"] == "cc":
        return ("cc", op["cc"])
    return None


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def parse_json(raw: bytes | str) -> Any:
    try:
        return json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, RecursionError, UnicodeDecodeError) as exc:
        raise ApiError(400, "bad_request", "malformed JSON") from exc


# ---------------------------------------------------------------- model helpers


def _round(value: float) -> float:
    return round(struct.unpack("<f", struct.pack("<f", value))[0], 6)


def live_from_snapshot(snapshot: RuntimeSnapshot) -> dict[str, Any]:
    return {
        "volume": snapshot.preset_volume,
        "chain_order": list(snapshot.chain_order) or list(range(EFFECT_COUNT)),
        "effects": [
            {
                "index": index,
                "effect_id": EFFECT_IDS[index],
                "enabled": effect.enabled,
                "variant": effect.variant,
                "params": [_round(value) for value in effect.params],
            }
            for index, effect in enumerate(snapshot.effects)
        ],
    }


def apply_op(live: dict[str, Any], op: dict[str, Any]) -> None:
    kind = op["op"]
    if kind == "volume":
        live["volume"] = op["value"]
    elif kind == "chain_order":
        live["chain_order"] = list(op["order"])
    elif kind in ("param", "enabled", "variant"):
        effect = live["effects"][op["effect"]]
        if kind == "param":
            if op["index"] < len(effect["params"]):
                effect["params"][op["index"]] = _round(op["value"])
        elif kind == "enabled":
            effect["enabled"] = op["enabled"]
        else:
            effect["variant"] = op["variant"]
            effect["params"] = [_round(value) for value in op["params"]]


def diff_live(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    ops: list[dict[str, Any]] = []
    if old["volume"] != new["volume"]:
        ops.append({"op": "volume", "value": new["volume"]})
    if old["chain_order"] != new["chain_order"]:
        ops.append({"op": "chain_order", "order": new["chain_order"]})
    for before, after in zip(old["effects"], new["effects"], strict=True):
        index = after["index"]
        if before["variant"] != after["variant"] or len(before["params"]) != len(after["params"]):
            ops.append(
                {"op": "variant", "effect": index, "variant": after["variant"], "params": after["params"]}
            )
        else:
            ops.extend(
                {"op": "param", "effect": index, "index": i, "value": value}
                for i, (was, value) in enumerate(zip(before["params"], after["params"], strict=True))
                if was != value
            )
        if before["enabled"] != after["enabled"]:
            ops.append({"op": "enabled", "effect": index, "enabled": after["enabled"]})
    return ops


class RateLimiter:
    """A token bucket per client: ``rate`` tokens per second, burst of ``rate``."""

    def __init__(self, rate: int, clock: Callable[[], float]) -> None:
        self._rate = rate
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str, cost: int) -> bool:
        # A request is never free, and one bigger than the burst still passes when the bucket is full.
        cost = min(max(cost, 1), self._rate)
        now = self._clock()
        tokens, stamp = self._buckets.get(key, (float(self._rate), now))
        tokens = min(float(self._rate), tokens + (now - stamp) * self._rate)
        allowed = cost <= tokens
        if allowed:
            tokens -= cost
        self._buckets[key] = (tokens, now)
        if len(self._buckets) > MAX_BUCKETS:
            self._evict(now)
        return allowed

    def _evict(self, now: float) -> None:
        """Forget clients whose bucket is full again, then the longest idle ones."""

        idle = [k for k, (t, s) in self._buckets.items() if t + (now - s) * self._rate >= self._rate]
        for key in idle:
            del self._buckets[key]
        excess = len(self._buckets) - MAX_BUCKETS
        if excess > 0:
            for key, _ in sorted(self._buckets.items(), key=lambda item: item[1][1])[:excess]:
                del self._buckets[key]


# ---------------------------------------------------------------- the single writer


class EditFailure(Exception):
    def __init__(self, cause: Exception, applied: int) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.applied = applied


@dataclass(eq=False)
class _Job:
    run: Callable[[], Awaitable[Any]]
    edit: EditRequest | None
    future: asyncio.Future[Any]


class Writer:
    """Runs jobs one at a time, in submission order."""

    def __init__(self, on_error: Callable[[Exception], None]) -> None:
        self._jobs: deque[_Job] = deque()
        self._wake = asyncio.Event()
        self._on_error = on_error
        self._closed = False
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._loop())

    def queued(self) -> list[_Job]:
        return list(self._jobs)

    async def submit(self, run: Callable[[], Awaitable[T]], edit: EditRequest | None = None) -> T:
        if self._closed:
            raise ApiError(503, "shutting_down", "the server is shutting down")
        if len(self._jobs) >= MAX_QUEUED_JOBS:
            raise ApiError(503, "busy", "too many operations are waiting for the pedal")
        job = _Job(run, edit, asyncio.get_running_loop().create_future())
        self._jobs.append(job)
        self._wake.set()
        return await job.future

    async def close(self) -> None:
        self._closed = True
        for job in self._jobs:
            if not job.future.done():
                job.future.set_exception(ApiError(503, "shutting_down", "the server is shutting down"))
        self._jobs.clear()
        self._wake.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            while not self._jobs:
                if self._closed:
                    return
                self._wake.clear()
                await self._wake.wait()
            job = self._jobs.popleft()
            try:
                result = await job.run()
            except Exception as exc:
                self._on_error(exc.cause if isinstance(exc, EditFailure) else exc)
                if not job.future.done():
                    job.future.set_exception(exc)
            else:
                if not job.future.done():
                    job.future.set_result(result)


# ---------------------------------------------------------------- global settings

SETTING_FIELDS: dict[str, tuple[str, int, int] | None] = {
    "wireless_enabled": None,
    "loopback_enabled": None,
    "input_gain_db": (
        f"an integer from -{MAX_INPUT_GAIN_DB} to {MAX_INPUT_GAIN_DB}",
        -MAX_INPUT_GAIN_DB,
        MAX_INPUT_GAIN_DB,
    ),
    "usb_volume": (f"an integer from 0 to {MAX_SETTING_VOLUME}", 0, MAX_SETTING_VOLUME),
    "bt_volume": (f"an integer from 0 to {MAX_SETTING_VOLUME}", 0, MAX_SETTING_VOLUME),
    "midi_channel": (f"an integer from 0 to {MAX_MIDI_CHANNEL}", 0, MAX_MIDI_CHANNEL),
}


def parse_settings(body: object) -> dict[str, Any]:
    """Validate a settings change: a non-empty subset of the writable fields, strictly typed."""

    _require(isinstance(body, dict), "the body must be a JSON object")
    assert isinstance(body, dict)
    _require(bool(body), "give at least one setting to change")
    unknown = sorted(set(body) - set(SETTING_FIELDS))
    _require(not unknown, f"unknown setting: {', '.join(map(str, unknown))}")
    for name, value in body.items():
        rule = SETTING_FIELDS[name]
        if rule is None:
            _require(type(value) is bool, f"{name} must be a boolean")
        else:
            description, low, high = rule
            _require(_is_int(value, low, high), f"{name} must be {description}")
    return dict(body)


def _settings_view(settings: GlobalSettings) -> dict[str, Any]:
    return dataclasses.asdict(settings)


# ---------------------------------------------------------------- the server


class NanocoreServer:
    """State, writer, autosave and connection handling behind the HTTP routes."""

    def __init__(
        self,
        options: ServeOptions,
        device: NanocoreDevice,
        *,
        token: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        auth_timeout: float = AUTH_TIMEOUT,
    ) -> None:
        if options.read_only and options.autosave:
            options = dataclasses.replace(options, autosave=False)  # a read-only server never stores
        self.options = options
        self.device = device
        # Baselines and unsaved-change tracking apply unless the server is read-only; whether
        # edits are also stored by a timer is options.autosave.
        self.stores = not options.read_only
        self.token = token or secrets.token_urlsafe(32)
        self.auth_timeout = auth_timeout
        self.extra_hosts: set[str] = set()
        self._clock = clock
        self._sleep = sleep
        self.baselines = BaselineStore(options.baseline_dir)
        self.writer = Writer(self._on_writer_error)
        self.autosaver = AutoSaver(
            device,
            self.baselines,
            options.backup_dir,
            enabled=options.autosave,
            delay=options.autosave_delay,
            min_interval=options.min_save_interval,
            clock=clock,
            sleep=sleep,
            submit=self.writer.submit,
            on_change=self._on_autosave_change,
        )
        self.limiter = RateLimiter(PARAM_RATE, clock)
        self.rev = 0
        self.connected = False
        self.connected_event = asyncio.Event()
        self.stopping = False
        self._live: dict[str, Any] | None = None
        self._slot: int | None = None
        self._names: dict[int, str] = {}
        self._renamed: set[int] = set()  # slots whose name was changed live and is not stored yet
        self._assets: dict[str, Any] | None = None
        self._last_sent: dict[tuple[Any, ...], float] = {}
        self._last_write_at = -math.inf
        self._timeouts = 0
        self.sockets = 0
        self._clients: set[web.WebSocketResponse] = set()
        self._outbox: asyncio.Queue[tuple[dict[str, Any], Any, Any]] = asyncio.Queue()
        self._drop = asyncio.Event()
        self._drop_reason = ""
        self._refresh_queued = False
        self._tasks: list[asyncio.Task[Any]] = []
        self._supervisor: asyncio.Task[None] | None = None

    # lifecycle

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.writer.start()
        self.autosaver.start()
        self._tasks = [loop.create_task(self._broadcaster())]
        self._supervisor = loop.create_task(self._supervise())

    async def close_clients(self) -> None:
        """Tell every connected page the server is going away, so shutdown does not wait for them."""

        self.stopping = True
        for client in list(self._clients):
            with contextlib.suppress(Exception):
                await client.close(code=1001)

    async def stop(self) -> None:
        self.stopping = True
        if self._supervisor is not None:
            self._supervisor.cancel()
            await asyncio.gather(self._supervisor, return_exceptions=True)
        for client in list(self._clients):
            with contextlib.suppress(Exception):
                await client.close(code=1001)
        try:
            await self.autosaver.close()
        finally:
            await self.writer.close()
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            with contextlib.suppress(Exception):
                await self.device.close()

    # state document

    def state_document(self) -> dict[str, Any]:
        preset = None
        if self._slot is not None:
            preset = {
                "slot": self._slot,
                "display_number": self._slot + 1,
                "name": self._names.get(self._slot),
            }
        return {
            "rev": self.rev,
            "connected": self.connected,
            "transport": self.options.transport,
            "read_only": self.options.read_only,
            "preset": preset,
            "live": copy.deepcopy(self._live),
            "autosave": self._autosave_view(),
        }

    def _autosave_view(self) -> dict[str, Any]:
        view = self.autosaver.snapshot()
        if view["error"] is not None:
            view["error"] = redact(view["error"])
        return view

    # publishing

    def _publish(self, message: dict[str, Any], *, only: Any = None, exclude: Any = None) -> None:
        self._outbox.put_nowait((message, only, exclude))

    def _on_autosave_change(self, view: dict[str, Any]) -> None:
        detail = redact(view["error"]) if view["error"] else None
        if view["state"] == "saved" and self._renamed:
            self._schedule_name_refresh()
        self._publish({"type": "autosave", "state": view["state"], "detail": detail})

    async def _broadcaster(self) -> None:
        while True:
            message, only, exclude = await self._outbox.get()
            targets = [only] if only is not None else [c for c in self._clients if c is not exclude]
            await asyncio.gather(*(self._deliver(client, message) for client in targets))

    async def _deliver(self, client: web.WebSocketResponse, message: dict[str, Any]) -> None:
        try:
            await asyncio.wait_for(client.send_json(message), 2.0)
        except Exception:
            self._clients.discard(client)
            with contextlib.suppress(Exception):
                await client.close()

    def add_client(self, client: web.WebSocketResponse) -> None:
        self._clients.add(client)
        self._publish({"type": "state", "rev": self.rev, "state": self.state_document()}, only=client)

    def remove_client(self, client: web.WebSocketResponse) -> None:
        self._clients.discard(client)

    # model

    def _ingest(self, snapshot: RuntimeSnapshot) -> tuple[bool, list[dict[str, Any]]]:
        """Fold a read of the pedal into the model; returns (slot changed, changes)."""

        live = live_from_snapshot(snapshot)
        slot = snapshot.active_preset
        if self._live is None or self._slot != slot:
            self._live, self._slot = live, slot
            self._assets = None
            self.rev += 1
            self.autosaver.slot_changed(slot)
            self._publish({"type": "state", "rev": self.rev, "state": self.state_document()})
            return True, []
        ops = diff_live(self._live, live)
        if ops:
            self._live = live
            self.rev += 1
            self._publish({"type": "patch", "rev": self.rev, "ops": ops})
        return False, ops

    async def _read_and_track(self) -> RuntimeSnapshot:
        """Read the pedal inside the writer; changes made on the pedal make the preset dirty."""

        snapshot = await self.device.read_live()
        self._timeouts = 0
        if self.stores:
            try:
                await self.autosaver.ensure_baseline(snapshot.active_preset)
            except Exception as exc:
                log.warning("no baseline for preset %d yet: %s", snapshot.active_preset + 1, exc)
        changed, ops = self._ingest(snapshot)
        if changed and self._renamed:
            await self._refresh_names()  # a live name of another preset did not survive the switch
        if ops and self.stores:
            slot = snapshot.active_preset
            try:
                await self.autosaver.ensure_baseline(slot)
            except Exception as exc:
                log.warning("no baseline for a change made on the pedal: %s", exc)
            self.autosaver.mark_dirty(slot)
        return snapshot

    async def _refresh_names(self) -> None:
        """Read the stored names again (inside the writer): the live names that were not stored are gone."""

        try:
            catalog = await self.device.catalog()
        except Exception as exc:
            log.warning("could not refresh the preset names: %s", exc)
            return
        shown = self._names.get(self._slot) if self._slot is not None else None
        self._names = {entry["slot"]: clean_text(entry["name"]) for entry in catalog}
        self._renamed.clear()
        now = self._names.get(self._slot) if self._slot is not None else None
        if now is not None and now != shown:
            self.rev += 1
            self._publish({"type": "patch", "rev": self.rev, "ops": [{"op": "name", "name": now}]})

    def _schedule_name_refresh(self) -> None:
        async def job() -> None:
            if self._renamed:
                await self._refresh_names()

        async def run() -> None:
            try:
                await self.writer.submit(job)
            except Exception as exc:
                log.warning("refresh of the preset names failed: %s", exc)

        self._tasks = [task for task in self._tasks if not task.done()]
        self._tasks.append(asyncio.get_running_loop().create_task(run()))

    def _on_writer_error(self, exc: Exception) -> None:
        if isinstance(exc, DeviceTimeout):
            self._timeouts += 1
            if self._timeouts >= MAX_TIMEOUTS:
                self._mark_disconnected("the pedal stopped answering")
        elif isinstance(exc, (DeviceDisconnected, DeviceNotFound)):
            self._mark_disconnected(str(exc) or "the link to the pedal was lost")

    # connection

    def _mark_disconnected(self, reason: str) -> None:
        self._drop_reason = reason
        self._drop.set()
        if self.connected:
            self.connected = False
            self.connected_event.clear()
            self._publish({"type": "connection", "connected": False, "reason": redact(reason)})

    async def _supervise(self) -> None:
        delay = 1.0
        while not self.stopping:
            self._drop.clear()
            self._drop_reason = ""
            try:
                if not self.device.connected:
                    await self.device.connect()
                await self.writer.submit(self._hydrate)
                delay = 1.0
                self.autosaver.reconnected()
                await self._watch()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("pedal connection failed: %s", exc)
                self._drop_reason = str(exc) or type(exc).__name__
            self._mark_disconnected(self._drop_reason or "the link to the pedal was lost")
            if self.device.connected:
                with contextlib.suppress(Exception):
                    await self.device.close()
            await self._sleep(delay)
            delay = min(delay * 2, MAX_BACKOFF)

    async def _hydrate(self) -> None:
        catalog = await self.device.catalog()
        snapshot = await self.device.read_live()
        self._names = {entry["slot"]: clean_text(entry["name"]) for entry in catalog}
        self._renamed.clear()
        self._live = live_from_snapshot(snapshot)
        self._slot = snapshot.active_preset
        self._assets = None
        if self.stores:
            try:
                await self.autosaver.ensure_baseline(self._slot)
            except Exception as exc:
                log.warning("no baseline for preset %d yet: %s", self._slot + 1, exc)
        self.rev += 1
        self.connected = True
        self._publish({"type": "connection", "connected": True, "reason": None})
        self._publish({"type": "state", "rev": self.rev, "state": self.state_document()})
        self.connected_event.set()

    async def _watch(self) -> None:
        loop = asyncio.get_running_loop()
        consumer = loop.create_task(self._consume_events())
        dropped = loop.create_task(self._drop.wait())
        try:
            done, _ = await asyncio.wait({consumer, dropped}, return_when=asyncio.FIRST_COMPLETED)
            if consumer in done and dropped not in done:
                exc = consumer.exception()
                self._drop_reason = str(exc) if exc else "the event stream of the pedal ended"
        finally:
            for task in (consumer, dropped):
                task.cancel()
            await asyncio.gather(consumer, dropped, return_exceptions=True)

    async def _consume_events(self) -> None:
        async for _event in self.device.events():
            if not self._refresh_queued:
                self._refresh_queued = True
                self._tasks = [task for task in self._tasks if not task.done()]
                self._tasks.append(asyncio.get_running_loop().create_task(self._refresh_from_pedal()))

    async def _refresh_from_pedal(self) -> None:
        async def job() -> None:
            self._refresh_queued = False
            await self._read_and_track()

        try:
            await self.writer.submit(job)
        except Exception as exc:
            self._refresh_queued = False
            log.warning("refresh after a pedal event failed: %s", exc)

    # operations used by the routes

    def check_writable(self) -> None:
        if self.stopping:
            raise ApiError(503, "shutting_down", "the server is shutting down")
        if self.options.read_only:
            raise ApiError(403, "read_only", "the server is read-only")

    def check_connected(self) -> None:
        if not self.connected:
            raise ApiError(503, "disconnected", "no link to the pedal")

    def rate_limit(self, client: str, edit: EditRequest) -> None:
        keys = {key for op in edit.ops if (key := coalesce_key(op)) is not None}
        others = sum(1 for op in edit.ops if coalesce_key(op) is None)
        if not self.limiter.allow(client, len(keys) + others):
            raise ApiError(429, "rate_limited", "too many edits")

    async def state(self) -> dict[str, Any]:
        if self.connected:

            async def job() -> None:
                await self._read_and_track()

            await self.writer.submit(job)
        return self.state_document()

    async def presets(self) -> list[dict[str, Any]]:
        if not self._names and self.connected:

            async def job() -> None:
                catalog = await self.device.catalog()
                self._names = {entry["slot"]: clean_text(entry["name"]) for entry in catalog}

            await self.writer.submit(job)
        return [
            {"slot": slot, "display_number": slot + 1, "name": name}
            for slot, name in sorted(self._names.items())
        ]

    async def settings(self) -> dict[str, Any]:
        """The pedal-wide settings, read inside the writer so they never interleave with a write."""

        self.check_connected()

        async def job() -> GlobalSettings:
            return await self.device.read_global_settings()

        return _settings_view(await self.writer.submit(job))

    async def write_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Change some pedal-wide settings. They belong to no preset: nothing here touches the saving state."""

        self.check_connected()

        async def job() -> GlobalSettings:
            return await self.device.write_global_settings(**changes)

        return _settings_view(await self.writer.submit(job))

    async def assets(self) -> dict[str, Any]:
        self.check_connected()
        if self._assets is None or self._assets["slot"] != self._slot:

            async def job() -> None:
                document = await self.device.read_state()
                preset = document["preset"]
                entries = document["assets"]
                self._assets = {
                    "slot": preset["slot"],
                    "amp": {"slot": preset["amp_slot"], "name": clean_text(entries["amp"].get("name") or "")},
                    "ir": {"slot": preset["ir_slot"], "name": clean_text(entries["ir"].get("name") or "")},
                }

            await self.writer.submit(job)
        assert self._assets is not None
        return {"amp": self._assets["amp"], "ir": self._assets["ir"]}

    # amplifier and IR models

    def model_backup_dir(self) -> Path:
        """Where the old content of a model slot is kept before it is replaced (next to the autosave backups)."""

        if self.options.model_backup_dir is not None:
            return Path(self.options.model_backup_dir)
        base = self.options.backup_dir if self.options.backup_dir is not None else default_backup_dir()
        return Path(base).parent / "model-backups"

    def _new_model_backup_path(self, kind: str, slot: int) -> Path:
        directory = self.model_backup_dir()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        path = directory / f"{kind}-{slot}-{stamp}.bin"
        suffix = 0
        while path.exists():
            suffix += 1
            path = directory / f"{kind}-{slot}-{stamp}-{suffix}.bin"
        return path

    @staticmethod
    def _model_view(info: AssetSlot, active: bool | None = None) -> dict[str, Any]:
        return {
            "slot": info.slot,
            "name": clean_text(info.name),
            "size": info.size,
            "crc32": info.checksum,
            "active": info.active if active is None else active,
        }

    async def models(self) -> dict[str, list[dict[str, Any]]]:
        """Every occupied slot of both storages, read inside the writer. Nothing is cached."""

        self.check_connected()

        async def job() -> dict[str, list[dict[str, Any]]]:
            return {
                name: [self._model_view(info) for info in await self.device.list_assets(kind) if info.present]
                for name, kind in MODEL_KINDS.items()
            }

        return await self.writer.submit(job)

    async def read_model(self, kind: str, slot: int) -> tuple[dict[str, Any], bytes]:
        self.check_connected()

        async def job() -> tuple[AssetSlot, bytes]:
            return await self.device.read_asset(MODEL_KINDS[kind], slot)

        info, data = await self.writer.submit(job)
        return self._model_view(info), data

    async def write_model(self, kind: str, slot: int, data: bytes, name: str | None) -> dict[str, Any]:
        """Replace one slot (the old content goes to a new backup file first). Touches no preset state."""

        self.check_connected()
        storage = MODEL_KINDS[kind]

        async def job() -> dict[str, Any]:
            try:
                path = await asyncio.to_thread(self._new_model_backup_path, kind, slot)
                info = await self.device.write_asset(storage, slot, data, safety_backup=path, name=name)
                active = (await self.device.asset_storage(storage)).active_slot == slot
            finally:
                self._assets = None  # the cached names of the active amp and IR may be stale, even after a failure
            return self._model_view(info, active)

        return await self.writer.submit(job)

    async def recall_preset(self, display_number: int, *, discard: bool = False) -> dict[str, Any]:
        self.check_connected()
        if self.options.autosave:
            await self.autosaver.flush()
        if self.autosaver.unsaved_slot is not None and self.autosaver.unsaved_slot == self._slot:
            if not discard:
                raise ApiError(
                    409,
                    "unsaved_changes",
                    f"the edits of preset {self._slot + 1} have not been stored; recalling "
                    "another preset would discard them",
                )

        async def job() -> None:
            snapshot = await self.device.select_preset(display_number)
            if self._renamed:
                await self._refresh_names()  # the recall dropped the live names that were not stored
            self._ingest(snapshot)
            self.autosaver.discard()
            if snapshot.active_preset not in self._names:
                catalog = await self.device.catalog()
                self._names = {e["slot"]: clean_text(e["name"]) for e in catalog}

        await self.writer.submit(job)
        return self.state_document()

    async def revert(self) -> dict[str, Any]:
        """Reload the stored version of the active preset, dropping the edits made in RAM."""

        self.check_connected()
        slot = self._slot
        if slot is None:
            raise ApiError(503, "disconnected", "no link to the pedal")

        async def job() -> None:
            snapshot = await self.device.select_preset(slot + 1)
            if self._renamed:
                await self._refresh_names()  # show the stored name again
            self._live = None  # publish a full state, not a diff
            self._ingest(snapshot)
            self._assets = None
            self.autosaver.discard()

        await self.writer.submit(job)
        return self.state_document()

    async def save_now(self) -> dict[str, Any]:
        self.check_connected()
        saved = await self.autosaver.flush()
        if saved and self._renamed:
            # The stored name now equals the live one; read it back from the catalog.
            async def job() -> None:
                if self._renamed:
                    await self._refresh_names()

            await self.writer.submit(job)
        return {"saved": saved, "autosave": self._autosave_view()}

    async def list_baselines(self, slot: int | None) -> list[dict[str, Any]]:
        target = slot if slot is not None else self._slot
        if target is None:
            return []
        entries = await asyncio.to_thread(self.baselines.list, target)
        return [
            {"id": e.id, "captured_at": e.captured_at, "name": clean_text(e.name) if e.name else None}
            for e in entries
        ]

    async def restore_baseline(self, slot: int, entry_id: str) -> dict[str, Any]:
        self.check_connected()

        async def job() -> None:
            active = (await self._read_and_track()).active_preset
            if active != slot:
                raise SlotChangedError(slot, active)
            try:
                document = await asyncio.to_thread(self.baselines.load, slot, entry_id)
            except BackupError as exc:
                raise ApiError(404, "not_found", "no such baseline") from exc
            await self._apply_document(slot, document, "restoring the baseline")

        await self.writer.submit(job)
        return self.state_document()

    async def _apply_document(self, slot: int, document: dict[str, Any], what: str) -> None:
        """Apply a backup document to the live state of the active preset ``slot`` (inside the writer).

        The preset becomes unsaved. A failure that could not be rolled back stops the autosave.
        """

        if self.stores:
            await self.autosaver.ensure_baseline(slot)
        safety = await asyncio.to_thread(self.autosaver.new_backup_path, slot)
        try:
            await self.device.restore_active(document, safety)
        except Exception as exc:
            with contextlib.suppress(Exception):
                self._ingest(await self.device.read_live())
            self._assets = None
            if getattr(exc, "rolled_back", None) is not True:
                self.autosaver.suspend(f"{what} failed and the pedal was not put back; nothing was stored")
            raise
        self._ingest(await self.device.read_live())
        self._assets = None
        self.autosaver.mark_dirty(slot)

    async def preset_file(self) -> tuple[dict[str, Any], str]:
        """The active preset as a document in the backup format, and the file name to offer for it."""

        self.check_connected()

        async def job() -> dict[str, Any]:
            await self._read_and_track()
            document = await self.device.read_state()
            slot = document["preset"]["slot"]
            shown = self._names.get(slot)
            if shown:
                document["preset"]["name"] = shown  # what the page shows, a live rename included
            return document

        document = await self.writer.submit(job)
        preset = document["preset"]
        label = re.sub(r"[^A-Za-z0-9_-]", "", str(preset.get("name") or ""))
        filename = f"preset-{preset['display_number']:02d}" + (f"-{label}" if label else "") + ".json"
        return document, filename

    async def load_preset_file(self, document: dict[str, Any]) -> dict[str, Any]:
        """Apply the content of a preset file to the ACTIVE preset, live. The user still has to save."""

        self.check_connected()
        build_restore_plan(document)  # refuse an invalid file before anything else happens
        wanted: str | None = None
        try:
            name = document["preset"].get("name")
            if isinstance(name, str):
                wanted = preset_name_payload(name)[2:].decode("ascii")
        except ValidationError:
            wanted = None  # the file has no usable name: keep the current one

        async def job() -> None:
            slot = (await self._read_and_track()).active_preset
            retargeted = copy.deepcopy(document)
            retargeted["preset"]["slot"] = slot
            retargeted["preset"]["display_number"] = slot + 1
            raw = bytearray.fromhex(retargeted["raw_snapshot"])
            raw[1] = slot
            retargeted["raw_snapshot"] = raw.hex()
            build_restore_plan(retargeted)
            await self._apply_document(slot, retargeted, "loading the preset file")
            if wanted is not None:
                op = {"op": "name", "name": wanted}
                await self._send(op)
                self._record([op], None, slot)
                self.autosaver.mark_dirty(slot)

        await self.writer.submit(job)
        return self.state_document()

    async def edit(self, request: EditRequest, *, origin: Any = None) -> dict[str, Any]:
        self.check_connected()

        async def job() -> int:
            return await self._run_edit(request, origin)

        try:
            applied = await self.writer.submit(job, request)
        except EditFailure as failure:
            error = api_error_for(failure.cause)
            error.extra["applied"] = failure.applied
            raise error from failure.cause
        return {"applied": applied, "rev": self.rev}

    def _superseded(self, request: EditRequest, index: int) -> bool:
        key = coalesce_key(request.ops[index])
        if any(coalesce_key(op) == key for op in request.ops[index + 1 :]):
            return True
        for later in self.writer.queued():
            if later.edit is None or later.edit.slot != request.slot:
                return False
            if any(coalesce_key(op) == key for op in later.edit.ops):
                return True
        return False

    async def _run_edit(self, request: EditRequest, origin: Any) -> int:
        applied = 0
        done: list[dict[str, Any]] = []
        slot: int | None = None
        try:
            await self._let_writes_settle()
            snapshot = await self._read_and_track()
            slot = snapshot.active_preset
            if request.slot is not None and request.slot != slot:
                raise SlotChangedError(request.slot, slot)
            if self.stores:
                await self.autosaver.ensure_baseline(slot)
            for index, op in enumerate(request.ops):
                key = coalesce_key(op)
                if key is not None and await self._throttle(request, index, key):
                    applied += 1
                    continue
                try:
                    await self._send(op)
                finally:
                    if key is not None:
                        self._last_sent[key] = self._clock()
                applied += 1
                done.append(op)
        except Exception as exc:
            raise EditFailure(exc, applied) from exc
        finally:
            if done and slot is not None:
                self._record(done, origin, slot)
                self.autosaver.mark_dirty(slot)
        if any(op["op"] == "cc" for op in done):
            with contextlib.suppress(Exception):
                await self._let_writes_settle()
                await self._read_and_track()
        return applied

    async def _let_writes_settle(self) -> None:
        """Wait until the pedal shows the last write: it acknowledges it about 20 ms earlier."""

        delay = self.options.apply_delay
        remaining = self._last_write_at + delay - self._clock()
        if delay > 0 and remaining > 0:
            await self._sleep(remaining)

    async def _throttle(self, request: EditRequest, index: int, key: tuple[Any, ...]) -> bool:
        """Wait out the per-parameter rate; true if a later value replaces this one."""

        while True:
            if self._superseded(request, index):
                return True
            wait = self._last_sent.get(key, -math.inf) + 1 / PARAM_RATE - self._clock()
            if wait <= 0:
                return False
            await self._sleep(wait)

    async def _send(self, op: dict[str, Any]) -> None:
        try:
            await self._send_op(op)
        finally:
            self._last_write_at = self._clock()

    async def _send_op(self, op: dict[str, Any]) -> None:
        kind, device = op["op"], self.device
        if kind == "param":
            await device.set_param(op["effect"], op["index"], op["value"])
        elif kind == "enabled":
            await device.set_enabled(op["effect"], op["enabled"])
        elif kind == "variant":
            await device.set_variant(op["effect"], op["variant"], op["params"])
        elif kind == "volume":
            await device.set_volume(op["value"])
        elif kind == "name":
            await device.set_preset_name(op["name"])
        elif kind == "chain_order":
            await device.set_chain_order(op["order"])
        elif kind == "amp":
            await device.select_amp(op["slot"])
        elif kind == "ir":
            await device.select_ir(op["slot"])
        else:
            await device.send_midi(control_change(1, op["cc"], op["value"]))

    def _record(self, ops: list[dict[str, Any]], origin: Any, slot: int) -> None:
        if self._live is None:
            return
        shown = [op for op in ops if op["op"] != "cc"]
        for op in shown:
            if op["op"] == "name":
                self._names[slot] = op["name"]
                self._renamed.add(slot)
            apply_op(self._live, op)
            if op["op"] in ("amp", "ir"):
                self._assets = None
        if shown:
            self.rev += 1
            self._publish({"type": "patch", "rev": self.rev, "ops": shown}, exclude=origin)


# ---------------------------------------------------------------- HTTP layer

SECURITY_HEADERS = {
    "X-Nanocore-Api": API_VERSION,
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
CSP = (
    "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self' ws://127.0.0.1:* ws://localhost:*; frame-ancestors 'none'"
)


def _allowed_hosts(request: web.Request, server: NanocoreServer) -> set[str]:
    sockname = request.transport.get_extra_info("sockname") if request.transport else None
    port = sockname[1] if sockname else 0
    return {f"127.0.0.1:{port}", f"localhost:{port}"} | server.extra_hosts


def _check_origin(request: web.Request, server: NanocoreServer) -> None:
    host = request.headers.get("Host", "")
    if host not in _allowed_hosts(request, server):
        raise ApiError(403, "forbidden", "host not allowed")
    origin = request.headers.get("Origin")
    if origin is not None and origin != f"http://{host}":
        raise ApiError(403, "forbidden", "origin not allowed")


def _token_matches(server: NanocoreServer, supplied: object) -> bool:
    if not server.options.require_token:
        return True  # --no-token: Host and Origin are still checked by the guard
    return isinstance(supplied, str) and hmac.compare_digest(
        supplied.encode("utf-8", "surrogatepass"), server.token.encode("utf-8")
    )


@web.middleware
async def guard(request: web.Request, handler: Callable[..., Awaitable[Any]]) -> web.StreamResponse:
    server: NanocoreServer = request.app[SERVER_KEY]
    try:
        _check_origin(request, server)
        if request.path.startswith("/api/") and request.path != "/api/health":
            if not _token_matches(server, request.headers.get("X-Nanocore-Token")):
                raise ApiError(401, "unauthorized", "missing or wrong token")
        response: web.StreamResponse = await handler(request)
    except web.HTTPRequestEntityTooLarge:
        response = _error_response(ApiError(413, "too_large", "request body too large"))
    except web.HTTPException as exc:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status, "bad_request")
        response = _error_response(ApiError(exc.status, code, exc.reason or "error"))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        response = _error_response(api_error_for(exc))
    if not response.prepared:
        response.headers.update(SECURITY_HEADERS)
    return response


def _error_response(error: ApiError) -> web.Response:
    return web.json_response(error.body(), status=error.status)


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


async def _read_json(request: web.Request) -> Any:
    if request.content_type != "application/json":
        raise ApiError(400, "bad_request", "the body must be application/json")
    try:
        raw = await request.read()
    except web.HTTPRequestEntityTooLarge as exc:
        raise ApiError(413, "too_large", "request body too large") from exc
    return parse_json(raw)


def _client_key(request: web.Request) -> str:
    return request.remote or "local"


def _query_slot(request: web.Request) -> int | None:
    raw = request.query.get("slot")
    if raw is None:
        return None
    if not (raw.isascii() and raw.isdigit() and int(raw) <= MAX_PRESET_SLOT):
        raise ApiError(400, "validation", f"slot must be an integer from 0 to {MAX_PRESET_SLOT}")
    return int(raw)


async def _read_bounded(request: web.Request, limit: int) -> bytes:
    """The raw body, read in pieces and refused (413) as soon as it passes ``limit``.

    This reads the stream itself, so the application-wide ``client_max_size`` does not apply here.
    """

    if request.content_length is not None and request.content_length > limit:
        raise ApiError(413, "too_large", "request body too large")
    body = bytearray()
    async for chunk in request.content.iter_chunked(64 * 1024):
        body += chunk
        if len(body) > limit:
            raise ApiError(413, "too_large", "request body too large")
    return bytes(body)


def _model_target(request: web.Request) -> tuple[str, int]:
    kind, raw = request.match_info["kind"], request.match_info["slot"]
    if kind not in MODEL_KINDS or not (raw.isascii() and raw.isdigit()):
        raise ApiError(404, "not_found", "no such model")
    return kind, int(raw)


def build_routes(server: NanocoreServer) -> list[Any]:
    async def health(request: web.Request) -> web.Response:
        return _json({"ok": True, "token_required": server.options.require_token})

    async def state(request: web.Request) -> web.Response:
        return _json(await server.state())

    async def presets(request: web.Request) -> web.Response:
        return _json({"presets": await server.presets()})

    async def assets(request: web.Request) -> web.Response:
        return _json(await server.assets())

    async def preset(request: web.Request) -> web.Response:
        server.check_writable()
        body = await _read_json(request)
        number = body.get("display_number") if isinstance(body, dict) else None
        discard = body.get("discard", False) if isinstance(body, dict) else False
        if (
            not isinstance(body, dict)
            or not set(body) <= {"display_number", "discard"}
            or not _is_int(number, 1, 128)
            or not isinstance(discard, bool)
        ):
            raise ApiError(
                400,
                "validation",
                "display_number must be an integer from 1 to 128 and discard a boolean",
            )
        assert isinstance(number, int)
        return _json(await server.recall_preset(number, discard=discard))

    async def edit(request: web.Request) -> web.Response:
        server.check_writable()
        parsed = parse_edit(await _read_json(request))
        server.rate_limit(_client_key(request), parsed)
        return _json(await server.edit(parsed), status=202)

    async def baselines(request: web.Request) -> web.Response:
        return _json(await server.list_baselines(_query_slot(request)))

    async def restore(request: web.Request) -> web.Response:
        server.check_writable()
        body = await _read_json(request)
        if (
            not isinstance(body, dict)
            or set(body) != {"slot", "id"}
            or not _is_int(body["slot"], 0, MAX_PRESET_SLOT)
            or not isinstance(body["id"], str)
            or not _BASELINE_ID.fullmatch(body["id"])
        ):
            raise ApiError(400, "validation", "body must be {slot, id} with a valid baseline id")
        return _json(await server.restore_baseline(body["slot"], body["id"]))

    async def get_settings(request: web.Request) -> web.Response:
        return _json(await server.settings())

    async def post_settings(request: web.Request) -> web.Response:
        server.check_writable()
        changes = parse_settings(await _read_json(request))
        if not server.limiter.allow(_client_key(request), 1):
            raise ApiError(429, "rate_limited", "too many edits")
        return _json(await server.write_settings(changes))

    async def get_models(request: web.Request) -> web.Response:
        listing = await server.models()
        # Whether an .ead file can be installed here: a decryptor is configured (the web editor says so).
        has_decryptor = bool((server.options.ead_decryptor or os.environ.get(DECRYPTOR_ENV, "")).strip())
        return _json({**listing, "ead": has_decryptor})

    async def get_model(request: web.Request) -> web.Response:
        kind, slot = _model_target(request)
        view, data = await server.read_model(kind, slot)
        label = re.sub(r"[^A-Za-z0-9_.-]", "", view["name"]).lstrip(".")
        filename = f"{kind}-{slot}" + (f"-{label}" if label else "") + ".bin"
        return web.Response(
            body=data,
            content_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    async def post_model(request: web.Request) -> web.Response:
        server.check_writable()
        kind, slot = _model_target(request)
        server.check_connected()
        if not server.limiter.allow(_client_key(request), 1):
            raise ApiError(429, "rate_limited", "too many edits")
        body = await _read_bounded(request, MAX_MODEL_BODY)
        if kind == "ir" and body.startswith(b"RIFF"):
            body = wav_to_ir(body)
        elif kind == "amp" and body[:4] in (b"SAPF", b"EADL"):
            command = resolve_decryptor(server.options.ead_decryptor)  # the user's own decryptor, if any
            body = await asyncio.to_thread(decrypt_ead, body, command)
        return _json(await server.write_model(kind, slot, body, request.query.get("name")))

    async def get_preset_file(request: web.Request) -> web.Response:
        document, filename = await server.preset_file()
        return web.Response(
            text=json.dumps(document, indent=2) + "\n",
            content_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    async def post_preset_file(request: web.Request) -> web.Response:
        server.check_writable()
        document = await _read_json(request)
        if not isinstance(document, dict):
            raise ApiError(400, "validation", "the body must be a preset file (a JSON object)")
        if not server.limiter.allow(_client_key(request), 1):
            raise ApiError(429, "rate_limited", "too many edits")
        return _json(await server.load_preset_file(document))

    async def save_now(request: web.Request) -> web.Response:
        server.check_writable()
        return _json(await server.save_now())

    async def revert(request: web.Request) -> web.Response:
        server.check_writable()
        return _json(await server.revert())

    return [
        web.get("/api/health", health),
        web.get("/api/state", state),
        web.get("/api/presets", presets),
        web.get("/api/assets", assets),
        web.post("/api/preset", preset),
        web.post("/api/edit", edit),
        web.get("/api/baselines", baselines),
        web.post("/api/baselines/restore", restore),
        web.post("/api/save-now", save_now),
        web.post("/api/revert", revert),
        web.get("/api/settings", get_settings),
        web.post("/api/settings", post_settings),
        web.get("/api/models", get_models),
        web.get("/api/models/{kind}/{slot}", get_model),
        web.post("/api/models/{kind}/{slot}", post_model),
        web.get("/api/preset-file", get_preset_file),
        web.post("/api/preset-file", post_preset_file),
        web.get("/ws", websocket),
        web.get("/{tail:.*}", static),
    ]


async def websocket(request: web.Request) -> web.StreamResponse:
    server: NanocoreServer = request.app[SERVER_KEY]
    if server.sockets >= MAX_SOCKETS:
        raise ApiError(503, "busy", "too many connections")
    server.sockets += 1
    try:
        return await _websocket(request, server)
    finally:
        server.sockets -= 1


async def _websocket(request: web.Request, server: NanocoreServer) -> web.StreamResponse:
    ws = web.WebSocketResponse(max_msg_size=MAX_BODY, heartbeat=30)
    ws.headers.update(SECURITY_HEADERS)
    await ws.prepare(request)
    try:
        first = await asyncio.wait_for(ws.receive(), server.auth_timeout)
    except TimeoutError:
        await ws.close(code=WS_AUTH_FAILED, message=b"auth timeout")
        return ws
    authenticated = False
    if first.type == WSMsgType.TEXT:
        try:
            message = parse_json(first.data)
        except ApiError:
            message = None
        authenticated = (
            isinstance(message, dict)
            and message.get("type") == "auth"
            and _token_matches(server, message.get("token"))
        )
    if not authenticated:
        await ws.close(code=WS_AUTH_FAILED, message=b"unauthorized")
        return ws
    server.add_client(ws)
    limiter_key = _client_key(request)
    try:
        async for incoming in ws:
            if incoming.type != WSMsgType.TEXT:
                continue
            await _ws_message(server, ws, limiter_key, incoming.data)
    finally:
        server.remove_client(ws)
    return ws


async def _ws_message(
    server: NanocoreServer, ws: web.WebSocketResponse, limiter_key: str, data: str
) -> None:
    seq = None
    try:
        message = parse_json(data)
        if not isinstance(message, dict) or message.get("type") != "edit":
            raise ApiError(400, "bad_request", "unknown message type")
        seq = message.get("client_seq") if _is_int(message.get("client_seq"), 0, 2**53) else None
        server.check_writable()
        parsed = parse_edit({k: v for k, v in message.items() if k != "type"})
        server.rate_limit(limiter_key, parsed)
        await server.edit(parsed, origin=ws)
    except Exception as exc:
        error = api_error_for(exc)
        await ws.send_json({"type": "error", "client_seq": seq, **error.body()})


_PLACEHOLDER = Path(__file__).with_name("web_placeholder.html")
_FALLBACK_PAGE = "<!doctype html><title>NANOCORE</title><p>The web editor is not installed.</p>"


async def static(request: web.Request) -> web.StreamResponse:
    server: NanocoreServer = request.app[SERVER_KEY]
    tail = request.match_info["tail"]
    if tail == "api" or tail.startswith("api/"):
        raise web.HTTPNotFound()
    root = server.options.static_dir
    headers = {"Content-Security-Policy": CSP}
    if root is not None and (root / "index.html").is_file():
        base = root.resolve()
        candidate = (base / tail).resolve() if tail else base / "index.html"
        inside = candidate.is_relative_to(base) and not any(
            part.startswith(".") for part in candidate.relative_to(base).parts
        )
        if inside and candidate.is_file():
            return web.FileResponse(candidate, headers=headers)
        if inside and not Path(tail).suffix:
            return web.FileResponse(base / "index.html", headers=headers)
        raise web.HTTPNotFound()
    if tail not in ("", "index.html"):
        raise web.HTTPNotFound()
    try:
        page = _PLACEHOLDER.read_text(encoding="utf-8")
    except OSError:
        page = _FALLBACK_PAGE
    return web.Response(text=page, content_type="text/html", headers=headers)


def page_url(host: str, port: int, token: str, require_token: bool) -> str:
    """The address to open: the token travels in the fragment, which never reaches the server."""

    base = f"http://{host}:{port}/"
    return f"{base}#token={token}" if require_token else base


def create_app(server: NanocoreServer) -> web.Application:
    app = web.Application(middlewares=[guard], client_max_size=MAX_BODY)
    app[SERVER_KEY] = server
    app.add_routes(build_routes(server))

    async def on_startup(_: web.Application) -> None:
        await server.start()

    async def on_cleanup(_: web.Application) -> None:
        await server.stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


# ---------------------------------------------------------------- entry points

_NEEDS_AIOHTTP = "the server needs aiohttp; install the 'serve' extra of nanocore-controller"


async def default_device_factory(options: ServeOptions) -> NanocoreDevice:
    if options.transport == "usb":
        from .discovery import discover_nanocore_midi_port_async
        from .usb_midi import AmidiSession

        port = options.alsa_port or await discover_nanocore_midi_port_async()
        return NanocoreDevice(AmidiSession(port), device_info={"port": port})
    from .ble_midi import BleMidiSession
    from .config import DeviceProfile, detect_adapter, load_profile

    try:
        profile = load_profile()
    except FileNotFoundError:
        profile = None
    address = options.address or (profile.address if profile else None)
    if not address:
        raise ValidationError("no Bluetooth device configured; pass --address or run 'nanocore configure'")
    adapter = options.adapter or (profile.adapter if profile else None) or detect_adapter()
    checked = DeviceProfile(address, adapter)
    session = BleMidiSession(checked.address, adapter=checked.adapter)
    return NanocoreDevice(session, device_info={"address": checked.address, "adapter": adapter})


async def serve(
    options: ServeOptions,
    *,
    device_factory: Callable[[ServeOptions], Any] | None = None,
    ready: Callable[[str], None] | None = None,
    stop: asyncio.Event | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Run the server until ``stop`` is set or SIGINT/SIGTERM arrives."""

    if web is None:
        raise RuntimeError(_NEEDS_AIOHTTP)
    if options.host != LOOPBACK:
        print(
            f"warning: binding to {options.host}; anyone who can reach it and has the token "
            "can change your presets",
            file=sys.stderr,
        )
    if not options.require_token:
        if options.host != LOOPBACK:
            raise ValidationError("the access token can only be disabled on the loopback address")
        print(
            "warning: running without an access token; any program on this computer can change "
            "your presets (web pages are still blocked by the Host and Origin checks)",
            file=sys.stderr,
        )
    built = (device_factory or default_device_factory)(options)
    device = await built if inspect.isawaitable(built) else built
    server = NanocoreServer(options, device, clock=clock, sleep=sleep)
    runner = web.AppRunner(create_app(server), handle_signals=False, shutdown_timeout=SHUTDOWN_TIMEOUT)
    await runner.setup()
    try:
        site = web.TCPSite(runner, options.host, options.port)
        await site.start()
        port = runner.addresses[0][1]
        if options.host != LOOPBACK:
            server.extra_hosts = {f"{options.host}:{port}"}
        url = page_url(options.host, port, server.token, options.require_token)
        print(url, flush=True)
        if ready is not None:
            ready(url)
        stop = stop or asyncio.Event()
        loop = asyncio.get_running_loop()
        installed = []
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
                installed.append(sig)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        try:
            await stop.wait()
        finally:
            for sig in installed:
                loop.remove_signal_handler(sig)
    finally:
        # runner.cleanup() waits for open connections before it reaches the app's own cleanup, and
        # a page keeps its WebSocket open, so close them first.
        await server.close_clients()
        await runner.cleanup()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nanocore serve", description="Local web editor server")
    parser.add_argument("--host", default=LOOPBACK, help="bind address (default 127.0.0.1; others are unsafe)")
    parser.add_argument("--port", type=int, default=0, help="TCP port (default: a free one)")
    parser.add_argument("--transport", choices=("bluetooth", "usb"), default="bluetooth")
    parser.add_argument("--address", help="NANOCORE Bluetooth address")
    parser.add_argument("--adapter", help="BlueZ adapter, e.g. hci0")
    parser.add_argument("--alsa-port", help="ALSA MIDI port, e.g. hw:6,0,0")
    parser.add_argument(
        "--autosave",
        action="store_true",
        help="store edits by themselves after --autosave-delay idle seconds (default: only when asked)",
    )
    parser.add_argument("--no-autosave", action="store_true", help=argparse.SUPPRESS)  # now the default
    parser.add_argument("--autosave-delay", type=float, default=3.0, help="idle seconds before a save")
    parser.add_argument("--read-only", action="store_true", help="reject every write")
    parser.add_argument(
        "--no-token",
        action="store_true",
        help="do not require the access token (loopback only; Host and Origin are still checked)",
    )
    parser.add_argument("--baseline-dir", type=Path)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument(
        "--ead-decryptor",
        help="program that decrypts an .ead file for the models panel (default: $NANOCORE_EAD_DECRYPTOR); "
        "see docs/ead-decryptor.md",
    )
    parser.add_argument(
        "--model-backup-dir",
        type=Path,
        help="where the old content of an amplifier or IR slot is kept before it is replaced "
        "(default: model-backups next to the autosave backups)",
    )
    parser.add_argument("--static-dir", type=Path, help="built web editor to serve")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535 or args.autosave_delay < 0:
        parser.error("--port must be 0..65535 and --autosave-delay must not be negative")
    if args.no_token and args.host != LOOPBACK:
        parser.error("--no-token is only allowed with the default loopback address")
    if web is None:
        print(f"error: {_NEEDS_AIOHTTP}", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    options = ServeOptions(
        host=args.host,
        port=args.port,
        transport=args.transport,
        address=args.address,
        adapter=args.adapter,
        alsa_port=args.alsa_port,
        autosave=args.autosave and not args.no_autosave,
        autosave_delay=args.autosave_delay,
        read_only=args.read_only,
        require_token=not args.no_token,
        baseline_dir=args.baseline_dir,
        backup_dir=args.backup_dir,
        model_backup_dir=args.model_backup_dir,
        ead_decryptor=args.ead_decryptor,
        static_dir=args.static_dir or default_static_dir(),
        verbose=args.verbose,
    )
    try:
        asyncio.run(serve(options))
    except KeyboardInterrupt:
        pass
    except NanocoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0
