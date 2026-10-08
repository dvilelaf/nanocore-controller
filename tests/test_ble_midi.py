import asyncio
import logging
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import pytest

import nanocore_controller.ble_midi as ble_midi
from nanocore_controller.ble_midi import (
    BLE_MIDI_CHARACTERISTIC_UUID,
    BLE_MIDI_SERVICE_UUID,
    BleMidiDecoder,
    BleMidiSession,
    MidiEvent,
    capture_ble,
    decode_packet,
    encode_packet,
    parse_gatttool_notifications,
)
from nanocore_controller.errors import (
    DeviceDisconnected,
    DeviceNotFound,
    DeviceStatusError,
    DeviceTimeout,
    ValidationError,
)
from nanocore_controller.nanocore_protocol import pack_7bit
from nanocore_controller.session import SysexSession


class BleMidiCodecTests(unittest.TestCase):
    def test_encodes_one_control_change_with_timestamp(self):
        packet = encode_packet(bytes.fromhex("b0 50 7f"), timestamp=0)
        self.assertEqual(packet, bytes.fromhex("80 80 b0 50 7f"))

    def test_decodes_one_control_change(self):
        events = decode_packet(bytes.fromhex("80 80 b0 50 7f"))
        self.assertEqual(events, [MidiEvent(timestamp=0, message=bytes.fromhex("b0 50 7f"))])

    def test_decodes_two_messages_with_distinct_timestamps(self):
        packet = bytes.fromhex("80 80 b0 50 7f 82 c0 0c")
        self.assertEqual(
            decode_packet(packet),
            [
                MidiEvent(timestamp=0, message=bytes.fromhex("b0 50 7f")),
                MidiEvent(timestamp=2, message=bytes.fromhex("c0 0c")),
            ],
        )

    def test_decodes_running_status_within_packet(self):
        packet = bytes.fromhex("80 80 b0 50 7f 82 51 00")
        self.assertEqual(
            decode_packet(packet),
            [
                MidiEvent(timestamp=0, message=bytes.fromhex("b0 50 7f")),
                MidiEvent(timestamp=2, message=bytes.fromhex("b0 51 00")),
            ],
        )

    def test_preserves_sysex_message(self):
        message = bytes.fromhex("f0 7d 01 02 f7")
        self.assertEqual(decode_packet(encode_packet(message)), [MidiEvent(timestamp=0, message=message)])

    def test_decoder_reassembles_sysex_continuation_without_timestamp(self):
        decoder = BleMidiDecoder()
        self.assertEqual(decoder.feed(bytes.fromhex("80 80 f0 7d 01")), [])
        self.assertEqual(
            decoder.feed(bytes.fromhex("80 02 f7")),
            [MidiEvent(timestamp=0, message=bytes.fromhex("f0 7d 01 02 f7"))],
        )

    def test_decoder_accepts_timestamp_immediately_before_sysex_end(self):
        self.assertEqual(
            decode_packet(bytes.fromhex("80 80 f0 7d 01 80 f7")),
            [MidiEvent(timestamp=0, message=bytes.fromhex("f0 7d 01 f7"))],
        )

    def test_decoder_does_not_keep_running_status_across_packets(self):
        decoder = BleMidiDecoder()
        self.assertEqual(decoder.feed(bytes.fromhex("80 80 b0 50 7f")), [MidiEvent(0, bytes.fromhex("b0 50 7f"))])
        with self.assertRaisesRegex(ValueError, "running status"):
            decoder.feed(bytes.fromhex("80 82 51 00"))

    def test_rejects_malformed_packet(self):
        with self.assertRaises(ValueError):
            decode_packet(bytes.fromhex("00 80 b0 50 7f"))

    def test_session_captures_raw_packets_and_decoded_events(self):
        session = BleMidiSession("AA:BB:CC:DD:EE:FF", record=True)
        session.handle_notification(None, bytes.fromhex("80 80 b0 50 7f"))
        self.assertEqual(session.raw_packets, [bytes.fromhex("80 80 b0 50 7f")])
        self.assertEqual(session.recorded_events, [MidiEvent(timestamp=0, message=bytes.fromhex("b0 50 7f"))])

    def test_exposes_standard_ble_midi_identifiers(self):
        self.assertEqual(len(BLE_MIDI_SERVICE_UUID), 36)
        self.assertEqual(len(BLE_MIDI_CHARACTERISTIC_UUID), 36)

    def test_capture_serializes_raw_packets_and_events(self):
        class FakeSession:
            def __init__(self, address, *, adapter, record=False):
                self.address = address
                self.adapter = adapter
                self.raw_packets = []
                self.events = []

            async def connect(self, *, handshake=False):
                self.handle_notification(None, bytes.fromhex("80 80 b0 50 7f"))

            def handle_notification(self, sender, data):
                session = BleMidiSession(self.address, record=True)
                session.handle_notification(sender, data)
                self.raw_packets.extend(session.raw_packets)
                self.events.extend(session.recorded_events)

            async def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.json"
            document = asyncio.run(
                capture_ble(
                    "AA:BB:CC:DD:EE:FF",
                    seconds=0,
                    output_path=path,
                    adapter="hci1",
                    session_factory=FakeSession,
                )
            )
            self.assertEqual(document["adapter"], "hci1")
            self.assertEqual(document["raw_packets"], ["80 80 b0 50 7f"])
            self.assertEqual(document["events"][0]["message"], "b0 50 7f")
            self.assertTrue(path.exists())

    def test_parses_gatttool_notification_lines(self):
        output = """Characteristic value was written successfully
Notification handle = 0x0013 value: 80 80 b0 50 7f
Indication handle = 0x0013 value: 80 82 c0 09
"""
        self.assertEqual(
            parse_gatttool_notifications(output),
            [bytes.fromhex("80 80 b0 50 7f"), bytes.fromhex("80 82 c0 09")],
        )

    def test_close_stops_notifications_then_disconnects(self):
        calls = []

        class FakeClient:
            is_connected = True

            async def stop_notify(self, _characteristic):
                calls.append("stop_notify")

            async def disconnect(self):
                calls.append("disconnect")
                self.is_connected = False

        session = BleMidiSession("test-address")
        client = session.client = FakeClient()
        session._notifications_started = True
        asyncio.run(session.close())
        self.assertEqual(calls, ["stop_notify", "disconnect"])
        self.assertFalse(client.is_connected)

    def test_connect_discovers_on_selected_adapter_without_empty_handshake_write(self):
        calls = []
        device = object()

        class FakeScanner:
            @staticmethod
            async def find_device_by_address(address, *, timeout, adapter):
                calls.append(("scan", address, adapter))
                return device

        class FakeClient:
            def __init__(self, selected_device, *, adapter, timeout, disconnected_callback):
                self.is_connected = False
                calls.append(("client", selected_device, adapter))

            async def connect(self):
                self.is_connected = True
                calls.append(("connect",))

            async def start_notify(self, characteristic, callback):
                calls.append(("notify", characteristic))

            async def write_gatt_char(self, characteristic, payload, *, response):
                calls.append(("write", characteristic, payload, response))

        fake_bleak = types.SimpleNamespace(BleakClient=FakeClient, BleakScanner=FakeScanner)
        with mock.patch.dict(sys.modules, {"bleak": fake_bleak}):
            session = BleMidiSession("AA:BB:CC:DD:EE:FF", adapter="hci1")
            asyncio.run(session.connect(handshake=True))

        self.assertEqual(calls[0], ("scan", "AA:BB:CC:DD:EE:FF", "hci1"))
        self.assertEqual(calls[1], ("client", device, "hci1"))
        self.assertEqual(calls[2], ("connect",))
        self.assertEqual(calls[3], ("notify", BLE_MIDI_CHARACTERISTIC_UUID))
        self.assertFalse(any(call[0] == "write" for call in calls))

    def test_gatttool_capture_command_uses_selected_adapter(self):
        command = ble_midi.gatttool_capture_command("AA:BB:CC:DD:EE:FF", adapter="hci1")
        self.assertEqual(command[command.index("-i") + 1], "hci1")

    def test_query_sends_request_and_waits_for_matching_response(self):
        session = BleMidiSession("test-address")

        class FakeClient:
            is_connected = True

            async def write_gatt_char(self, characteristic, packet, *, response):
                self.packet = packet
                raw_response = bytes.fromhex("02 01 00 63 00 00 03 00 03 08 54")
                sysex = bytes.fromhex("f0 7d 4e 43 71") + pack_7bit(raw_response) + bytes([0xF7])
                session.handle_notification(None, encode_packet(sysex))

        session.client = FakeClient()
        response = asyncio.run(session.query(0x63, sequence=1, timeout=0.1))
        self.assertEqual(response.command, 0x63)
        self.assertEqual(response.payload, bytes.fromhex("03 08 54"))
        self.assertIn(bytes.fromhex("f0 7d 4e 43 70"), session.client.packet)

    def test_automatic_query_sequence_starts_after_configured_seed(self):
        session = BleMidiSession("test-address", initial_sequence=40)

        class FakeClient:
            is_connected = True

            async def write_gatt_char(self, characteristic, packet, *, response):
                raw_response = bytes.fromhex("02 29 00 63 00 00 03 00 03 08 54")
                sysex = bytes.fromhex("f0 7d 4e 43 71") + pack_7bit(raw_response) + bytes([0xF7])
                session.handle_notification(None, encode_packet(sysex))

        session.client = FakeClient()
        response = asyncio.run(session.query(0x63, timeout=0.1))
        self.assertEqual(response.sequence, 41)


