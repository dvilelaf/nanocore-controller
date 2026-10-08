"""Transport-independent request/response matching and event fan-out.

``QueryEngine`` turns "send one SysEx request, wait for its answer" into a
coroutine for any transport: the transport hands complete SysEx messages to
``deliver`` and gives the engine a ``send`` coroutine. ``EventHub`` fans
device-originated MIDI events out to any number of bounded consumers.
"""

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

from .errors import (
    DeviceStatusError,
    DeviceTimeout,
    ProtocolError,
    ValidationError,
)
from .nanocore_protocol import NanocoreResponse, ResponseAssembler, decode_response, encode_request
from .session import READ_ONLY_COMMANDS

T = TypeVar("T")

MAX_BACKLOG = 1024
_END = object()


class QueryEngine:
    """Serialised request/response matching over an arbitrary SysEx link.

    Only one query is in flight at a time. While it waits, ``deliver`` claims
    the responses that answer it and leaves everything else to the caller.
    """

    def __init__(
        self,
        send: Callable[[bytes], Awaitable[None]],
        *,
        logger: logging.Logger,
        initial_sequence: int | None = None,
    ) -> None:
        if initial_sequence is not None and not 0 <= initial_sequence <= 0xFFFF:
            raise ValidationError("initial_sequence must be a 16-bit integer")
        self._send = send
        self._logger = logger
        self._sequence = secrets.randbelow(0x10000) if initial_sequence is None else initial_sequence
        self._lock = asyncio.Lock()
        self._pending: tuple[int, int] | None = None
        self._inbox: asyncio.Queue[NanocoreResponse | Exception] = asyncio.Queue(MAX_BACKLOG)

    def deliver(self, message: bytes) -> bool:
        """Offer one complete SysEx message; return true if it answers the pending query."""

        pending = self._pending
        if pending is None:
            return False
        try:
            response = decode_response(message)
        except ValueError:
            return False
        if (response.sequence, response.command) != pending:
            return False
        try:
            self._inbox.put_nowait(response)
        except asyncio.QueueFull:
            self.fail(ProtocolError("response backlog overflowed"))
        return True

    def fail(self, error: Exception) -> None:
        """Abort the pending query, if any, with ``error``."""

        if self._pending is None:
            return
        self._clear_inbox()
        self._inbox.put_nowait(error)

    def reset(self) -> None:
        self._pending = None
        self._clear_inbox()

    async def query(
        self,
        command: int,
        payload: bytes = b"",
        *,
        sequence: int | None = None,
        timeout: float = 5.0,
    ) -> NanocoreResponse:
        async with self._lock:
            if sequence is None:
                self._sequence = (self._sequence + 1) & 0xFFFF
                sequence = self._sequence
            started = time.monotonic()
            try:
                response = await self._exchange(command, payload, sequence, timeout)
            except Exception as exc:
                self._logger.warning(
                    "query command=0x%02x sequence=%d failed after %.3fs: %s",
                    command,
                    sequence,
                    time.monotonic() - started,
                    exc,
                )
                raise
            finally:
                self._pending = None
            self._logger.debug(
                "query command=0x%02x sequence=%d status=0x%02x duration=%.3fs",
                command,
                sequence,
                response.status,
                time.monotonic() - started,
            )
            return response

    async def _exchange(
        self, command: int, payload: bytes, sequence: int, timeout: float
    ) -> NanocoreResponse:
        request = encode_request(command, payload, sequence=sequence)
        self._clear_inbox()
        self._pending = (sequence, command)
        await self._send(request)
        assembler = ResponseAssembler()
        try:
            async with asyncio.timeout(timeout):
                while True:
                    item = await self._inbox.get()
                    if isinstance(item, Exception):
                        raise item
                    try:
                        assembled = assembler.feed(item)
                    except ValueError as exc:
                        raise ProtocolError(
                            f"malformed streamed response to command 0x{command:02x}: {exc}"
                        ) from exc
                    if assembled is None:
                        continue
                    if assembled.status != 0:
                        raise DeviceStatusError(command, assembled.status, assembled.payload)
                    return assembled
        except TimeoutError as exc:
            raise DeviceTimeout(
                command, maybe_applied=command not in READ_ONLY_COMMANDS, timeout=timeout
            ) from exc

    def _clear_inbox(self) -> None:
        while not self._inbox.empty():
            self._inbox.get_nowait()


class EventStream(Generic[T]):
    """One consumer's bounded view of an ``EventHub``; also an async iterator."""

    def __init__(self, hub: "EventHub[T]", max_queued: int, *, closed: bool) -> None:
        self._hub = hub
        self._max_queued = max_queued
        self.queue: asyncio.Queue[object] = asyncio.Queue(max_queued + 1)
        self.dropped = 0
        if closed:
            self.queue.put_nowait(_END)

    def __aiter__(self) -> "EventStream[T]":
        return self

    async def __anext__(self) -> T:
        item = await self.queue.get()
        if item is _END:
            self.queue.put_nowait(_END)
            raise StopAsyncIteration
        return item  # type: ignore[return-value]

    async def aclose(self) -> None:
        self._hub.unsubscribe(self)

    def push(self, item: object) -> bool:
        """Queue ``item``, dropping the oldest entry if full; return true if one was dropped."""

        dropped = False
        if item is not _END and self.queue.qsize() >= self._max_queued:
            self.queue.get_nowait()
            self.dropped += 1
            dropped = True
        self.queue.put_nowait(item)
        return dropped


class EventHub(Generic[T]):
    """Fan-out of events to per-consumer bounded queues that drop the oldest."""

    def __init__(self, *, logger: logging.Logger, max_queued: int = 256) -> None:
        self._logger = logger
        self._max_queued = max_queued
        self._streams: list[EventStream[T]] = []
        self._closed = False

    def subscribe(self) -> EventStream[T]:
        stream: EventStream[T] = EventStream(self, self._max_queued, closed=self._closed)
        if not self._closed:
            self._streams.append(stream)
        return stream

    def unsubscribe(self, stream: EventStream[T]) -> None:
        if stream in self._streams:
            self._streams.remove(stream)

    def publish(self, item: T) -> None:
        for stream in self._streams:
            if stream.push(item):
                self._logger.warning(
                    "event consumer is too slow; dropped the oldest event (%d dropped so far)",
                    stream.dropped,
                )

    def close(self) -> None:
        """End every current stream; later subscribers get an ended stream until ``reopen``."""

        self._closed = True
        streams, self._streams = self._streams, []
        for stream in streams:
            stream.push(_END)

    def reopen(self) -> None:
        self._closed = False
