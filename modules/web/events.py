"""Event broker primitives for desktop API streaming."""

from __future__ import annotations

import json
import queue
import threading
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from itertools import count
from typing import Any, Iterator

# Pushed to every subscriber queue on broker shutdown so streaming generators
# terminate cleanly instead of looping forever.
STREAM_CLOSE_SENTINEL = object()


class EventBroker:
    """Fan-out event broker using per-subscriber queues."""

    def __init__(self, max_queue_size: int = 256):
        self._max_queue_size = max_queue_size
        self._subscribers: list[queue.Queue[Any]] = []
        self._subscribers_lock = threading.Lock()
        self._event_counter = count(1)
        self._dropped_event_count = 0
        self._closed = False

    @property
    def subscriber_count(self) -> int:
        """Current live SSE subscriber count."""
        with self._subscribers_lock:
            return len(self._subscribers)

    @property
    def dropped_event_count(self) -> int:
        """Diagnostic count of events dropped due to full subscriber queues."""
        with self._subscribers_lock:
            return self._dropped_event_count

    def subscribe(self) -> queue.Queue[Any]:
        subscriber: queue.Queue[Any] = queue.Queue(maxsize=self._max_queue_size)
        with self._subscribers_lock:
            self._subscribers.append(subscriber)
            closed = self._closed
        # A subscriber that arrives after shutdown must still terminate promptly.
        if closed:
            self._force_put(subscriber, STREAM_CLOSE_SENTINEL)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[Any]) -> None:
        with self._subscribers_lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    def shutdown(self) -> None:
        """Signal every subscriber to terminate its stream and stop fan-out."""
        with self._subscribers_lock:
            if self._closed:
                return
            self._closed = True
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            self._force_put(subscriber, STREAM_CLOSE_SENTINEL)

    def _decorate_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        decorated = dict(payload)
        decorated.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        decorated.setdefault("event_id", str(next(self._event_counter)))
        return decorated

    @staticmethod
    def _force_put(subscriber: queue.Queue[Any], item: Any) -> None:
        """Enqueue ``item`` even when the queue is full, evicting the oldest."""
        try:
            subscriber.put_nowait(item)
            return
        except queue.Full:
            pass

        try:
            subscriber.get_nowait()
        except queue.Empty:
            pass

        try:
            subscriber.put_nowait(item)
        except queue.Full:
            pass

    def _publish_to_subscriber(
        self,
        subscriber: queue.Queue[Any],
        payload: dict[str, Any],
    ) -> bool:
        """Deliver ``payload``; return True if an old event was dropped."""
        try:
            subscriber.put_nowait(payload)
            return False
        except queue.Full:
            pass

        dropped = False
        try:
            subscriber.get_nowait()
            dropped = True
        except queue.Empty:
            pass

        try:
            subscriber.put_nowait(payload)
        except queue.Full:
            pass

        return dropped

    def publish(self, event: Any) -> None:
        payload = self._decorate_payload(normalize_event(event))
        with self._subscribers_lock:
            subscribers = list(self._subscribers)

        dropped = 0
        for subscriber in subscribers:
            if self._publish_to_subscriber(subscriber, payload):
                dropped += 1

        if dropped:
            with self._subscribers_lock:
                self._dropped_event_count += dropped


def normalize_event(event: Any) -> dict[str, Any]:
    """Normalize dataclass/dict payload into JSON-safe dict."""
    if is_dataclass(event):
        payload = asdict(event)
    elif isinstance(event, dict):
        payload = dict(event)
    else:
        payload = {"event_type": "log", "level": "info", "message": str(event)}
    return payload


def sse_frame(payload: dict[str, Any]) -> str:
    """Encode one SSE frame from payload."""
    return f"data: {json.dumps(payload, ensure_ascii=True)}\n\n"


def event_stream(broker: EventBroker, heartbeat_seconds: float = 5.0) -> Iterator[str]:
    """Yield SSE frames for one subscribed client."""
    subscriber = broker.subscribe()
    try:
        while True:
            try:
                payload = subscriber.get(timeout=heartbeat_seconds)
            except queue.Empty:
                yield "event: ping\ndata: {}\n\n"
                continue
            if payload is STREAM_CLOSE_SENTINEL:
                break
            yield sse_frame(payload)
    finally:
        broker.unsubscribe(subscriber)
