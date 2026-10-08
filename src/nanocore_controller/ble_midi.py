"""BLE-MIDI packet codec, capture support, and optional transport."""

import asyncio
import json
import logging
import re
import shutil
import subprocess
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import DEFAULT_ADAPTER, validate_adapter_name, validate_bluetooth_address
from .errors import DeviceDisconnected, DeviceNotFound
from .nanocore_protocol import NanocoreResponse
from .sysex_query import EventHub, QueryEngine

logger = logging.getLogger("nanocore.ble")

BLE_MIDI_SERVICE_UUID = "03b80e5a-ede8-4b33-a751-6ce34ec4c700"
BLE_MIDI_CHARACTERISTIC_UUID = "7772e5db-3868-4112-a1a9-f2669d106bf3"
GATTTOOL_CCC_HANDLE = "0x0014"
GATTTOOL_MIDI_HANDLE = "0x0013"
DEFAULT_MAX_RECORDED = 10_000


@dataclass(frozen=True)
class MidiEvent:
    timestamp: int
    message: bytes


def _message_length(status: int) -> int:
    if 0x80 <= status <= 0xEF:
        return 2 if (status & 0xE0) in (0xC0, 0xD0) else 3
    if status in (0xF1, 0xF3):
        return 2
    if status == 0xF2:
        return 3
    if status == 0xF0:
        return -1
    if status in (0xF4, 0xF5, 0xF7):
        return 1
    if 0xF8 <= status <= 0xFF:
        return 1
    raise ValueError(f"unsupported MIDI status byte: 0x{status:02x}")


def encode_packet(message: bytes, timestamp: int = 0) -> bytes:
    """Wrap one complete MIDI message in a BLE-MIDI packet."""

    if not message:
        raise ValueError("message must not be empty")
    if not 0 <= timestamp <= 8191:
        raise ValueError("timestamp must be from 0 to 8191")
    if message[0] < 0x80:
        raise ValueError("message must start with a MIDI status byte")
    if any(byte > 0x7F for byte in message[1:] if byte != 0xF7):
        raise ValueError("MIDI data bytes must be 7-bit values")
    header = 0x80 | ((timestamp >> 7) & 0x3F)
    event_timestamp = 0x80 | (timestamp & 0x7F)
    return bytes((header, event_timestamp)) + message


def decode_packet(packet: bytes) -> list[MidiEvent]:
    """Decode complete MIDI messages from one BLE-MIDI packet."""

    return BleMidiDecoder().feed(packet)


class BleMidiDecoder:
    """Decode BLE-MIDI packets while retaining running status and SysEx state."""

    def __init__(self) -> None:
        self.running_status: int | None = None
        self._sysex: bytearray | None = None
        self._sysex_timestamp: int | None = None

    def reset(self) -> None:
        self.running_status = None
        self._sysex = None
        self._sysex_timestamp = None

    def feed(self, packet: bytes) -> list[MidiEvent]:
        try:
            return self._feed(packet)
        except ValueError:
            self.reset()
            raise

    def _feed(self, packet: bytes) -> list[MidiEvent]:
        if len(packet) < 2 or packet[0] & 0x80 == 0:
            raise ValueError("BLE-MIDI packet must start with a header byte")
        high = packet[0] & 0x3F
        index = 1
        events: list[MidiEvent] = []

        while index < len(packet):
            if self._sysex is not None and packet[index] < 0x80:
                index = self._consume_sysex(packet, index, self._sysex_timestamp or 0, events)
                continue
            timestamp_byte = packet[index]
            if timestamp_byte & 0x80 == 0:
                raise ValueError("each MIDI message must have a timestamp byte")
            timestamp = (high << 7) | (timestamp_byte & 0x7F)
            index += 1
            if index >= len(packet):
                raise ValueError("timestamp has no MIDI message")

            first = packet[index]
            if self._sysex is not None and first < 0x80:
                index = self._consume_sysex(packet, index, timestamp, events)
                continue

            if first & 0x80:
                status = first
                index += 1
                if 0x80 <= status <= 0xEF:
                    self.running_status = status
                elif status < 0xF8:
                    self.running_status = None
            elif self.running_status is not None:
                status = self.running_status
            else:
                raise ValueError("MIDI data byte has no running status")

            if status == 0xF0:
                self._sysex = bytearray((status,))
                self._sysex_timestamp = timestamp
                index = self._consume_sysex(packet, index, timestamp, events)
                continue

            length = _message_length(status)
            data_length = length - 1
            if index + data_length > len(packet):
                raise ValueError("truncated MIDI message")
            data = packet[index : index + data_length]
            if any(byte & 0x80 for byte in data):
                raise ValueError("status byte found inside MIDI message data")
            events.append(MidiEvent(timestamp=timestamp, message=bytes((status,)) + data))
            index += data_length
        # BLE-MIDI running status is scoped to one packet. SysEx is the only
        # MIDI message state that may continue into a later packet.
        self.running_status = None
        return events

    def _consume_sysex(
        self,
        packet: bytes,
        index: int,
        timestamp: int,
        events: list[MidiEvent],
    ) -> int:
        assert self._sysex is not None
        while index < len(packet):
            byte = packet[index]
            if byte & 0x80 and byte != 0xF7 and index + 1 < len(packet) and packet[index + 1] == 0xF7:
                index += 1
                byte = packet[index]
            if byte >= 0xF8:
                events.append(MidiEvent(timestamp=timestamp, message=bytes((byte,))))
                index += 1
                continue
            if byte & 0x80 and byte != 0xF7:
                raise ValueError("status byte found inside SysEx message")
            self._sysex.append(byte)
            index += 1
            if len(self._sysex) > MAX_SYSEX_BYTES:
                self._sysex = None
                self._sysex_timestamp = None
                raise ValueError("SysEx message is too long")
            if byte == 0xF7:
                events.append(MidiEvent(timestamp=timestamp, message=bytes(self._sysex)))
                self._sysex = None
                self._sysex_timestamp = None
                break
        return index


