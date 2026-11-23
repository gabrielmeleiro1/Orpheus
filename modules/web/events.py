"""Event broker primitives for desktop API streaming."""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from itertools import count
from typing import Any, AsyncIterator, Callable, Iterator
from uuid import uuid4

# Pushed to every subscriber queue on broker shutdown so streaming generators
# terminate cleanly instead of looping forever.
STREAM_CLOSE_SENTINEL = object()


class _NotifyingQueue(queue.Queue):
    """A subscriber queue that wakes an event-loop consumer after each put."""

    def __init__(self, maxsize: int, notify: Callable[[], None]) -> None:
        super().__init__(maxsize=maxsize)
        self.notify = notify


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

    def subscribe(self, notify: Callable[[], None] | None = None) -> queue.Queue[Any]:
        """Add a bounded subscriber queue.

        ``notify`` runs after every enqueue, from the publishing thread, and
        must not block.
        """
        subscriber: queue.Queue[Any] = (
            queue.Queue(maxsize=self._max_queue_size)
            if notify is None
            else _NotifyingQueue(self._max_queue_size, notify)
        )
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
        EventBroker._put_evicting_oldest(subscriber, item)
        notify = getattr(subscriber, "notify", None)
        if notify is not None:
            notify()

    @staticmethod
    def _put_evicting_oldest(subscriber: queue.Queue[Any], item: Any) -> None:
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


_PING_FRAME = "event: ping\ndata: {}\n\n"


async def async_event_stream(
    broker: EventBroker, heartbeat_seconds: float = 5.0, *, announce_instance: bool = False
) -> AsyncIterator[str]:
    """Yield SSE frames for one client on the event loop, without a worker thread.

    Publishers enqueue from any thread exactly as for :func:`event_stream`;
    each put schedules a wakeup on this loop. Cancellation (a disconnect)
    interrupts the wait immediately and the ``finally`` unsubscribes.
    """
    loop = asyncio.get_running_loop()
    wakeup = asyncio.Event()

    def notify() -> None:
        try:
            loop.call_soon_threadsafe(wakeup.set)
        except RuntimeError:
            pass  # The loop has closed; the stream is gone with it.

    subscriber = broker.subscribe(notify=notify)
    try:
        if announce_instance:
            # Subscribe before announcing identity so events published while the
            # client reconciles are queued. This message does not advance the
            # event sequence or replay events from earlier connections.
            yield sse_frame({"event_type": "stream", "backend_instance_id": broker.instance_id})
        while True:
            try:
                payload = subscriber.get_nowait()
            except queue.Empty:
                wakeup.clear()
                # A put that landed before the clear has its item queued already.
                if subscriber.empty():
                    try:
                        await asyncio.wait_for(wakeup.wait(), heartbeat_seconds)
                    except asyncio.TimeoutError:
                        yield _PING_FRAME
                continue
            if payload is STREAM_CLOSE_SENTINEL:
                break
            yield sse_frame(payload)
    finally:
        broker.unsubscribe(subscriber)


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
                yield _PING_FRAME
                continue
            if payload is STREAM_CLOSE_SENTINEL:
                break
            yield sse_frame(payload)
    finally:
        broker.unsubscribe(subscriber)
