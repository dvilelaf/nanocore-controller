import asyncio
import logging

import pytest

from nanocore_controller.errors import (
    DeviceDisconnected,
    DeviceStatusError,
    DeviceTimeout,
    ProtocolError,
    ValidationError,
)
from nanocore_controller.nanocore_protocol import pack_7bit
from nanocore_controller.sysex_query import EventHub, QueryEngine

LOGGER = logging.getLogger("nanocore.test")


def response(sequence, command, status=0, payload=b""):
    raw = (
        bytes([2])
        + sequence.to_bytes(2, "little")
        + command.to_bytes(2, "little")
        + bytes([status])
        + len(payload).to_bytes(2, "little")
        + payload
    )
    return bytes.fromhex("f0 7d 4e 43 71") + pack_7bit(raw) + bytes([0xF7])


def chunk(sequence, command, status, total, offset, data):
    payload = total.to_bytes(2, "little") + offset.to_bytes(2, "little") + data
    return response(sequence, command, status, payload)


def make_engine(on_send, **kwargs):
    sent = []
    holder = {}

    async def send(message):
        sent.append(message)
        await on_send(holder["engine"], message)

    engine = QueryEngine(send, logger=LOGGER, **kwargs)
    holder["engine"] = engine
    return engine, sent


def answering(*messages):
    async def on_send(engine, request):
        for message in messages:
            assert engine.deliver(message)

    return on_send


def silent():
    async def on_send(engine, request):
        pass

    return on_send


def test_returns_the_matching_response_and_ignores_others():
    async def on_send(engine, request):
        assert not engine.deliver(response(2, 0x63, payload=b"x"))
        assert not engine.deliver(response(1, 0x40, payload=b"y"))
        assert not engine.deliver(b"\xf0\x7d\x01\xf7")
        assert engine.deliver(response(1, 0x63, payload=b"\x03\x08"))

    engine, sent = make_engine(on_send)
    result = asyncio.run(engine.query(0x63, sequence=1))
    assert (result.command, result.payload) == (0x63, b"\x03\x08")
    assert decode_request_command(sent[0]) == 0x63


def decode_request_command(message):
    from nanocore_controller.nanocore_protocol import unpack_7bit

    raw = unpack_7bit(message[5:-1])
    return int.from_bytes(raw[3:5], "little")


def test_automatic_sequence_increments_from_the_seed():
    engine, sent = make_engine(silent(), initial_sequence=40)

    async def scenario():
        async def respond():
            await asyncio.sleep(0)
            engine.deliver(response(41, 0x63))

        task = asyncio.ensure_future(respond())
        result = await engine.query(0x63)
        await task
        return result

    assert asyncio.run(scenario()).sequence == 41


def test_rejects_an_out_of_range_seed():
    with pytest.raises(ValidationError):
        QueryEngine(silent(), logger=LOGGER, initial_sequence=0x10000)


def test_reassembles_streamed_chunks():
    engine, _ = make_engine(
        answering(
            chunk(3, 0x40, 0x10, 4, 0, b"ab"),
            chunk(3, 0x40, 0x11, 4, 2, b"cd"),
        )
    )
    assert asyncio.run(engine.query(0x40, sequence=3)).payload == b"abcd"


def test_malformed_stream_for_the_matching_request_is_a_protocol_error():
    engine, _ = make_engine(answering(chunk(3, 0x40, 0x11, 4, 0, b"ab")))
    with pytest.raises(ProtocolError):
        asyncio.run(engine.query(0x40, sequence=3))

    truncated = response(3, 0x40, 0x10, b"\x01")
    engine, _ = make_engine(answering(truncated))
    with pytest.raises(ProtocolError):
        asyncio.run(engine.query(0x40, sequence=3))


def test_non_zero_status_raises_device_status_error():
    engine, _ = make_engine(answering(response(5, 0x76, status=0x7E, payload=b"\x01")))
    with pytest.raises(DeviceStatusError) as caught:
        asyncio.run(engine.query(0x76, sequence=5))
    assert (caught.value.command, caught.value.status, caught.value.payload) == (0x76, 0x7E, b"\x01")


@pytest.mark.parametrize(("command", "maybe_applied"), [(0x63, False), (0x46, True), (0x6D, True)])
def test_timeout_reports_whether_the_command_may_have_been_applied(command, maybe_applied):
    engine, _ = make_engine(silent())
    with pytest.raises(DeviceTimeout) as caught:
        asyncio.run(engine.query(command, sequence=1, timeout=0.01))
    assert caught.value.maybe_applied is maybe_applied
    assert caught.value.timeout == 0.01


