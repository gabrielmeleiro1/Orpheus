"""Stream identity and ordering across broker lifetimes."""

import asyncio
import json

from modules.web.events import EventBroker
from modules.web.routes.events import stream_events


def test_broker_sequences_are_monotonic_and_scoped_to_distinct_instances():
    first = EventBroker()
    second = EventBroker()
    first_queue = first.subscribe()
    second_queue = second.subscribe()
    event = {"event_type": "log", "message": "hello", "event_id": "custom"}
    first.publish(event)
    first.publish(event)
    second.publish(event)
    one, two = first_queue.get_nowait(), first_queue.get_nowait()
    restarted = second_queue.get_nowait()

    assert one["backend_instance_id"] == two["backend_instance_id"]
    assert one["backend_instance_id"] != restarted["backend_instance_id"]
    assert [one["event_sequence"], two["event_sequence"], restarted["event_sequence"]] == [1, 2, 1]
    assert one["event_id"] == "custom"
    assert event == {"event_type": "log", "message": "hello", "event_id": "custom"}


def test_stream_identifies_an_idle_backend_before_any_job_events():
    async def consume():
        broker = EventBroker()
        response = stream_events(broker, token=None)
        stream = response.body_iterator
        first = json.loads((await anext(stream)).removeprefix("data: "))
        assert first["event_type"] == "stream"
        assert first["backend_instance_id"]
        broker.publish({"event_type": "log", "message": "ready"})
        event = json.loads((await anext(stream)).removeprefix("data: "))
        assert event["backend_instance_id"] == first["backend_instance_id"]
        assert event["event_sequence"] == 1
        broker.shutdown()
        assert [frame async for frame in stream] == []
        assert broker.subscriber_count == 0

    asyncio.run(consume())