# --- session lifecycle, notification robustness and capture limits -------------------------

ADDRESS = "AA:BB:CC:DD:EE:FF"


def device_response(sequence, command, status=0, payload=b""):
    raw = (
        bytes([2])
        + sequence.to_bytes(2, "little")
        + command.to_bytes(2, "little")
        + bytes([status])
        + len(payload).to_bytes(2, "little")
        + payload
    )
    sysex = bytes.fromhex("f0 7d 4e 43 71") + pack_7bit(raw) + bytes([0xF7])
    return encode_packet(sysex)


class FakeBleakClient:
    """Scriptable stand-in for bleak.BleakClient; one instance per construction."""

    instances: list = []
    notify_error: Exception | None = None
    connect_error: Exception | None = None
    stop_notify_error: Exception | None = None
    disconnect_error: Exception | None = None

    def __init__(self, device, *, adapter, timeout, disconnected_callback=None):
        self.is_connected = False
        self.disconnected_callback = disconnected_callback
        self.calls = []
        self.on_write = None
        type(self).instances.append(self)

    async def connect(self):
        if self.connect_error is not None:
            raise self.connect_error
        self.is_connected = True

    async def start_notify(self, characteristic, callback):
        self.calls.append("start_notify")
        self.callback = callback
        if self.notify_error is not None:
            raise self.notify_error

    async def stop_notify(self, characteristic):
        self.calls.append("stop_notify")
        if self.stop_notify_error is not None:
            raise self.stop_notify_error

    async def disconnect(self):
        self.calls.append("disconnect")
        self.is_connected = False
        if self.disconnect_error is not None:
            raise self.disconnect_error

    async def write_gatt_char(self, characteristic, packet, *, response):
        self.calls.append("write")
        if self.on_write is not None:
            self.on_write(packet)


