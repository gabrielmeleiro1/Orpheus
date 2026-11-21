"""Event broker primitives for desktop API streaming."""

from __future__ import annotations

import json
import queue
import threading
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from itertools import count
from typing import Any, AsyncIterator, Iterator
from uuid import uuid4

import anyio

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
        self.instance_id = uuid4().hex
        self._closed = False

    @property
    def subscriber_count(self) -> int:
        """Current live SSE subscriber count."""
        with self._subscribers_lock:
            return len(self._subscribers)

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
        sequence = next(self._event_counter)
        decorated.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        decorated.setdefault("event_id", str(sequence))
        decorated["backend_instance_id"] = self.instance_id
        decorated["event_sequence"] = sequence
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

    def publish(self, event: Any) -> None:
        normalized = normalize_event(event)
        with self._subscribers_lock:
            if self._closed:
                return
            payload = self._decorate_payload(normalized)
            for subscriber in self._subscribers:
                self._force_put(subscriber, payload)


def normalize_event(event: Any) -> dict[str, Any]:
    """Normalize dataclass/dict payload into JSON-safe dict."""
    if is_dataclass(event):
        payload = asdict(event)
    elif isinstance(event, dict):
        payload = dict(event)
    else:
        payload = {"event_type": "log", "level": "info", "message": str(event)}
    if payload.get("job_id") is not None:
        payload.setdefault("client_request_id", None)
    return payload


def sse_frame(payload: dict[str, Any]) -> str:
    """Encode one SSE frame from payload."""
    return f"data: {json.dumps(payload, ensure_ascii=True)}\n\n"


_STREAM_END = object()


async def close_with_response(frames: Iterator[str]) -> AsyncIterator[str]:
    """Iterate blocking SSE frames off the event loop and close them on exit.

    Starlette's own thread-pool iteration abandons the generator when the
    client disconnects, leaving it in a reference cycle: its broker queue stays
    subscribed until the cyclic collector happens to run. Closing explicitly
    runs the generator's cleanup as soon as the response ends.
    """
    try:
        while (frame := await anyio.to_thread.run_sync(next, frames, _STREAM_END)) is not _STREAM_END:
            yield frame
    finally:
        # Cancellation waits for the worker thread, so the generator is idle here.
        close = getattr(frames, "close", None)
        if callable(close):
            close()


def event_stream(
    broker: EventBroker, heartbeat_seconds: float = 5.0, *, announce_instance: bool = False
) -> Iterator[str]:
    """Yield SSE frames for one subscribed client."""
    subscriber = broker.subscribe()
    try:
        if announce_instance:
            # Subscribe before announcing identity so events published while the
            # client reconciles are queued. This message does not advance the
            # event sequence or replay events from earlier connections.
            yield sse_frame({"event_type": "stream", "backend_instance_id": broker.instance_id})
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