def test_failing_the_engine_aborts_the_waiting_query_only():
    engine, _ = make_engine(silent())

    async def scenario():
        engine.fail(DeviceDisconnected("not pending"))
        task = asyncio.ensure_future(engine.query(0x63, sequence=1, timeout=5))
        await asyncio.sleep(0.01)
        engine.fail(DeviceDisconnected("link dropped"))
        with pytest.raises(DeviceDisconnected, match="link dropped"):
            await task
        engine.reset()
        late = asyncio.ensure_future(engine.query(0x63, sequence=2, timeout=0.01))
        with pytest.raises(DeviceTimeout):
            await late

    asyncio.run(scenario())


def test_queries_are_serialised_and_stale_answers_are_drained():
    order = []

    async def on_send(engine, request):
        order.append("send")
        await asyncio.sleep(0.01)
        order.append("answer")

    engine, _ = make_engine(on_send)

    async def scenario():
        async def respond_after(delay, sequence):
            await asyncio.sleep(delay)
            engine.deliver(response(sequence, 0x63))

        feeders = [
            asyncio.ensure_future(respond_after(0.005, 1)),
            asyncio.ensure_future(respond_after(0.03, 2)),
        ]
        first, second = await asyncio.gather(
            engine.query(0x63, sequence=1, timeout=1), engine.query(0x63, sequence=2, timeout=1)
        )
        await asyncio.gather(*feeders)
        return first, second

    first, second = asyncio.run(scenario())
    assert (first.sequence, second.sequence) == (1, 2)
    assert order == ["send", "answer", "send", "answer"]


def test_stale_responses_from_a_previous_query_are_drained_before_sending():
    engine, _ = make_engine(silent())

    async def scenario():
        engine._pending = (1, 0x63)
        engine.deliver(response(1, 0x63, payload=b"stale"))
        engine._pending = None
        task = asyncio.ensure_future(engine.query(0x63, sequence=1, timeout=0.05))
        await asyncio.sleep(0.01)
        engine.deliver(response(1, 0x63, payload=b"fresh"))
        return await task

    assert asyncio.run(scenario()).payload == b"fresh"


def test_logs_queries_at_debug_and_failures_at_warning(caplog):
    engine, _ = make_engine(answering(response(1, 0x63)))
    with caplog.at_level(logging.DEBUG, logger="nanocore.test"):
        asyncio.run(engine.query(0x63, sequence=1))
    debug = [record for record in caplog.records if record.levelno == logging.DEBUG]
    assert "command=0x63" in debug[0].getMessage() and "sequence=1" in debug[0].getMessage()

    caplog.clear()
    engine, _ = make_engine(silent())
    with caplog.at_level(logging.DEBUG, logger="nanocore.test"), pytest.raises(DeviceTimeout):
        asyncio.run(engine.query(0x63, sequence=1, timeout=0.01))
    assert [record.levelno for record in caplog.records] == [logging.WARNING]


def test_hub_delivers_to_every_consumer_and_ends_on_close():
    async def scenario():
        hub = EventHub(logger=LOGGER)
        first, second = hub.subscribe(), hub.subscribe()
        hub.publish(1)
        hub.publish(2)
        hub.close()
        return [x async for x in first], [x async for x in second], [x async for x in hub.subscribe()]

    assert asyncio.run(scenario()) == ([1, 2], [1, 2], [])


def test_hub_reopen_accepts_new_consumers():
    async def scenario():
        hub = EventHub(logger=LOGGER)
        hub.close()
        hub.reopen()
        stream = hub.subscribe()
        hub.publish(7)
        return await anext(stream)

    assert asyncio.run(scenario()) == 7


def test_hub_drops_the_oldest_event_of_a_slow_consumer_with_a_warning(caplog):
    async def scenario():
        hub = EventHub(logger=LOGGER, max_queued=2)
        slow, fast = hub.subscribe(), hub.subscribe()
        for value in range(5):
            hub.publish(value)
            assert await anext(fast) == value
        hub.close()
        return [x async for x in slow]

    with caplog.at_level(logging.WARNING, logger="nanocore.test"):
        assert asyncio.run(scenario()) == [3, 4]
    assert any("dropped" in record.getMessage() for record in caplog.records)


def test_closed_stream_stops_receiving():
    async def scenario():
        hub = EventHub(logger=LOGGER)
        stream = hub.subscribe()
        await stream.aclose()
        hub.publish(1)
        return stream.queue.qsize()

    assert asyncio.run(scenario()) == 0