MAX_SYSEX_BYTES = 65536


class BleMidiSession:
    """BLE-MIDI session implementing ``SysexSession``, with optional packet capture.

    Bleak is imported lazily so USB-only commands do not require Bluetooth
    dependencies. The session is deliberately capture-first: it does not
    send a handshake or undocumented command automatically.

    ``raw_packets`` and ``recorded_events`` only fill when ``record`` is true,
    and stop growing at ``max_recorded`` entries.
    """

    def __init__(
        self,
        address: str,
        on_event: Callable[[MidiEvent], None] | None = None,
        *,
        adapter: str = DEFAULT_ADAPTER,
        initial_sequence: int | None = None,
        record: bool = False,
        max_recorded: int = DEFAULT_MAX_RECORDED,
        max_queued_events: int = 256,
        scan_timeout: float = 15.0,
        connect_timeout: float = 20.0,
        write_timeout: float = 5.0,
        close_timeout: float = 5.0,
    ):
        self.address = address
        self.adapter = adapter
        self.on_event = on_event
        self.client: Any = None
        self.record = record
        self.max_recorded = max_recorded
        self.raw_packets: list[bytes] = []
        self.recorded_events: list[MidiEvent] = []
        self.decoder = BleMidiDecoder()
        self.scan_timeout = scan_timeout
        self.connect_timeout = connect_timeout
        self.write_timeout = write_timeout
        self.close_timeout = close_timeout
        self._engine = QueryEngine(self.send, logger=logger, initial_sequence=initial_sequence)
        self._hub: EventHub[MidiEvent] = EventHub(logger=logger, max_queued=max_queued_events)
        self._lifecycle = asyncio.Lock()
        self._notifications_started = False
        self._link_lost = False
        self._recording_full_logged = False

    @property
    def connected(self) -> bool:
        return self.client is not None and bool(self.client.is_connected) and not self._link_lost

    def events(self) -> AsyncIterator[MidiEvent]:
        """Device-originated MIDI events; the stream ends when the session closes."""

        return self._hub.subscribe()

    async def connect(self, *, handshake: bool = False, subscribe: bool = True) -> None:
        try:
            from bleak import BleakClient, BleakScanner
        except ImportError as exc:
            raise RuntimeError("BLE support requires the optional 'ble' dependency") from exc
        async with self._lifecycle:
            if self.connected:
                return
            await self._release_client()
            self._reset_state()
            device = await self._find_device(BleakScanner)
            client = BleakClient(
                device,
                adapter=self.adapter,
                timeout=self.connect_timeout,
                disconnected_callback=self._on_disconnected,
            )
            self.client = client
            ready = False
            try:
                try:
                    async with asyncio.timeout(self.connect_timeout + 5.0):
                        await client.connect()
                except Exception as exc:
                    raise DeviceDisconnected(f"BLE-MIDI connection failed: {exc}") from exc
                if subscribe:
                    try:
                        async with asyncio.timeout(self.write_timeout):
                            await client.start_notify(
                                BLE_MIDI_CHARACTERISTIC_UUID, self.handle_notification
                            )
                    except Exception as exc:
                        raise DeviceDisconnected(
                            f"BLE-MIDI notification subscription failed: {exc}"
                        ) from exc
                    self._notifications_started = True
                ready = True
            finally:
                if not ready:
                    await self._release_client()
            logger.info("connected to %s on %s", self.address, self.adapter)

    async def _find_device(self, scanner: Any) -> Any:
        try:
            async with asyncio.timeout(self.scan_timeout + 5.0):
                device = await scanner.find_device_by_address(
                    self.address,
                    timeout=self.scan_timeout,
                    adapter=self.adapter,
                )
        except Exception as exc:
            raise DeviceNotFound(
                f"BLE-MIDI scan failed on {self.adapter} for {self.address}: {exc}"
            ) from exc
        if device is None:
            raise DeviceNotFound(f"BLE-MIDI device not found on {self.adapter}: {self.address}")
        return device

    def _reset_state(self) -> None:
        self.decoder = BleMidiDecoder()
        self._engine.reset()
        self._hub.reopen()
        self._notifications_started = False
        self._link_lost = False

    def _on_disconnected(self, client: Any = None) -> None:
        if client is not self.client:
            return
        self._link_lost = True
        logger.warning("link to %s dropped", self.address)
        self._engine.fail(DeviceDisconnected("BLE-MIDI link dropped"))
        self._hub.close()

    def handle_notification(self, _sender: Any, data: bytearray | bytes) -> None:
        packet = bytes(data)
        if self.record:
            self._record(self.raw_packets, packet)
        try:
            events = self.decoder.feed(packet)
        except Exception as exc:
            self.decoder.reset()
            logger.warning("dropped malformed BLE-MIDI packet %s: %s", packet.hex(" "), exc)
            return
        for event in events:
            self._dispatch(event)

    def _dispatch(self, event: MidiEvent) -> None:
        if self.record:
            self._record(self.recorded_events, event)
        answered = event.message[0] == 0xF0 and self._engine.deliver(event.message)
        if not answered:
            self._hub.publish(event)
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception as exc:
                logger.warning("event callback failed: %s", exc, exc_info=True)

    def _record(self, destination: list[Any], item: Any) -> None:
        if len(destination) < self.max_recorded:
            destination.append(item)
        elif not self._recording_full_logged:
            self._recording_full_logged = True
            logger.warning("capture is full at %d entries; later ones are not recorded", self.max_recorded)

    async def send(self, message: bytes, timestamp: int = 0) -> None:
        if not self.connected:
            raise DeviceDisconnected("BLE-MIDI session is not connected")
        packet = encode_packet(message, timestamp=timestamp)
        try:
            async with asyncio.timeout(self.write_timeout):
                await self.client.write_gatt_char(BLE_MIDI_CHARACTERISTIC_UUID, packet, response=False)
        except TimeoutError as exc:
            raise DeviceDisconnected("BLE-MIDI write timed out") from exc
        except Exception as exc:
            raise DeviceDisconnected(f"BLE-MIDI write failed: {exc}") from exc

    async def query(
        self,
        command: int,
        payload: bytes = b"",
        *,
        sequence: int | None = None,
        timeout: float = 5.0,
    ) -> NanocoreResponse:
        return await self._engine.query(command, payload, sequence=sequence, timeout=timeout)

    async def close(self) -> None:
        async with self._lifecycle:
            try:
                await self._release_client()
            finally:
                self.decoder.reset()
                self._link_lost = False
                self._engine.fail(DeviceDisconnected("BLE-MIDI session closed"))
                self._hub.close()

    async def _release_client(self) -> None:
        client, self.client = self.client, None
        started, self._notifications_started = self._notifications_started, False
        if client is None:
            return
        if client.is_connected:
            if started:
                await self._quietly("stop_notify", client.stop_notify(BLE_MIDI_CHARACTERISTIC_UUID))
            await self._quietly("disconnect", client.disconnect())
            logger.info("disconnected from %s", self.address)

    async def _quietly(self, what: str, call: Any) -> None:
        try:
            async with asyncio.timeout(self.close_timeout):
                await call
        except Exception as exc:
            logger.debug("BLE-MIDI %s failed: %s", what, exc)


