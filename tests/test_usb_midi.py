import asyncio
import logging
import os
import socket
from unittest import mock

import pytest

from nanocore_controller.ble_midi import MidiEvent
from nanocore_controller.errors import (
    DeviceDisconnected,
    DeviceNotFound,
    DeviceStatusError,
    DeviceTimeout,
    ValidationError,
)
from nanocore_controller.nanocore_protocol import encode_request, pack_7bit
from nanocore_controller.session import SysexSession
from nanocore_controller.usb_midi import AmidiSession, MidiStreamDecoder, rawmidi_device_path


def feed_all(decoder, *chunks):
    messages = []
    for chunk in chunks:
        messages.extend(decoder.feed(bytes.fromhex(chunk)))
    return [message.hex(" ") for message in messages]


# --- MidiStreamDecoder --------------------------------------------------------------------


def test_decodes_channel_and_system_messages():
    assert feed_all(MidiStreamDecoder(), "b0 50 7f c0 09 e0 01 02") == [
        "b0 50 7f",
        "c0 09",
        "e0 01 02",
    ]


def test_running_status_repeats_the_last_channel_status():
    assert feed_all(MidiStreamDecoder(), "b0 50 7f 51 00 52 01") == [
        "b0 50 7f",
        "b0 51 00",
        "b0 52 01",
    ]


def test_message_split_across_reads_is_completed():
    assert feed_all(MidiStreamDecoder(), "b0", "50", "7f 51", "00") == ["b0 50 7f", "b0 51 00"]


def test_sysex_split_across_reads():
    assert feed_all(MidiStreamDecoder(), "f0 7d 4e", "43 71 01", "02 f7 b0 50 7f") == [
        "f0 7d 4e 43 71 01 02 f7",
        "b0 50 7f",
    ]


def test_realtime_bytes_are_emitted_and_do_not_break_sysex_or_data():
    assert feed_all(MidiStreamDecoder(), "f0 7d f8 01 fe 02 f7") == [
        "f8",
        "fe",
        "f0 7d 01 02 f7",
    ]
    assert feed_all(MidiStreamDecoder(), "b0 50 f8 7f") == ["f8", "b0 50 7f"]


def test_realtime_does_not_cancel_running_status():
    assert feed_all(MidiStreamDecoder(), "b0 50 7f f8 51 00") == ["b0 50 7f", "f8", "b0 51 00"]


def test_stray_data_bytes_are_dropped():
    assert feed_all(MidiStreamDecoder(), "10 20 7f") == []
    assert feed_all(MidiStreamDecoder(), "10 b0 50 7f") == ["b0 50 7f"]


def test_system_common_cancels_running_status():
    assert feed_all(MidiStreamDecoder(), "b0 50 7f f3 04 51 00") == ["b0 50 7f", "f3 04"]


def test_sysex_interrupted_by_a_status_byte_is_dropped_and_the_status_is_kept():
    assert feed_all(MidiStreamDecoder(), "f0 7d 01 b0 50 7f") == ["b0 50 7f"]


def test_new_status_replaces_an_incomplete_message():
    assert feed_all(MidiStreamDecoder(), "b0 50 c0 09") == ["c0 09"]


def test_stray_sysex_end_is_ignored_and_oversized_sysex_is_dropped():
    decoder = MidiStreamDecoder(max_sysex=8)
    assert feed_all(decoder, "f7 b0 50 7f") == ["b0 50 7f"]
    assert feed_all(decoder, "f0 " + "01 " * 20 + "f7 c0 05") == ["c0 05"]


def test_reset_discards_a_partial_sysex():
    decoder = MidiStreamDecoder()
    feed_all(decoder, "f0 7d 01")
    decoder.reset()
    assert feed_all(decoder, "02 f7") == []


# --- port to device node -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("port", "path"),
    [
        ("hw:6,0,0", "/dev/snd/midiC6D0"),
        ("hw:6,0", "/dev/snd/midiC6D0"),
        ("hw:12,3,1", "/dev/snd/midiC12D3"),
    ],
)
def test_port_maps_to_the_rawmidi_node(port, path):
    assert rawmidi_device_path(port) == path


@pytest.mark.parametrize("port", ["", "hw:6", "plughw:6,0", "hw:a,0", "/dev/snd/midiC6D0", "hw:6,0;x"])
def test_malformed_port_is_a_validation_error(port):
    with pytest.raises(ValidationError):
        rawmidi_device_path(port)


# --- session over a socketpair ------------------------------------------------------------


def device_response(sequence, command, status=0, payload=b""):
    raw = (
        bytes([2])
        + sequence.to_bytes(2, "little")
        + command.to_bytes(2, "little")
        + bytes([status])
        + len(payload).to_bytes(2, "little")
        + payload
    )
    return bytes.fromhex("f0 7d 4e 43 71") + pack_7bit(raw) + bytes([0xF7])


