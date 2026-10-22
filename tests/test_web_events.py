"""
Test Web Event Broker + SSE Surface
===================================
Script-style tests for web event streaming primitives.
"""

import sys
import queue as queue_module
import time
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def test_published_event_carries_envelope_and_encodes_as_sse_frame() -> None:
    from modules.app.events import make_log_event
    from modules.web.events import EventBroker, sse_frame

    broker = EventBroker(max_queue_size=2)
    queue = broker.subscribe()
    broker.publish(make_log_event("hello", "info"))

    payload = queue.get(timeout=1.0)
    assert payload["event_type"] == "log"
    assert payload["message"] == "hello"
    assert "timestamp" in payload
    assert "event_id" in payload

    frame = sse_frame(payload)
    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    broker.unsubscribe(queue)


def test_event_broker_tolerates_full_queue_between_drop_and_publish(monkeypatch) -> None:
    from modules.app.events import make_log_event
    from modules.web.events import EventBroker

    broker = EventBroker(max_queue_size=1)
    subscriber = broker.subscribe()
    subscriber.put_nowait({"event_type": "log", "message": "old"})

    original_put_nowait = subscriber.put_nowait
    calls = 0

    def racing_put_nowait(item):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise queue_module.Full
        return original_put_nowait(item)

    monkeypatch.setattr(subscriber, "put_nowait", racing_put_nowait)

    broker.publish(make_log_event("new", "info"))

    assert subscriber.get_nowait()["message"] == "new"
    broker.unsubscribe(subscriber)


def test_event_broker_late_publish_cannot_displace_shutdown_sentinel() -> None:
    from modules.app.events import make_log_event
    from modules.web.events import EventBroker, STREAM_CLOSE_SENTINEL

    broker = EventBroker(max_queue_size=1)
    subscriber = broker.subscribe()

    broker.shutdown()
    broker.publish(make_log_event("too late", "info"))

    assert subscriber.get_nowait() is STREAM_CLOSE_SENTINEL
    broker.unsubscribe(subscriber)


def test_progress_event_throughput_delivers_latest_state_within_budget() -> None:
    from modules.app.events import make_progress_event
    from modules.web.events import EventBroker

    event_count = 2000
    runtime_budget_seconds = 1.0
    broker = EventBroker(max_queue_size=event_count)
    subscriber = broker.subscribe()

    started = time.perf_counter()
    for index in range(event_count):
        broker.publish(
            make_progress_event(
                stage="synthetic-throughput",
                progress=index / (event_count - 1),
                message=f"progress {index}",
            )
        )
    delivered = [subscriber.get(timeout=1.0) for _ in range(event_count)]
    elapsed = time.perf_counter() - started

    assert elapsed < runtime_budget_seconds
    assert len(delivered) == event_count
    assert delivered[0]["event_type"] == "progress"
    assert delivered[-1]["stage"] == "synthetic-throughput"
    assert delivered[-1]["progress"] == 1.0
    assert delivered[-1]["message"] == f"progress {event_count - 1}"
    broker.unsubscribe(subscriber)


if __name__ == "__main__":
    test_published_event_carries_envelope_and_encodes_as_sse_frame()
