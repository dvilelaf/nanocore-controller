"""USB MIDI session that reads and writes the ALSA raw MIDI device node directly.

``amidi`` can only write one message at a time, so reads would be impossible
through it. The kernel exposes the same port as ``/dev/snd/midiC{card}D{device}``;
opening it non-blocking lets one session send requests and receive answers
with the same SysEx frames as over Bluetooth.
"""

import asyncio
import logging
import os
import re
from collections.abc import AsyncIterator, Callable

from .ble_midi import MidiEvent
from .errors import DeviceDisconnected, DeviceNotFound, ValidationError
from .nanocore_protocol import NanocoreResponse
from .sysex_query import EventHub, QueryEngine

logger = logging.getLogger("nanocore.usb")

_PORT = re.compile(r"hw:(\d+),(\d+)(?:,\d+)?")
READ_SIZE = 4096
MAX_SYSEX = 65536

OpenFn = Callable[[str, int], int]


def rawmidi_device_path(port: str) -> str:
    """Map an ALSA port such as ``hw:6,0,0`` to its device node."""

    match = _PORT.fullmatch(port)
    if match is None:
        raise ValidationError(f"ALSA MIDI port must look like hw:CARD,DEVICE[,SUB]: {port!r}")
    return f"/dev/snd/midiC{match.group(1)}D{match.group(2)}"


def _data_length(status: int) -> int:
    if status in (0xF1, 0xF3):
        return 1
    if status == 0xF2:
        return 2
    return 1 if 0xC0 <= status <= 0xDF else 2


class MidiStreamDecoder:
    """Split a raw MIDI byte stream into complete messages.

    Handles running status, SysEx split across reads, realtime bytes
    interleaved anywhere and stray data bytes. Realtime messages are emitted
    on their own and never disturb the message they interrupt. A SysEx cut
    short by another status byte, or longer than ``max_sysex``, is dropped.
    """

    def __init__(self, *, max_sysex: int = MAX_SYSEX) -> None:
        self.max_sysex = max_sysex
        self._status: int | None = None
        self._data = bytearray()
        self._sysex: bytearray | None = None

    def reset(self) -> None:
        self._status = None
        self._data.clear()
        self._sysex = None

    def feed(self, chunk: bytes) -> list[bytes]:
        messages: list[bytes] = []
        for byte in chunk:
            if byte >= 0xF8:
                messages.append(bytes((byte,)))
            elif byte >= 0x80:
                self._on_status(byte, messages)
            else:
                self._on_data(byte, messages)
        return messages

    def _on_status(self, byte: int, messages: list[bytes]) -> None:
        if self._sysex is not None:
            if byte == 0xF7:
                self._sysex.append(byte)
                messages.append(bytes(self._sysex))
                self._sysex = None
                return
            self._sysex = None
        self._data.clear()
        if byte == 0xF7 or byte in (0xF4, 0xF5):
            self._status = None
        elif byte == 0xF0:
            self._status = None
            self._sysex = bytearray((byte,))
        elif byte == 0xF6:
            self._status = None
            messages.append(bytes((byte,)))
        else:
            self._status = byte

    def _on_data(self, byte: int, messages: list[bytes]) -> None:
        if self._sysex is not None:
            self._sysex.append(byte)
            if len(self._sysex) > self.max_sysex:
                self._sysex = None
            return
        if self._status is None:
            return
        self._data.append(byte)
        if len(self._data) == _data_length(self._status):
            messages.append(bytes((self._status,)) + bytes(self._data))
            self._data.clear()
            if self._status >= 0xF0:
                self._status = None


