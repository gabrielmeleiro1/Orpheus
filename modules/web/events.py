"""Event broker primitives for desktop API streaming."""

from __future__ import annotations

import json
import queue
import threading
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from itertools import count
from typing import Any, Iterator


class EventBroker:
    """Fan-out event broker using per-subscriber queues."""

    def __init__(self, max_queue_size: int = 256):
        self._max_queue_size = max_queue_size
        self._subscribers: list[queue.Queue[dict[str, Any]]] = []
        self._subscribers_lock = threading.Lock()
        self._event_counter = count(1)

    @property
    def subscriber_count(self) -> int:
        """Current live SSE subscriber count."""
        with self._subscribers_lock:
            return len(self._subscribers)

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=self._max_queue_size)
        with self._subscribers_lock:
            self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any]]) -> None:
        with self._subscribers_lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    def _decorate_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        decorated = dict(payload)
        decorated.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        decorated.setdefault("event_id", str(next(self._event_counter)))
        return decorated

    @staticmethod
    def _publish_to_subscriber(
        subscriber: queue.Queue[dict[str, Any]],
        payload: dict[str, Any],
    ) -> None:
        try:
            subscriber.put_nowait(payload)
            return
        except queue.Full:
            pass

        try:
            subscriber.get_nowait()
        except queue.Empty:
            pass

        try:
            subscriber.put_nowait(payload)
        except queue.Full:
            pass

    def publish(self, event: Any) -> None:
        payload = self._decorate_payload(normalize_event(event))
        with self._subscribers_lock:
            subscribers = list(self._subscribers)

        for subscriber in subscribers:
            self._publish_to_subscriber(subscriber, payload)


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
                yield sse_frame(payload)
            except queue.Empty:
                yield "event: ping\ndata: {}\n\n"
    finally:
        broker.unsubscribe(subscriber)