@pytest.fixture
def fake_bleak(monkeypatch):
    class Client(FakeBleakClient):
        instances: list = []

    class Scanner:
        result: object = object()
        error: Exception | None = None
        scans = 0

        @classmethod
        async def find_device_by_address(cls, address, *, timeout, adapter):
            cls.scans += 1
            if cls.error is not None:
                raise cls.error
            return cls.result

    namespace = types.SimpleNamespace(BleakClient=Client, BleakScanner=Scanner)
    monkeypatch.setitem(sys.modules, "bleak", namespace)
    return namespace


def run(coro):
    return asyncio.run(coro)


def test_session_satisfies_sysex_session_protocol():
    assert isinstance(BleMidiSession(ADDRESS), SysexSession)


def test_connect_disconnects_client_when_start_notify_fails(fake_bleak):
    fake_bleak.BleakClient.notify_error = RuntimeError("no notify")
    session = BleMidiSession(ADDRESS)
    with pytest.raises(DeviceDisconnected):
        run(session.connect())
    client = fake_bleak.BleakClient.instances[0]
    assert "disconnect" in client.calls
    assert session.client is None
    assert not session.connected


def test_connect_twice_reuses_the_connection(fake_bleak):
    async def scenario():
        session = BleMidiSession(ADDRESS)
        await session.connect()
        await session.connect()
        assert session.connected
        await session.close()

    run(scenario())
    assert fake_bleak.BleakScanner.scans == 1
    assert len(fake_bleak.BleakClient.instances) == 1