_GATTTOOL_NOTIFICATION = re.compile(
    r"(?:Notification|Indication)\s+handle\s*=\s*0x[0-9a-f]+\s+value:\s*(?P<value>[0-9a-f ]+)",
    re.IGNORECASE,
)


def parse_gatttool_notifications(output: str) -> list[bytes]:
    """Extract notification payloads from gatttool's human-readable output."""

    packets: list[bytes] = []
    for match in _GATTTOOL_NOTIFICATION.finditer(output):
        try:
            packets.append(bytes.fromhex(match.group("value")))
        except ValueError:
            continue
    return packets


def gatttool_capture_command(
    address: str,
    *,
    adapter: str = DEFAULT_ADAPTER,
    executable: str = "gatttool",
) -> list[str]:
    address = validate_bluetooth_address(address)
    adapter = validate_adapter_name(adapter)
    return [
        "stdbuf",
        "-o0",
        executable,
        "-i",
        adapter,
        "-b",
        address,
        "--char-write-req",
        "-a",
        GATTTOOL_CCC_HANDLE,
        "-n",
        "0100",
        "--listen",
    ]


def _run_gatttool_capture(
    address: str,
    seconds: float,
    *,
    adapter: str = DEFAULT_ADAPTER,
    executable: str = "gatttool",
    grace: float = 3.0,
) -> list[bytes]:
    if shutil.which(executable) is None:
        raise RuntimeError(f"{executable} is not installed")
    command = gatttool_capture_command(address, adapter=adapter, executable=executable)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        try:
            output, _ = process.communicate(timeout=max(seconds, 0.0) + 2.0)
        except subprocess.TimeoutExpired as exc:
            output = _stop_process(process, grace)
            if not output:
                raise RuntimeError("gatttool capture timed out without output") from exc
    finally:
        _reap(process, grace)
    return parse_gatttool_notifications(output)