@pytest.fixture
def link():
    """A session wired to one end of a socketpair; ``peer`` plays the pedal."""

    near, peer = socket.socketpair()
    near.setblocking(False)
    peer.setblocking(False)
    opened = []

    def open_fn(path, flags):
        opened.append((path, flags))
        return near.detach()

    session = AmidiSession("hw:6,0,0", open_fn=open_fn, initial_sequence=0)
    yield session, peer, opened
    peer.close()


def test_session_satisfies_sysex_session_protocol():
    assert isinstance(AmidiSession("hw:6,0,0"), SysexSession)


def test_connect_opens_the_node_non_blocking_and_is_idempotent(link):
    session, _peer, opened = link

    async def scenario():
        await session.connect()
        await session.connect()
        assert session.connected
        await session.close()
        await session.close()
        assert not session.connected

    asyncio.run(scenario())
    assert opened == [("/dev/snd/midiC6D0", os.O_RDWR | os.O_NONBLOCK)]


def test_close_releases_the_descriptor(link):
    session, _peer, _opened = link

    async def scenario():
        await session.connect()
        fd = session._fd
        await session.close()
        return fd

    fd = asyncio.run(scenario())
    with pytest.raises(OSError):
        os.fstat(fd)


@pytest.mark.parametrize("error", [PermissionError(13, "Permission denied"), FileNotFoundError(2, "No such file")])
def test_open_failure_is_device_not_found_with_the_path(error):
    def open_fn(path, flags):
        raise error

    session = AmidiSession("hw:6,0,0", open_fn=open_fn)
    with pytest.raises(DeviceNotFound, match="/dev/snd/midiC6D0") as caught:
        asyncio.run(session.connect())
    assert error.strerror in str(caught.value)
    assert not session.connected


def test_query_round_trip_with_response_split_and_realtime(link):
    session, peer, _opened = link
    loop_requests = []

    async def scenario():
        await session.connect()
        loop = asyncio.get_running_loop()

        async def pedal():
            request = await loop.sock_recv(peer, 4096)
            loop_requests.append(request)
            answer = device_response(1, 0x63, payload=b"\x03\x08")
            await loop.sock_sendall(peer, answer[:7])
            await loop.sock_sendall(peer, b"\xf8")
            await asyncio.sleep(0.01)
            await loop.sock_sendall(peer, answer[7:])

        task = asyncio.ensure_future(pedal())
        response = await session.query(0x63, timeout=2.0)
        await task
        await session.close()
        return response

    response = asyncio.run(scenario())
    assert (response.sequence, response.command, response.payload) == (1, 0x63, b"\x03\x08")
    assert loop_requests == [encode_request(0x63, sequence=1)]


def test_query_typed_errors(link):
    session, peer, _opened = link

    async def scenario():
        await session.connect()
        loop = asyncio.get_running_loop()

        async def pedal():
            await loop.sock_recv(peer, 4096)
            await loop.sock_sendall(peer, device_response(1, 0x76, status=3))

        task = asyncio.ensure_future(pedal())
        with pytest.raises(DeviceStatusError):
            await session.query(0x76, timeout=2.0)
        await task
        with pytest.raises(DeviceTimeout) as caught:
            await session.query(0x63, timeout=0.02)
        assert caught.value.maybe_applied is False
        await session.close()

    asyncio.run(scenario())


def test_device_originated_events_are_streamed_but_answers_are_not(link):
    session, peer, _opened = link

    async def scenario():
        await session.connect()
        loop = asyncio.get_running_loop()
        stream = session.events()

        async def pedal():
            await loop.sock_recv(peer, 4096)
            await loop.sock_sendall(peer, device_response(1, 0x63) + b"\xb0\x50\x7f")

        task = asyncio.ensure_future(pedal())
        await session.query(0x63, timeout=2.0)
        await task
        first = await asyncio.wait_for(anext(stream), 1.0)
        await session.close()
        return first, [event async for event in stream]

    first, rest = asyncio.run(scenario())
    assert first == MidiEvent(timestamp=0, message=b"\xb0\x50\x7f")
    assert rest == []