class AmidiSession:
    """``SysexSession`` over a USB MIDI port, e.g. ``hw:6,0,0``.

    ``open_fn`` has the signature of ``os.open`` and exists so tests can hand
    in one end of a socket pair instead of a real device node.
    """

    def __init__(
        self,
        port: str,
        on_event: Callable[[MidiEvent], None] | None = None,
        *,
        open_fn: OpenFn = os.open,
        initial_sequence: int | None = None,
        max_queued_events: int = 256,
        write_timeout: float = 5.0,
    ) -> None:
        self.port = port
        self.on_event = on_event
        self.write_timeout = write_timeout
        self._open = open_fn
        self._fd: int | None = None
        self.decoder = MidiStreamDecoder()
        self._engine = QueryEngine(self.send, logger=logger, initial_sequence=initial_sequence)
        self._hub: EventHub[MidiEvent] = EventHub(logger=logger, max_queued=max_queued_events)
        self._write_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._fd is not None

    def events(self) -> AsyncIterator[MidiEvent]:
        """Device-originated MIDI events; the stream ends when the session closes."""

        return self._hub.subscribe()

    async def connect(self) -> None:
        if self._fd is not None:
            return
        path = rawmidi_device_path(self.port)
        try:
            fd = self._open(path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as exc:
            raise DeviceNotFound(f"cannot open USB MIDI device {path}: {exc.strerror or exc}") from exc
        self.decoder = MidiStreamDecoder()
        self._engine.reset()
        self._hub.reopen()
        try:
            asyncio.get_running_loop().add_reader(fd, self._on_readable)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        logger.info("opened %s for %s", path, self.port)

    async def close(self) -> None:
        self._release("session closed")

    def _release(self, reason: str) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            loop = asyncio.get_running_loop()
            loop.remove_reader(fd)
            loop.remove_writer(fd)
            try:
                os.close(fd)
            except OSError as exc:
                logger.debug("closing the MIDI port failed: %s", exc)
            logger.info("closed %s: %s", self.port, reason)
        self.decoder.reset()
        self._engine.fail(DeviceDisconnected(f"USB MIDI {reason}"))
        self._hub.close()

    def _on_readable(self) -> None:
        fd = self._fd
        if fd is None:
            return
        try:
            data = os.read(fd, READ_SIZE)
        except BlockingIOError:
            return
        except OSError as exc:
            logger.warning("read from %s failed: %s", self.port, exc)
            self._release(f"read failed: {exc.strerror or exc}")
            return
        if not data:
            logger.warning("%s was closed by the device", self.port)
            self._release("port closed by the device")
            return
        try:
            messages = self.decoder.feed(data)
        except Exception as exc:
            self.decoder.reset()
            logger.warning("dropped undecodable MIDI input: %s", exc)
            return
        for message in messages:
            self._dispatch(MidiEvent(timestamp=0, message=message))

    def _dispatch(self, event: MidiEvent) -> None:
        answered = event.message[0] == 0xF0 and self._engine.deliver(event.message)
        if not answered:
            self._hub.publish(event)
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception as exc:
                logger.warning("event callback failed: %s", exc, exc_info=True)

    async def send(self, message: bytes) -> None:
        if not message:
            raise ValidationError("message must not be empty")
        if self._fd is None:
            raise DeviceDisconnected("USB MIDI session is not connected")
        try:
            async with asyncio.timeout(self.write_timeout), self._write_lock:
                await self._write_all(message)
        except TimeoutError as exc:
            # Part of a message may be on the wire: drop the link so the next use starts clean.
            self._release("write timed out")
            raise DeviceDisconnected("USB MIDI write timed out") from exc

    async def _write_all(self, message: bytes) -> None:
        view = memoryview(message)
        while view:
            fd = self._fd
            if fd is None:
                raise DeviceDisconnected("USB MIDI session closed during a write")
            try:
                written = os.write(fd, view)
            except BlockingIOError:
                await self._writable(fd)
                continue
            except OSError as exc:
                raise DeviceDisconnected(f"USB MIDI write failed: {exc.strerror or exc}") from exc
            view = view[written:]

    @staticmethod
    async def _writable(fd: int) -> None:
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[None] = loop.create_future()

        def wake() -> None:
            if not ready.done():
                ready.set_result(None)

        loop.add_writer(fd, wake)
        try:
            await ready
        finally:
            loop.remove_writer(fd)

    async def query(
        self,
        command: int,
        payload: bytes = b"",
        *,
        sequence: int | None = None,
        timeout: float = 5.0,
    ) -> NanocoreResponse:
        return await self._engine.query(command, payload, sequence=sequence, timeout=timeout)