def _stop_process(process: "subprocess.Popen[str]", grace: float) -> str:
    """Terminate, then kill after ``grace`` seconds; return whatever it printed."""

    process.terminate()
    try:
        output, _ = process.communicate(timeout=grace)
        return output or ""
    except subprocess.TimeoutExpired:
        logger.warning("gatttool ignored terminate; killing it")
    process.kill()
    try:
        output, _ = process.communicate(timeout=grace)
        return output or ""
    except subprocess.TimeoutExpired:
        logger.warning("gatttool output could not be collected after kill")
        return ""


def _reap(process: "subprocess.Popen[str]", grace: float) -> None:
    if process.poll() is not None:
        return
    process.kill()
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        logger.warning("gatttool did not exit after kill")


def _capture_document(
    address: str,
    packets: list[bytes],
    *,
    adapter: str,
    handshake: bool,
    backend: str,
) -> dict[str, Any]:
    decoder = BleMidiDecoder()
    events: list[MidiEvent] = []
    for packet in packets:
        try:
            events.extend(decoder.feed(packet))
        except ValueError:
            # Preserve raw evidence even if a future firmware adds a packet form
            # that this decoder does not know yet.
            continue
    return {
        "schema_version": 1,
        "captured_at": datetime.now(UTC).isoformat(),
        "address": address,
        "adapter": adapter,
        "backend": backend,
        "handshake": handshake,
        "raw_packets": [packet.hex(" ") for packet in packets],
        "events": [
            {"timestamp": event.timestamp, "message": event.message.hex(" ")}
            for event in events
        ],
    }


async def capture_ble(
    address: str,
    *,
    seconds: float = 10.0,
    output_path: str | Path | None = None,
    handshake: bool = False,
    backend: str = "bleak",
    adapter: str = DEFAULT_ADAPTER,
    session_factory: Callable[..., BleMidiSession] = BleMidiSession,
) -> dict[str, Any]:
    """Capture BLE-MIDI notifications and optionally save them as JSON."""

    address = validate_bluetooth_address(address)
    adapter = validate_adapter_name(adapter)
    if seconds < 0:
        raise ValueError("seconds must be non-negative")
    if backend not in {"bleak", "gatttool", "auto"}:
        raise ValueError("backend must be bleak, gatttool, or auto")

    if backend == "gatttool":
        packets = await asyncio.to_thread(_run_gatttool_capture, address, seconds, adapter=adapter)
        document = _capture_document(address, packets, adapter=adapter, handshake=False, backend=backend)
        if output_path is not None:
            destination = Path(output_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        return document

    session = session_factory(address, adapter=adapter, record=True)
    try:
        try:
            await session.connect(handshake=handshake)
        except RuntimeError:
            if backend != "auto":
                raise
            packets = await asyncio.to_thread(_run_gatttool_capture, address, seconds, adapter=adapter)
            document = _capture_document(
                address,
                packets,
                adapter=adapter,
                handshake=False,
                backend="gatttool-fallback",
            )
            if output_path is not None:
                destination = Path(output_path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
            return document
        await asyncio.sleep(seconds)
    finally:
        await session.close()

    document = _capture_document(
        address,
        session.raw_packets,
        adapter=adapter,
        handshake=handshake,
        backend=backend,
    )
    if output_path is not None:
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return document
