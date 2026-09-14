"""Concurrency regressions for lazily initialized web services."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from fastapi.testclient import TestClient

from modules.web.server import create_app


class _CountingController:
    instances = 0
    instances_lock = threading.Lock()

    def __init__(self) -> None:
        with self.instances_lock:
            type(self).instances += 1
        # Force first-request overlap so an unsynchronized factory is exposed.
        time.sleep(0.03)
        self.config = SimpleNamespace(temp_dir="temp")

    def get_library_count(self) -> int:
        return 0


def test_lazy_controller_factory_is_single_flight() -> None:
    _CountingController.instances = 0
    app = create_app(controller_factory=_CountingController)
    client = TestClient(app)
    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            responses = list(pool.map(lambda _index: client.get("/api/library/count"), range(16)))
    finally:
        client.close()

    assert all(response.status_code == 200 for response in responses)
    assert _CountingController.instances == 1
