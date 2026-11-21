"""Transport-level guarantees of the desktop API: error envelopes and Host checks."""

from __future__ import annotations

import logging
import sqlite3
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from modules.contracts.limits import MAX_UPLOAD_BYTES
from modules.web.server import create_app

TOKEN = "desktop-test-token"
ORIGIN = "http://tauri.localhost"
SECRET_PATH = r"C:\Users\someone\Library\audiobooks.db"


class _FailingController:
    def get_library_count(self):
        raise sqlite3.OperationalError(f"unable to open database file {SECRET_PATH}")

    def upload_source(self, **_kwargs):
        raise OSError(28, "No space left on device", SECRET_PATH)


def _client(controller=None, **kwargs) -> TestClient:
    app = create_app(
        controller=controller or _FailingController(),
        desktop_mode=True,
        desktop_api_token=TOKEN,
        **kwargs,
    )
    return TestClient(app, raise_server_exceptions=False)


def _auth(**extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}", "Origin": ORIGIN, **extra}


def test_unexpected_route_error_returns_typed_body_the_webview_can_read(caplog) -> None:
    client = _client()

    with caplog.at_level(logging.ERROR, logger="modules.web.server"):
        response = client.get("/api/library/count", headers=_auth())

    assert response.status_code == 500
    # Without the CORS header the cross-origin WebView sees an opaque network
    # failure instead of a server error it can report and retry.
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert response.headers["content-type"].startswith("application/json")
    detail = response.json()["detail"]
    assert detail["code"] == "internal_error"
    assert detail["message"]
    assert SECRET_PATH not in response.text
    assert "OperationalError" not in response.text

    records = [record for record in caplog.records if record.name == "modules.web.server"]
    assert len(records) == 1
    assert records[0].exc_info is not None
    assert "GET /api/library/count" in records[0].getMessage()


def test_unexpected_upload_storage_error_uses_the_same_envelope() -> None:
    client = _client()

    response = client.post(
        "/api/uploads/source",
        headers=_auth(),
        files={"file": ("book.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "internal_error"
    assert SECRET_PATH not in response.text


def test_oversized_upload_still_answers_413_not_internal_error(caplog) -> None:
    controller = Mock()
    controller.upload_source.side_effect = AssertionError("body must be rejected before the route")
    client = _client(controller)

    def chunks():
        chunk = b"x" * (1024 * 1024)
        for _ in range(MAX_UPLOAD_BYTES // len(chunk) + 2):
            yield chunk

    with caplog.at_level(logging.ERROR, logger="modules.web.server"):
        response = client.post(
            "/api/uploads/source",
            headers=_auth(**{"Content-Type": "multipart/form-data; boundary=x"}),
            content=chunks(),
        )

    assert response.status_code == 413
    assert not [record for record in caplog.records if record.name == "modules.web.server"]


def _oversized_json() -> bytes:
    return b'{"source_path": "' + b"a" * (2 * 1024 * 1024) + b'"}'


def test_json_routes_reject_declared_oversized_bodies() -> None:
    controller = Mock()
    client = _client(controller)

    response = client.post(
        "/api/sources/preflight", headers=_auth(**{"Content-Type": "application/json"}), content=_oversized_json(),
    )

    assert response.status_code == 413
    assert response.headers["access-control-allow-origin"] == ORIGIN
    controller.preflight_source.assert_not_called()


def test_json_routes_reject_oversized_chunked_bodies_while_streaming() -> None:
    controller = Mock()
    client = _client(controller)
    sent = 0

    def chunks():
        nonlocal sent
        body = _oversized_json()
        for start in range(0, len(body), 64 * 1024):
            sent += 1
            yield body[start:start + 64 * 1024]

    response = client.put(
        "/api/library/1/playback-position", headers=_auth(**{"Content-Type": "application/json"}), content=chunks(),
    )

    assert response.status_code == 413
    controller.update_book_playback_position.assert_not_called()


def test_small_json_bodies_and_large_text_uploads_keep_working(tmp_path) -> None:
    source = tmp_path / "book.epub"
    source.write_bytes(b"epub")
    controller = Mock()
    controller.preflight_source.return_value = {"title": "Book"}
    controller.upload_text_source.return_value = {"source_handle": "upload:x"}
    client = _client(controller)

    preflight = client.post("/api/sources/preflight", headers=_auth(), json={"source_path": str(source)})
    paste = client.post("/api/uploads/text", headers=_auth(), json={"text": "word " * (512 * 1024)})

    assert preflight.status_code == 200
    assert paste.status_code == 200


def test_query_tokens_never_reach_the_internal_error_log(caplog) -> None:
    class _BrokenLibrary:
        def library_snapshot(self, _expected_epoch=None):
            raise sqlite3.DatabaseError("disk I/O error")

    client = _client(_BrokenLibrary())
    with caplog.at_level(logging.ERROR, logger="modules.web.server"):
        response = client.get("/api/library/1/audio?token=query-secret", headers=_auth())

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "internal_error"
    assert "GET /api/library/1/audio" in caplog.text
    assert "query-secret" not in caplog.text


@pytest.mark.parametrize("path,body", [
    # A missing field reports the whole request object as its input.
    ("/api/voices/suggest", {"cleaner_api_key": "sk-synthetic-secret"}),
    ("/api/options/openai/models", {"api_key": ["sk-synthetic-secret"]}),
])
def test_validation_errors_do_not_echo_request_values(path: str, body: dict) -> None:
    client = _client(Mock())

    response = client.post(path, headers=_auth(), json=body)

    assert response.status_code == 422
    assert "sk-synthetic-secret" not in response.text
    detail = response.json()["detail"]
    assert detail and all(item["msg"] and item["loc"] for item in detail)


@pytest.mark.parametrize("host", ["attacker.example", "attacker.example:4000", "127.0.0.1.nip.io:4000", "", "[::2]:4000"])
def test_loopback_host_enforcement_rejects_rebound_hosts(host: str) -> None:
    client = _client(enforce_loopback_host=True)

    health = client.get("/health", headers={"Host": host})
    api = client.get("/api/library/count", headers=_auth(Host=host))

    assert health.status_code == 421
    assert health.json()["detail"]["code"] == "invalid_host"
    assert api.status_code == 421


@pytest.mark.parametrize("host", ["127.0.0.1:4000", "127.0.0.1", "localhost:4000", "LOCALHOST:4000", "[::1]:4000", "127.0.0.2:4000"])
def test_loopback_host_enforcement_accepts_loopback_hosts(host: str) -> None:
    client = _client(enforce_loopback_host=True)

    assert client.get("/health", headers={"Host": host}).status_code == 200
    assert client.get("/api/library/count", headers={"Host": host}).status_code == 401


def test_launcher_enables_loopback_host_enforcement(monkeypatch) -> None:
    from apps.launchers import desktop_backend

    monkeypatch.setattr(desktop_backend, "configure_espeak_runtime", Mock())
    monkeypatch.setattr(desktop_backend.multiprocessing, "freeze_support", Mock())
    monkeypatch.setattr(desktop_backend, "_runtime_profile_identity", lambda: "base")
    create = Mock()
    monkeypatch.setattr(desktop_backend, "create_app", create)
    monkeypatch.setattr(desktop_backend, "_build_desktop_server", Mock(return_value=Mock(ready_emitted=True)))
    monkeypatch.setattr(desktop_backend, "_start_desktop_parent_watchdog", Mock(return_value=None))

    assert desktop_backend.main([]) == 0
    assert create.call_args.kwargs["enforce_loopback_host"] is True
