"""Shutdown / lifecycle tests for the SSE EventBroker."""

from __future__ import annotations

import threading
import time

from modules.app.events import make_log_event
from modules.web.events import STREAM_CLOSE_SENTINEL, EventBroker, event_stream


def test_shutdown_pushes_sentinel_to_live_subscribers() -> None:
    broker = EventBroker()
    subscriber = broker.subscribe()

    broker.shutdown()

    assert subscriber.get_nowait() is STREAM_CLOSE_SENTINEL


def test_subscribe_after_shutdown_receives_sentinel_immediately() -> None:
    broker = EventBroker()
    broker.shutdown()

    subscriber = broker.subscribe()
    assert subscriber.get_nowait() is STREAM_CLOSE_SENTINEL


def test_event_stream_generator_terminates_on_shutdown() -> None:
    broker = EventBroker()
    frames: list[str] = []

    def consume() -> None:
        for frame in event_stream(broker, heartbeat_seconds=60.0):
            frames.append(frame)

    consumer = threading.Thread(target=consume)
    consumer.start()

    # Wait for the generator to subscribe (real condition, bounded).
    deadline = time.monotonic() + 2.0
    while broker.subscriber_count < 1 and time.monotonic() < deadline:
        pass
    assert broker.subscriber_count == 1

    broker.shutdown()
    consumer.join(timeout=2.0)

    assert not consumer.is_alive()
    assert broker.subscriber_count == 0


def test_event_stream_unsubscribes_on_generator_close() -> None:
    broker = EventBroker()
    stream = event_stream(broker, heartbeat_seconds=0.01)

    # First iteration subscribes; empty queue yields a heartbeat ping.
    ping = next(stream)
    assert ping.startswith("event: ping")
    assert broker.subscriber_count == 1

    stream.close()
    assert broker.subscriber_count == 0


def test_dropped_event_count_increments_on_queue_overflow() -> None:
    broker = EventBroker(max_queue_size=1)
    subscriber = broker.subscribe()

    broker.publish(make_log_event("one", "info"))
    assert broker.dropped_event_count == 0

    broker.publish(make_log_event("two", "info"))
    assert broker.dropped_event_count == 1

    broker.publish(make_log_event("three", "info"))
    assert broker.dropped_event_count == 2

    # The newest event is retained after the overflow drops.
    assert subscriber.get_nowait()["message"] == "three"
    broker.unsubscribe(subscriber)
