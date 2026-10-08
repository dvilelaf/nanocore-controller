"""Fixtures for the server tests: a pedal that remembers live writes, a manual clock."""

import asyncio
import struct
import tempfile
import unittest
from pathlib import Path
from typing import Any

from aiohttp.test_utils import TestClient, TestServer

from nanocore_controller.ble_midi import MidiEvent
from nanocore_controller.device import NanocoreDevice
from nanocore_controller.errors import DeviceDisconnected
from nanocore_controller.nanocore_protocol import NanocoreResponse
from nanocore_controller.serve import NanocoreServer, ServeOptions, create_app
from support import PedalSession, load_fixture

TOKEN = "test-token-0123456789"


def runtime_bytes(slot: int, volume: int, effects: list[tuple[bool, int, list[float]]], chain: list[int]) -> bytes:
    body = bytearray((3, slot, volume))
    for enabled, variant, params in effects:
        body.extend((int(enabled), variant, len(params)))
        body.extend(struct.pack(f"<{len(params)}f", *params))
    body.extend((8, *chain))
    return bytes(body)


DEFAULT_EFFECTS: list[tuple[bool, int, list[float]]] = [
    (True, 0, [0.5, 0.25]),
    (False, 1, [0.1, 0.2, 0.3]),
    (True, 2, [0.75]),
    (False, 0, []),
    (True, 0, [0.5, 0.5]),
    (False, 0, [0.0]),
    (True, 3, [0.9, 0.8]),
    (False, 0, [0.4]),
]


class LivePedal(PedalSession):
    """A ``PedalSession`` whose live writes change what it reports next."""

    def __init__(self, names: dict[int, str] | None = None) -> None:
        document = load_fixture()
        super().__init__(document)
        self.runtime = bytearray(runtime_bytes(8, 84, DEFAULT_EFFECTS, list(range(8))))
        self.stored = bytes(self.runtime)  # what the flash holds; a recall reloads it
        self.names = names or {slot: f"Preset{slot + 1}" for slot in range(12)}
        self.save_gate: asyncio.Event | None = None
        self.save_started = asyncio.Event()
        self.saves = 0
        # Raw controller writes: CC number -> (snapshot entry, parameter index). A write shows in
        # the snapshot only ``lag`` seconds later on ``clock``, like the real pedal (about 20 ms).
        self.clock: Any = None
        self.lag = 0.0
        self.cc_params: dict[int, tuple[int, int]] = {}
        self._pending: list[tuple[float, Any]] = []

    def drop(self) -> None:
        """The link goes away: queries fail and the event stream ends."""

        self._connected = False
        self._events.put_nowait(None)

    def push_pedal_event(self) -> None:
        self.push_event(MidiEvent(0, bytes((0xB0, 1, 2))))

    def turn_knob(self, effect: int, index: int, value: float) -> None:
        """A change made on the pedal itself; the event is pushed separately."""

        self._edit(lambda state: state["effects"][effect][2].__setitem__(index, value))

    def _edit(self, change: Any) -> None:
        state = self._decode()
        change(state)
        self.runtime = bytearray(
            runtime_bytes(state["slot"], state["volume"], [tuple(e) for e in state["effects"]], state["chain"])  # type: ignore[misc]
        )

    def _decode(self) -> dict[str, Any]:
        from nanocore_controller.nanocore_protocol import parse_runtime_snapshot

        snapshot = parse_runtime_snapshot(bytes(self.runtime))
        return {
            "slot": snapshot.active_preset,
            "volume": snapshot.preset_volume,
            "effects": [[e.enabled, e.variant, list(e.params)] for e in snapshot.effects],
            "chain": list(snapshot.chain_order),
        }

    async def send(self, message: bytes) -> None:
        await super().send(message)
        if self.clock is not None and len(message) == 3 and message[0] & 0xF0 == 0xB0:
            target = self.cc_params.get(message[1])
            if target is not None:
                effect, index = target
                value = message[2] / 127
                due = self.clock() + self.lag
                self._pending.append((due, lambda: self.turn_knob(effect, index, value)))

    def _apply_due_writes(self) -> None:
        now = self.clock() if self.clock is not None else 0.0
        due = [item for item in self._pending if item[0] <= now]
        self._pending = [item for item in self._pending if item[0] > now]
        for _, change in due:
            change()

    async def query(self, command, payload=b"", *, sequence=None, timeout=5.0):  # type: ignore[no-untyped-def]
        if command == 0x63:
            self._apply_due_writes()
        if command == 0x40:
            if not self._connected:
                raise DeviceDisconnected("session is not connected")
            self.queries.append((command, payload))
            start = payload[0]
            slots = [s for s in range(start, start + 6) if s in self.names]
            body = bytearray((start, len(slots)))
            for slot in slots:
                body.extend((slot, 1))
                body.extend(self.names[slot].encode("ascii", "replace")[:8].ljust(8, b" "))
            return NanocoreResponse(1, command, 0, bytes(body))
        if command == 0x46 and self.save_gate is not None:
            self.save_started.set()
            await self.save_gate.wait()
        response = await super().query(command, payload, sequence=sequence, timeout=timeout)
        if command == 0x46:
            self.saves += 1
            self.stored = bytes(self.runtime)
            if self.live_name is not None:  # a rename lives in RAM until the save stores it
                self.names[payload[0]] = self.live_name
        if command == 0x6D:
            self._apply_live(payload)
        return response

    def _apply_live(self, payload: bytes) -> None:
        kind = payload[0]
        if kind == 9:
            self.live_name = None  # a recall loads the stored preset, name included
            self.runtime = bytearray(self.stored)
            self.runtime[1] = payload[1]
        elif kind == 1:
            value = struct.unpack("<f", payload[3:7])[0]
            params = self._decode()["effects"][payload[1]][2]
            if payload[2] < len(params):
                self._edit(lambda s: s["effects"][payload[1]][2].__setitem__(payload[2], value))
        elif kind == 2:
            params = list(struct.unpack(f"<{payload[3]}f", payload[4 : 4 + 4 * payload[3]]))
            self._edit(lambda s: s["effects"].__setitem__(payload[1], [s["effects"][payload[1]][0], payload[2], params]))
        elif kind == 3:
            self._edit(lambda s: s["effects"][payload[1]].__setitem__(0, bool(payload[2])))
        elif kind == 4:
            self._edit(lambda s: s.__setitem__("volume", payload[1]))
        elif kind == 5:
            self._edit(lambda s: s.__setitem__("chain", list(payload[2:10])))