def test_connect_reports_missing_device_and_scan_failure(fake_bleak):
    fake_bleak.BleakScanner.result = None
    with pytest.raises(DeviceNotFound, match="hci0"):
        run(BleMidiSession(ADDRESS).connect())
    fake_bleak.BleakScanner.result = object()
    fake_bleak.BleakScanner.error = OSError("adapter is down")
    with pytest.raises(DeviceNotFound, match="adapter is down"):
        run(BleMidiSession(ADDRESS).connect())


def test_connect_failure_is_a_disconnect_and_leaves_no_client(fake_bleak):
    fake_bleak.BleakClient.connect_error = OSError("refused")
    session = BleMidiSession(ADDRESS)
    with pytest.raises(DeviceDisconnected, match="refused"):
        run(session.connect())
    assert session.client is None


def test_connect_passes_a_disconnected_callback(fake_bleak):
    run(BleMidiSession(ADDRESS).connect())
    assert callable(fake_bleak.BleakClient.instances[0].disconnected_callback)


def test_close_stops_notifications_and_disconnects(fake_bleak):
    async def scenario():
        session = BleMidiSession(ADDRESS)
        await session.connect()
        client = session.client
        await session.close()
        await session.close()
        return session, client

    session, client = run(scenario())
    assert client.calls == ["start_notify", "stop_notify", "disconnect"]
    assert session.client is None
    assert not session.connected


def test_close_never_raises_on_a_dead_link_and_resets_state(fake_bleak):
    fake_bleak.BleakClient.stop_notify_error = OSError("gone")
    fake_bleak.BleakClient.disconnect_error = OSError("gone")

    async def scenario():
        session = BleMidiSession(ADDRESS)
        await session.connect()
        await session.close()
        return session

    session = run(scenario())
    assert session.client is None
    assert not session.connected


def test_close_times_out_on_a_hung_disconnect(fake_bleak):
    async def hang(self):
        await asyncio.sleep(60)

    fake_bleak.BleakClient.disconnect = hang

    async def scenario():
        session = BleMidiSession(ADDRESS, close_timeout=0.05)
        await session.connect()
        await session.close()
        return session

    assert run(scenario()).client is None


def test_dropped_link_fails_the_pending_query(fake_bleak):
    async def scenario():
        session = BleMidiSession(ADDRESS)
        await session.connect()
        client = session.client
        task = asyncio.ensure_future(session.query(0x63, sequence=7, timeout=5.0))
        await asyncio.sleep(0.01)
        client.is_connected = False
        client.disconnected_callback(client)
        with pytest.raises(DeviceDisconnected):
            await task
        assert not session.connected
        with pytest.raises(DeviceDisconnected):
            await session.send(b"\xb0\x50\x7f")

    run(scenario())


def test_reconnect_resets_decoder_and_state(fake_bleak):
    async def scenario():
        session = BleMidiSession(ADDRESS)
        await session.connect()
        session.handle_notification(None, bytes.fromhex("80 80 f0 7d 01"))
        await session.close()
        await session.connect()
        seen = []
        session.on_event = seen.append
        session.handle_notification(None, bytes.fromhex("80 05 f7"))
        session.handle_notification(None, bytes.fromhex("80 80 b0 50 7f"))
        return seen

    assert [event.message for event in run(scenario())] == [bytes.fromhex("b0 50 7f")]


def test_query_over_session_raises_typed_status_error(fake_bleak):
    async def scenario():
        session = BleMidiSession(ADDRESS)
        await session.connect()
        session.client.on_write = lambda packet: session.handle_notification(
            None, device_response(9, 0x76, status=3, payload=b"\x01")
        )
        await session.query(0x76, sequence=9, timeout=0.5)

    with pytest.raises(DeviceStatusError) as caught:
        run(scenario())
    assert (caught.value.command, caught.value.status, caught.value.payload) == (0x76, 3, b"\x01")


def test_query_timeout_flags_writes_as_possibly_applied(fake_bleak):
    async def scenario(command):
        session = BleMidiSession(ADDRESS)
        await session.connect()
        await session.query(command, sequence=1, timeout=0.02)

    with pytest.raises(DeviceTimeout) as read:
        run(scenario(0x63))
    assert read.value.maybe_applied is False
    with pytest.raises(DeviceTimeout) as write:
        run(scenario(0x6D))
    assert write.value.maybe_applied is True


