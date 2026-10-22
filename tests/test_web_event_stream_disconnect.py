"""A disconnected SSE client must release its broker subscription promptly."""

from __future__ import annotations

import gc
import socket
import threading
import time

import pytest
import uvicorn

from modules.web.events import EventBroker
from modules.web.server import create_app


TOKEN = "desktop-test-token"


# Desktop mode adds the auth middleware between the server and the stream.
@pytest.fixture(params=[False, True], ids=["plain", "desktop"])
def served_broker(request):
    broker = EventBroker()
    server = uvicorn.Server(uvicorn.Config(
        create_app(controller=object(), broker=broker, desktop_mode=request.param, desktop_api_token=TOKEN),
        host="127.0.0.1", port=0, log_level="warning", access_log=False,
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.02)
    try:
        yield broker, server.servers[0].sockets[0].getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(10)


def _open_stream(port: int) -> socket.socket:
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    client.sendall(
        f"GET /api/events/stream HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        f"Authorization: Bearer {TOKEN}\r\n\r\n".encode()
    )
    received = b""
    while b"backend_instance_id" not in received:
        received += client.recv(1024)
    return client


def _wait_for_subscribers(broker: EventBroker, expected: int, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    while broker.subscriber_count != expected and time.monotonic() < deadline:
        # Wake blocked stream threads the way live conversion events would.
        broker.publish({"event_type": "log", "level": "info", "message": "tick"})
        time.sleep(0.05)
    return broker.subscriber_count


def test_disconnected_streams_unsubscribe_without_garbage_collection(served_broker) -> None:
    broker, port = served_broker
    clients = [_open_stream(port) for _ in range(4)]
    assert _wait_for_subscribers(broker, 4, timeout=5) == 4

    # Abandoned subscriptions sit in reference cycles; release must not depend
    # on when the cyclic collector next happens to run.
    gc.disable()
    try:
        for client in clients:
            client.close()
        assert _wait_for_subscribers(broker, 0, timeout=8) == 0
    finally:
        gc.enable()
