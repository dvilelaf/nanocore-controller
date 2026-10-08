"""The transport-independent session contract and a scriptable fake.

``SysexSession`` is what every higher layer depends on. ``BleMidiSession`` and
the USB ``AmidiSession`` implement it, which lets one device class work over
either transport and lets tests run without any hardware.
"""

import asyncio
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from .errors import DeviceDisconnected, DeviceStatusError
from .nanocore_protocol import NanocoreResponse

if TYPE_CHECKING:
    from .ble_midi import MidiEvent

Result = bytes | Exception | Callable[[bytes], "bytes | Exception"]

# Commands that only read state. After a timeout on any other command the
# outcome is unknown, which a session reports as ``DeviceTimeout.maybe_applied``.
READ_ONLY_COMMANDS = frozenset({0x63, 0x40, 0x36, 0x56, 0x45})


@runtime_checkable
class SysexSession(Protocol):
    """One open conversation with a NANOCORE, over BLE-MIDI or USB MIDI.

    Contract:

    * ``connect`` and ``close`` are idempotent. ``close`` never raises because
      the link is already dead.
    * ``query`` raises ``DeviceStatusError`` for a non-zero status,
      ``DeviceTimeout`` on timeout and ``DeviceDisconnected`` if the link drops.
    * ``events`` yields device-originated notifications (a knob or preset
      change on the pedal) that are not answers to a ``query``.
    """

    @property
    def connected(self) -> bool: ...

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def send(self, message: bytes) -> None: ...

    async def query(
        self,
        command: int,
        payload: bytes = b"",
        *,
        sequence: int | None = None,
        timeout: float = 5.0,
    ) -> NanocoreResponse: ...

    def events(self) -> AsyncIterator["MidiEvent"]: ...


class FakeSession:
    """A scriptable in-memory ``SysexSession`` for tests.

    ``script(command, *results)`` queues what successive queries for that
    command return: ``bytes`` become an OK response payload, an ``Exception``
    is raised, and a callable receives the request payload and returns either.
    When a queue holds a single result it repeats forever; with several, the
    last one repeats once the others are used. An unscripted command fails the
    test loudly.
    """

    def __init__(self, address: str = "AA:BB:CC:DD:EE:FF", *, adapter: str = "hci0") -> None:
        self.address = address
        self.adapter = adapter
        self.connect_count = 0
        self.close_count = 0
        self.sent: list[bytes] = []
        self.queries: list[tuple[int, bytes]] = []
        self._connected = False
        self._script: dict[int, deque[Result]] = defaultdict(deque)
        self._events: asyncio.Queue[MidiEvent | None] = asyncio.Queue()
        self.fail_connect: Exception | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    def script(self, command: int, *results: Result) -> None:
        self._script[command].extend(results)

    def push_event(self, event: "MidiEvent") -> None:
        self._events.put_nowait(event)

    async def connect(self) -> None:
        self.connect_count += 1
        if self.fail_connect is not None:
            raise self.fail_connect
        # A new connection starts a new event stream: end-of-stream markers left by an earlier close()
        # (the real session gives every subscriber its own queue) must not end it at once.
        kept = []
        while not self._events.empty():
            event = self._events.get_nowait()
            if event is not None:
                kept.append(event)
        for event in kept:
            self._events.put_nowait(event)
        self._connected = True

    async def close(self) -> None:
        self.close_count += 1
        self._connected = False
        self._events.put_nowait(None)

    async def send(self, message: bytes) -> None:
        if not self._connected:
            raise DeviceDisconnected("session is not connected")
        self.sent.append(message)

    async def query(
        self,
        command: int,
        payload: bytes = b"",
        *,
        sequence: int | None = None,
        timeout: float = 5.0,
    ) -> NanocoreResponse:
        if not self._connected:
            raise DeviceDisconnected("session is not connected")
        self.queries.append((command, payload))
        queue = self._script[command]
        if not queue:
            raise AssertionError(f"FakeSession: unscripted command 0x{command:02x}")
        result = queue[0] if len(queue) == 1 else queue.popleft()
        if callable(result):
            result = result(payload)
        if isinstance(result, Exception):
            raise result
        return NanocoreResponse(sequence or 1, command, 0, result)

    async def events(self) -> AsyncIterator["MidiEvent"]:
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event


def status_error(command: int, status: int) -> DeviceStatusError:
    """Shorthand for scripting a device-side failure in tests."""

    return DeviceStatusError(command, status)