def test_notification_error_resets_decoder_and_is_not_propagated(caplog):
    session = BleMidiSession(ADDRESS)
    seen = []
    session.on_event = seen.append
    session.handle_notification(None, bytes.fromhex("80 80 f0 7d 01"))
    with caplog.at_level(logging.WARNING, logger="nanocore.ble"):
        session.handle_notification(None, bytes.fromhex("80 02 90 03 f7"))
    assert "nanocore.ble" in {record.name for record in caplog.records}
    session.handle_notification(None, bytes.fromhex("80 05 06 f7"))
    session.handle_notification(None, bytes.fromhex("80 80 f0 7d 05 06 f7"))
    assert [event.message for event in seen] == [bytes.fromhex("f0 7d 05 06 f7")]


def test_failing_event_callback_is_contained_and_logged(caplog):
    session = BleMidiSession(ADDRESS)
    stream = session.events()

    def boom(_event):
        raise RuntimeError("callback bug")

    session.on_event = boom
    with caplog.at_level(logging.WARNING, logger="nanocore.ble"):
        session.handle_notification(None, bytes.fromhex("80 80 b0 50 7f"))
    assert any("callback bug" in record.getMessage() for record in caplog.records)
    assert run(anext(stream)).message == bytes.fromhex("b0 50 7f")


def test_realtime_byte_inside_sysex_does_not_abort_the_message():
    decoder = BleMidiDecoder()
    events = decoder.feed(bytes.fromhex("80 80 f0 7d f8 01 02 f7"))
    assert [event.message for event in events if event.message[0] == 0xF0] == [
        bytes.fromhex("f0 7d 01 02 f7")
    ]


def test_realtime_byte_between_sysex_chunks_is_tolerated():
    decoder = BleMidiDecoder()
    assert decoder.feed(bytes.fromhex("80 80 f0 7d 01")) == []
    events = decoder.feed(bytes.fromhex("80 81 f8 02 f7"))
    assert bytes.fromhex("f0 7d 01 02 f7") in [event.message for event in events]


def test_decoder_resets_itself_after_a_malformed_packet():
    decoder = BleMidiDecoder()
    decoder.feed(bytes.fromhex("80 80 f0 7d 01"))
    with pytest.raises(ValueError):
        decoder.feed(bytes.fromhex("80 02 90 03 f7"))
    with pytest.raises(ValueError, match="timestamp"):
        decoder.feed(bytes.fromhex("80 05 06 f7"))
    assert decoder.feed(bytes.fromhex("80 80 f0 7d 05 f7"))[0].message == bytes.fromhex(
        "f0 7d 05 f7"
    )


def test_capture_lists_are_opt_in_and_bounded():
    quiet = BleMidiSession(ADDRESS)
    quiet.handle_notification(None, bytes.fromhex("80 80 b0 50 7f"))
    assert quiet.raw_packets == []
    assert quiet.recorded_events == []

    recording = BleMidiSession(ADDRESS, record=True, max_recorded=2)
    for value in range(5):
        recording.handle_notification(None, bytes([0x80, 0x80, 0xB0, 0x50, value]))
    assert len(recording.raw_packets) == 2
    assert len(recording.recorded_events) == 2
    assert recording.raw_packets[0] == bytes([0x80, 0x80, 0xB0, 0x50, 0])


def test_events_streams_notifications_and_ends_on_close(fake_bleak):
    async def scenario():
        session = BleMidiSession(ADDRESS)
        await session.connect()
        stream = session.events()
        session.handle_notification(None, bytes.fromhex("80 80 b0 50 7f"))
        first = await anext(stream)
        await session.close()
        remaining = [event async for event in stream]
        return first, remaining

    first, remaining = run(scenario())
    assert first.message == bytes.fromhex("b0 50 7f")
    assert remaining == []


def test_events_has_a_bounded_queue_per_consumer(caplog):
    session = BleMidiSession(ADDRESS, max_queued_events=3)
    slow = session.events()
    with caplog.at_level(logging.WARNING, logger="nanocore.ble"):
        for value in range(10):
            session.handle_notification(None, bytes([0x80, 0x80, 0xB0, 0x50, value]))
    assert [run(anext(slow)).message[2] for _ in range(3)] == [7, 8, 9]
    assert any("dropp" in record.getMessage() for record in caplog.records)


