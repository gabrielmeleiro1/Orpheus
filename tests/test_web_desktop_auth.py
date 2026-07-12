"""Desktop API token auth contract tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from modules.web.server import create_app

TOKEN = "desktop-test-token"


@dataclass
class _FakeBookSummary:
    id: int
    title: str
    author: str
    total_chapters: int
    completed_chapters: int
    created_at: datetime


class _FakeLibraryRepository:
    def __init__(self) -> None:
        self.list_calls: list[dict[str, object]] = []

    def list_books(  # noqa: ANN001,ANN201
        self,
        *,
        search=None,
        limit=200,
        offset=0,
        conversion_mode=None,
        favorite=None,
    ):
        self.list_calls.append(
            {
                "search": search,
                "limit": limit,
                "offset": offset,
                "conversion_mode": conversion_mode,
                "favorite": favorite,
            }
        )
        return [
            _FakeBookSummary(
                id=1,
                title="Desktop Auth Book",
                author="Test Author",
                total_chapters=1,
                completed_chapters=1,
                created_at=datetime.now(timezone.utc),
            )
        ]


class _FakeController:
    def __init__(self, playback_path: Path, *, output_dir: Path | None = None) -> None:
        self._playback_path = playback_path
        self._repository = _FakeLibraryRepository()
        self.config = SimpleNamespace(
            output_dir=output_dir or playback_path.parent,
            temp_dir=(output_dir or playback_path.parent) / "temp",
        )

    def get_library_books(  # noqa: ANN001,ANN201
        self,
        search=None,
        limit=200,
        offset=0,
        conversion_mode=None,
        favorite=None,
    ):
        return self._repository.list_books(
            search=search,
            limit=limit,
            offset=offset,
            conversion_mode=conversion_mode,
            favorite=favorite,
        )

    def get_book(self, book_id: int):  # noqa: ANN201
        if book_id != 1:
            return None
        return {
            "id": 1,
            "title": "Desktop Auth Book",
            "playback_path": str(self._playback_path),
        }

    def get_book_playback_file(self, book_id: int):  # noqa: ANN201
        if book_id != 1:
            return None
        raw_path = self._playback_path
        path = raw_path if raw_path.is_absolute() else self.config.output_dir.parent / raw_path
        try:
            path.resolve().relative_to(self.config.output_dir.resolve())
        except ValueError:
            return None
        if not path.exists() or not path.is_file():
            return None
        return path


def _make_desktop_client(controller: _FakeController) -> TestClient:
    return TestClient(
        create_app(
            controller=controller,
            desktop_mode=True,
            desktop_api_token=TOKEN,
        )
    )


def _make_web_client(controller: _FakeController) -> TestClient:
    return TestClient(create_app(controller=controller))


def _mint_query_token(client: TestClient, scope: str, *, book_id: int | None = None) -> str:
    payload: dict[str, object] = {"scope": scope}
    if book_id is not None:
        payload["book_id"] = book_id
    response = client.post(
        "/api/desktop/query-token",
        json=payload,
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200
    token = response.json()["token"]
    assert isinstance(token, str)
    assert token
    return token


def _create_query_token(client: TestClient, scope: str, *, book_id: int | None = None) -> dict[str, object]:
    payload: dict[str, object] = {"scope": scope}
    if book_id is not None:
        payload["book_id"] = book_id
    response = client.post(
        "/api/desktop/query-token",
        json=payload,
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 200
    return response.json()


def test_desktop_mode_health_remains_open(tmp_path: Path) -> None:
    audio_path = tmp_path / "sample.m4b"
    audio_path.write_bytes(b"ID3desktop-audio")
    controller = _FakeController(audio_path)

    with _make_desktop_client(controller) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_desktop_mode_library_requires_bearer_token(tmp_path: Path) -> None:
    audio_path = tmp_path / "sample.m4b"
    audio_path.write_bytes(b"ID3desktop-audio")
    controller = _FakeController(audio_path)

    with _make_desktop_client(controller) as client:
        missing = client.get("/api/library")
        query_only = client.get(f"/api/library?token={TOKEN}")
        authed = client.get("/api/library", headers={"Authorization": f"Bearer {TOKEN}"})

    assert missing.status_code == 401
    assert query_only.status_code == 401
    assert authed.status_code == 200
    assert authed.json()["count"] == 1


def test_desktop_mode_library_forwards_offset_to_repository(tmp_path: Path) -> None:
    audio_path = tmp_path / "sample.m4b"
    audio_path.write_bytes(b"ID3desktop-audio")
    controller = _FakeController(audio_path)

    with _make_desktop_client(controller) as client:
        response = client.get(
            "/api/library?offset=25&limit=10",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    assert response.status_code == 200
    assert controller._repository.list_calls == [  # noqa: SLF001
        {
            "search": None,
            "limit": 10,
            "offset": 25,
            "conversion_mode": None,
            "favorite": None,
        }
    ]

def test_desktop_mode_allows_packaged_tauri_origin_for_api_cors(tmp_path: Path) -> None:
    audio_path = tmp_path / "sample.m4b"
    audio_path.write_bytes(b"ID3desktop-audio")
    controller = _FakeController(audio_path)

    with _make_desktop_client(controller) as client:
        preflight = client.options(
            "/api/library",
            headers={
                "Origin": "http://tauri.localhost",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        authed = client.get(
            "/api/library",
            headers={
                "Origin": "http://tauri.localhost",
                "Authorization": f"Bearer {TOKEN}",
            },
        )

    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://tauri.localhost"
    assert authed.status_code == 200
    assert authed.headers["access-control-allow-origin"] == "http://tauri.localhost"
    assert authed.json()["count"] == 1


def test_desktop_mode_cors_allows_only_exact_vite_dev_origin(tmp_path: Path) -> None:
    audio_path = tmp_path / "sample.m4b"
    audio_path.write_bytes(b"ID3desktop-audio")
    controller = _FakeController(audio_path)

    with _make_desktop_client(controller) as client:
        allowed = client.options(
            "/api/library",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        arbitrary_localhost = client.options(
            "/api/library",
            headers={
                "Origin": "http://localhost:9999",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        arbitrary_loopback = client.options(
            "/api/library",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )

    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert arbitrary_localhost.status_code == 400
    assert "access-control-allow-origin" not in arbitrary_localhost.headers
    assert arbitrary_loopback.status_code == 400
    assert "access-control-allow-origin" not in arbitrary_loopback.headers


def test_desktop_mode_requires_scoped_sse_query_token(tmp_path: Path) -> None:
    audio_path = tmp_path / "sample.m4b"
    audio_path.write_bytes(b"ID3desktop-audio")
    controller = _FakeController(audio_path)

    with _make_desktop_client(controller) as client:
        with patch(
            "modules.web.routes.events.event_stream",
            return_value=iter(["event: ping\ndata: {}\n\n"]),
        ):
            long_lived = client.get(f"/api/events/stream?token={TOKEN}")
            scoped_token = _mint_query_token(client, "events")
            response = client.get(f"/api/events/stream?token={scoped_token}")

    assert long_lived.status_code == 401
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: ping" in response.text


def test_desktop_query_tokens_keep_event_ttl_short_and_audio_ttl_longer(tmp_path: Path) -> None:
    audio_path = tmp_path / "sample.m4b"
    audio_path.write_bytes(b"ID3desktop-audio")
    controller = _FakeController(audio_path)

    with _make_desktop_client(controller) as client:
        with patch("modules.web.desktop_auth.time.time", return_value=1_700_000_000):
            events = _create_query_token(client, "events")
            audio = _create_query_token(client, "audio", book_id=1)

    assert events["expires_at"] == 1_700_000_060
    assert isinstance(audio["expires_at"], int)
    assert audio["expires_at"] > 1_700_000_060


def test_desktop_mode_requires_scoped_audio_query_token(tmp_path: Path) -> None:
    audio_path = tmp_path / "sample.m4b"
    expected_prefix = b"ID3desktop-audio"
    audio_path.write_bytes(expected_prefix)
    controller = _FakeController(audio_path)

    with _make_desktop_client(controller) as client:
        long_lived = client.get(f"/api/library/1/audio?token={TOKEN}")
        wrong_scope_token = _mint_query_token(client, "events")
        wrong_scope = client.get(f"/api/library/1/audio?token={wrong_scope_token}")
        scoped_token = _mint_query_token(client, "audio", book_id=1)
        response = client.get(f"/api/library/1/audio?token={scoped_token}")

    assert long_lived.status_code == 401
    assert wrong_scope.status_code == 401
    assert response.status_code == 200
    assert response.content.startswith(expected_prefix)


def test_desktop_audio_rejects_playback_path_outside_output_root(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    outside_audio = tmp_path / "outside.m4b"
    outside_audio.write_bytes(b"\x00\x00\x00\x18ftypM4A outside-audio")
    controller = _FakeController(outside_audio, output_dir=output_dir)

    with _make_desktop_client(controller) as client:
        scoped_token = _mint_query_token(client, "audio", book_id=1)
        response = client.get(f"/api/library/1/audio?token={scoped_token}")

    assert response.status_code == 404


def test_desktop_audio_rejects_output_relative_traversal(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    outside_audio = tmp_path / "outside.m4b"
    outside_audio.write_bytes(b"\x00\x00\x00\x18ftypM4A outside-audio")
    controller = _FakeController(Path("output/../outside.m4b"), output_dir=output_dir)

    with _make_desktop_client(controller) as client:
        scoped_token = _mint_query_token(client, "audio", book_id=1)
        response = client.get(f"/api/library/1/audio?token={scoped_token}")

    assert response.status_code == 404


def test_desktop_audio_response_uses_inline_playable_media_headers(tmp_path: Path) -> None:
    for suffix in (".m4b", ".m4a"):
        audio_path = tmp_path / f"sample{suffix}"
        audio_path.write_bytes(b"\x00\x00\x00\x18ftypM4A desktop-audio")
        controller = _FakeController(audio_path)

        with _make_desktop_client(controller) as client:
            scoped_token = _mint_query_token(client, "audio", book_id=1)
            response = client.get(
                f"/api/library/1/audio?token={scoped_token}",
                headers={
                    "Origin": "http://tauri.localhost",
                    "Range": "bytes=0-1",
                },
            )

        assert response.status_code == 206
        assert response.headers["content-type"].startswith("audio/mp4")
        assert response.headers["content-disposition"].startswith("inline;")
        assert response.headers["accept-ranges"] == "bytes"


def test_desktop_audio_route_supports_head_media_probe(tmp_path: Path) -> None:
    audio_path = tmp_path / "sample.m4b"
    audio_path.write_bytes(b"\x00\x00\x00\x18ftypM4A desktop-audio")
    controller = _FakeController(audio_path)

    with _make_desktop_client(controller) as client:
        scoped_token = _mint_query_token(client, "audio", book_id=1)
        response = client.head(
            f"/api/library/1/audio?token={scoped_token}",
            headers={"Origin": "http://tauri.localhost"},
        )

    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-type"].startswith("audio/mp4")
    assert response.headers["content-disposition"].startswith("inline;")
    assert response.headers["access-control-allow-origin"] == "http://tauri.localhost"


def test_non_desktop_mode_remains_unauthenticated(tmp_path: Path) -> None:
    audio_path = tmp_path / "sample.m4b"
    audio_path.write_bytes(b"ID3desktop-audio")
    controller = _FakeController(audio_path)

    with _make_web_client(controller) as client:
        response = client.get("/api/library")

    assert response.status_code == 200
    assert response.json()["count"] == 1


def _forged_query_token(scope: str) -> str:
    import base64
    import time

    encoded_scope = base64.urlsafe_b64encode(scope.encode("utf-8")).decode("ascii").rstrip("=")
    expires_at = int(time.time()) + 60
    return f"v1.{encoded_scope}.{expires_at}.nonce.signature"


def test_query_token_with_non_ascii_scope_is_rejected_without_error() -> None:
    from modules.web.desktop_auth import validate_desktop_query_token

    # ``hmac.compare_digest`` raises TypeError on non-ASCII str inputs; an
    # attacker-controlled decoded scope must yield False, not an exception.
    assert validate_desktop_query_token(_forged_query_token("évents"), TOKEN, "events") is False


def test_desktop_mode_non_ascii_scope_query_token_returns_401(tmp_path: Path) -> None:
    audio_path = tmp_path / "sample.m4b"
    audio_path.write_bytes(b"ID3desktop-audio")
    controller = _FakeController(audio_path)

    with _make_desktop_client(controller) as client:
        response = client.get(f"/api/events/stream?token={_forged_query_token('évents')}")

    assert response.status_code == 401
