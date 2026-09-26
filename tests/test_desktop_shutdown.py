"""Authenticated shutdown of the desktop Uvicorn server."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from apps.launchers.desktop_backend import _build_desktop_server
from modules.web.events import STREAM_CLOSE_SENTINEL
from modules.web.server import create_app


@pytest.mark.parametrize("authorization", [None, "Bearer wrong"])
def test_shutdown_rejects_missing_or_wrong_token(authorization):
    app = create_app(controller=SimpleNamespace(), desktop_mode=True, desktop_api_token="secret")
    server = _build_desktop_server(app, host="127.0.0.1", port=0, token="secret", runtime_profile="base")
    headers = {} if authorization is None else {"Authorization": authorization}
    response = TestClient(app).post("/api/desktop/shutdown", headers=headers)
    assert response.status_code == 401
    assert not server.should_exit


def test_authenticated_shutdown_closes_streams_and_requests_uvicorn_exit():
    app = create_app(controller=SimpleNamespace(), desktop_mode=True, desktop_api_token="secret")
    server = _build_desktop_server(app, host="127.0.0.1", port=0, token="secret", runtime_profile="base")
    subscriber = app.state.event_broker.subscribe()
    client = TestClient(app)
    for _ in range(2):
        response = client.post("/api/desktop/shutdown", headers={"Authorization": "Bearer secret"})
        assert response.status_code == 202
    assert server.should_exit
    assert subscriber.get_nowait() is STREAM_CLOSE_SENTINEL
    assert subscriber.empty()


def test_shutdown_is_not_exposed_outside_desktop_mode():
    app = create_app(controller=SimpleNamespace())
    assert TestClient(app).post("/api/desktop/shutdown").status_code == 404


def test_shutdown_without_server_callback_reports_unavailable():
    app = create_app(controller=SimpleNamespace(), desktop_mode=True, desktop_api_token="secret")
    response = TestClient(app).post("/api/desktop/shutdown", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 503