def test_on_event_callback_sees_events_and_its_errors_are_contained(caplog):
    near, peer = socket.socketpair()
    near.setblocking(False)
    seen = []

    def on_event(event):
        seen.append(event.message)
        raise RuntimeError("callback bug")

    session = AmidiSession("hw:6,0,0", on_event, open_fn=lambda path, flags: near.detach())

    async def scenario():
        await session.connect()
        loop = asyncio.get_running_loop()
        await loop.sock_sendall(peer, b"\xb0\x50\x7f\xc0\x09")
        for _ in range(500):  # poll instead of sleeping a fixed time: disks and CPUs vary
            if len(seen) == 2:
                break
            await asyncio.sleep(0.01)
        await session.close()

    with caplog.at_level(logging.WARNING, logger="nanocore.usb"):
        asyncio.run(scenario())
    peer.close()
    assert seen == [b"\xb0\x50\x7f", b"\xc0\x09"]
    assert any("callback bug" in record.getMessage() for record in caplog.records)


def test_peer_hangup_fails_the_pending_query_and_ends_events(link):
    session, peer, _opened = link

    async def scenario():
        await session.connect()
        stream = session.events()
        task = asyncio.ensure_future(session.query(0x63, timeout=5.0))
        await asyncio.sleep(0.02)
        peer.close()
        with pytest.raises(DeviceDisconnected):
            await task
        assert not session.connected
        assert [event async for event in stream] == []
        with pytest.raises(DeviceDisconnected):
            await session.send(b"\xb0\x50\x7f")
        await session.close()

    asyncio.run(scenario())


def test_send_handles_partial_writes_and_would_block(link):
    session, peer, _opened = link
    message = b"\xf0" + bytes(range(1, 128)) * 1500 + b"\xf7"

    async def scenario():
        await session.connect()
        loop = asyncio.get_running_loop()
        received = bytearray()

        async def pedal():
            await asyncio.sleep(0.05)
            while len(received) < len(message):
                received.extend(await loop.sock_recv(peer, 1024))

        task = asyncio.ensure_future(pedal())
        await session.send(message)
        await asyncio.wait_for(task, 5.0)
        await session.close()
        return bytes(received)

    assert asyncio.run(scenario()) == message


def test_send_times_out_when_the_device_never_drains(link):
    session, _peer, _opened = link
    session.write_timeout = 0.1

    async def scenario():
        await session.connect()
        with pytest.raises(DeviceDisconnected, match="timed out"):
            await session.send(b"\xf0" + b"\x01" * 2_000_000 + b"\xf7")
        await session.close()

    asyncio.run(scenario())


def test_send_rejects_empty_and_unconnected(link):
    session, _peer, _opened = link

    async def scenario():
        with pytest.raises(DeviceDisconnected):
            await session.send(b"\xb0\x50\x7f")
        await session.connect()
        with pytest.raises(ValidationError):
            await session.send(b"")
        await session.close()

    asyncio.run(scenario())


def test_reconnect_starts_with_a_clean_decoder_and_open_streams():
    pairs = []

    def open_fn(path, flags):
        near, peer = socket.socketpair()
        near.setblocking(False)
        pairs.append(peer)
        return near.detach()

    session = AmidiSession("hw:6,0,0", open_fn=open_fn)

    async def scenario():
        loop = asyncio.get_running_loop()
        await session.connect()
        await loop.sock_sendall(pairs[0], b"\xf0\x7d\x01")
        await asyncio.sleep(0.02)
        await session.close()
        await session.connect()
        stream = session.events()
        await loop.sock_sendall(pairs[1], b"\x02\xf7\xb0\x50\x7f")
        event = await asyncio.wait_for(anext(stream), 1.0)
        await session.close()
        return event

    event = asyncio.run(scenario())
    for peer in pairs:
        peer.close()
    assert event.message == b"\xb0\x50\x7f"


def test_a_write_that_timed_out_ends_the_session_so_the_next_use_starts_clean(link):
    session, _peer, _opened = link
    session.write_timeout = 0.1

    async def scenario():
        await session.connect()
        with pytest.raises(DeviceDisconnected, match="timed out"):
            await session.send(b"\xf0" + b"\x01" * 2_000_000 + b"\xf7")
        assert not session.connected
        with pytest.raises(DeviceDisconnected):
            await session.send(b"\xb0\x50\x7f")  # nothing half-written is followed by more bytes

    asyncio.run(scenario())


def test_the_device_is_closed_when_the_event_loop_refuses_to_watch_it():
    near, peer = socket.socketpair()
    near.setblocking(False)
    fds = []

    def open_fn(path, flags):
        fds.append(near.detach())
        return fds[0]

    session = AmidiSession("hw:6,0,0", open_fn=open_fn)

    async def scenario():
        loop = asyncio.get_running_loop()
        with mock.patch.object(loop, "add_reader", side_effect=RuntimeError("no selector")):
            with pytest.raises(RuntimeError, match="no selector"):
                await session.connect()
        assert not session.connected

    asyncio.run(scenario())
    peer.close()
    with pytest.raises(OSError):
        os.fstat(fds[0])  # the descriptor was closed, not leaked