def test_answers_to_a_query_are_not_device_events(fake_bleak):
    async def scenario():
        session = BleMidiSession(ADDRESS)
        await session.connect()
        stream = session.events()
        session.client.on_write = lambda packet: session.handle_notification(
            None, device_response(4, 0x63, payload=b"\x01")
        )
        await session.query(0x63, sequence=4, timeout=0.5)
        session.handle_notification(None, bytes.fromhex("80 80 b0 50 7f"))
        return await anext(stream)

    assert run(scenario()).message == bytes.fromhex("b0 50 7f")


def test_capture_ble_turns_recording_on():
    seen = {}

    class Recorder:
        def __init__(self, address, *, adapter, record=False):
            seen["record"] = record
            self.raw_packets = []

        async def connect(self, *, handshake=False):
            pass

        async def close(self):
            pass

    asyncio.run(capture_ble(ADDRESS, seconds=0, adapter="hci0", session_factory=Recorder))
    assert seen["record"] is True


class FakeProcess:
    def __init__(self, hangs):
        self.hangs = hangs
        self.calls = []
        self.returncode = None

    def communicate(self, timeout=None):
        self.calls.append("communicate")
        if self.hangs > 0:
            self.hangs -= 1
            raise subprocess.TimeoutExpired("gatttool", timeout)
        self.returncode = 0
        return "Notification handle = 0x0013 value: 80 80 b0 50 7f\n", None

    def terminate(self):
        self.calls.append("terminate")

    def kill(self):
        self.calls.append("kill")

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.calls.append("wait")
        self.returncode = -9
        return self.returncode


def _patched_gatttool(monkeypatch, process):
    monkeypatch.setattr(ble_midi.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(ble_midi.subprocess, "Popen", lambda *args, **kwargs: process)


def test_gatttool_capture_terminates_then_kills_a_stubborn_process(monkeypatch):
    process = FakeProcess(hangs=2)
    _patched_gatttool(monkeypatch, process)
    packets = ble_midi._run_gatttool_capture(ADDRESS, 0.0, grace=0.01)
    assert process.calls.index("terminate") < process.calls.index("kill")
    assert packets == [bytes.fromhex("80 80 b0 50 7f")]


def test_gatttool_capture_never_leaves_the_process_running(monkeypatch):
    process = FakeProcess(hangs=99)
    _patched_gatttool(monkeypatch, process)
    with pytest.raises(RuntimeError, match="without output"):
        ble_midi._run_gatttool_capture(ADDRESS, 0.0, grace=0.01)
    assert "kill" in process.calls
    assert process.poll() is not None


def test_gatttool_command_rejects_option_like_address_and_adapter():
    with pytest.raises(ValidationError):
        ble_midi.gatttool_capture_command("-t", adapter="hci0")
    with pytest.raises(ValidationError):
        ble_midi.gatttool_capture_command(ADDRESS, adapter="--help")
    command = ble_midi.gatttool_capture_command(ADDRESS.lower(), adapter="hci1")
    assert ADDRESS in command


def test_capture_ble_validates_before_touching_anything(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("must not run")

    monkeypatch.setattr(ble_midi.subprocess, "Popen", forbidden)
    with pytest.raises(ValidationError):
        asyncio.run(capture_ble("-i", seconds=0, backend="gatttool"))
    with pytest.raises(ValidationError):
        asyncio.run(capture_ble(ADDRESS, seconds=0, backend="gatttool", adapter="-x"))


def test_a_sysex_message_that_never_ends_is_dropped_and_the_decoder_recovers():
    from nanocore_controller.ble_midi import MAX_SYSEX_BYTES

    decoder = BleMidiDecoder()
    decoder.feed(bytes.fromhex("80 80 f0"))
    chunk = bytes([0x80 | 1]) + bytes([0x01] * 18)
    with pytest.raises(ValueError, match="too long"):
        for _ in range(MAX_SYSEX_BYTES // 18 + 2):
            decoder.feed(chunk)

    assert [e.message for e in decoder.feed(bytes.fromhex("80 80 b0 50 7f"))] == [
        bytes.fromhex("b0 50 7f")
    ]