class FakeClock:
    """A clock that only moves when a test says so; sleepers wake when it passes them."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleepers: list[tuple[float, asyncio.Future[None]]] = []
        self.requested: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.requested.append(seconds)
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.sleepers.append((self.now + seconds, future))
        await future

    async def advance(self, seconds: float) -> None:
        self.now += seconds
        await settle()
        for entry in list(self.sleepers):
            if entry[0] <= self.now:
                self.sleepers.remove(entry)
                if not entry[1].done():
                    entry[1].set_result(None)
        await settle()


async def settle(rounds: int = 30) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


class ServerTestCase(unittest.IsolatedAsyncioTestCase):
    options_kwargs: dict[str, Any] = {}
    default_options: dict[str, Any] = {"apply_delay": 0.0, "autosave": True}
    pedal_factory: Any = LivePedal

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.clock = FakeClock()
        self.pedal = self.pedal_factory()
        self.device = NanocoreDevice(self.pedal, sleep=self._no_sleep)
        self.options = ServeOptions(
            baseline_dir=self.tmp / "baselines",
            backup_dir=self.tmp / "backups",
            **{**self.default_options, **self.options_kwargs},
        )
        self.server = NanocoreServer(
            self.options, self.device, token=TOKEN, clock=self.clock, sleep=self.clock.sleep
        )
        self.test_server = TestServer(create_app(self.server), host="127.0.0.1")
        self.client = TestClient(self.test_server)
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        await asyncio.wait_for(self.server.connected_event.wait(), 5)
        self.auth = {"X-Nanocore-Token": TOKEN}

    @staticmethod
    async def _no_sleep(seconds: float) -> None:
        await asyncio.sleep(0)

    @property
    def port(self) -> int:
        return int(self.test_server.port or 0)

    async def get(self, path: str, **kwargs: Any) -> Any:
        kwargs.setdefault("headers", self.auth)
        return await self.client.get(path, **kwargs)

    async def post(self, path: str, payload: Any = None, **kwargs: Any) -> Any:
        kwargs.setdefault("headers", self.auth)
        if payload is not None:
            kwargs["json"] = payload
        return await self.client.post(path, **kwargs)

    async def edit(self, *ops: dict[str, Any], **extra: Any) -> Any:
        return await self.post("/api/edit", {"ops": list(ops), **extra})

    async def connect_ws(self, token: str = TOKEN) -> Any:
        ws = await self.client.ws_connect("/ws")
        await ws.send_json({"type": "auth", "token": token})
        first = await ws.receive_json(timeout=2)
        self.assertEqual(first["type"], "state")
        return ws

    def param_writes(self) -> list[bytes]:
        return [w for w in self.pedal.live_writes if w[0] == 1]

    async def wait_for_state(self, state: str) -> None:
        try:
            await wait_until(lambda: self.server.autosaver.state == state)
        except AssertionError:
            self.fail(f"autosave never reached {state!r}, is {self.server.autosaver.state!r}")


async def wait_until(predicate: Any, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.001)
