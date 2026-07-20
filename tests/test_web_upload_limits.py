from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from modules.app.config import AppConfig
from modules.app.controller import AppController
from modules.app.services import library_service
from modules.web.routes import uploads
from modules.web.server import create_app
from modules.web.source_handles import upload_root


def _make_upload_client(tmp_path):
    controller = AppController(
        config=AppConfig(
            data_dir=tmp_path / "data",
            output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp",
            db_path=tmp_path / "data" / "app.db",
        )
    )
    return TestClient(create_app(controller=controller)), controller


def test_source_upload_rejects_payloads_over_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "MAX_UPLOAD_BYTES", 10)
    client, controller = _make_upload_client(tmp_path)

    response = client.post(
        "/api/uploads/source",
        files={"file": ("oversized.txt", b"01234567890", "text/plain")},
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "Uploaded source exceeds 10 bytes"
    assert list(upload_root(controller.config.temp_dir).glob("*")) == []


def test_source_upload_accepts_payloads_at_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "MAX_UPLOAD_BYTES", 10)
    client, controller = _make_upload_client(tmp_path)

    response = client.post(
        "/api/uploads/source",
        files={"file": ("small.txt", b"0123456789", "text/plain")},
    )

    assert response.status_code == 200
    stored_name = response.json()["source_handle"].removeprefix("upload:")
    assert (upload_root(controller.config.temp_dir) / stored_name).read_bytes() == b"0123456789"


def test_source_upload_fallback_preflight_matches_frontend_contract(tmp_path, monkeypatch):
    def fail_preflight(_path):  # noqa: ANN001, ANN202
        raise ValueError("malformed source")

    monkeypatch.setattr(library_service, "inspect_source", fail_preflight)
    client, controller = _make_upload_client(tmp_path)

    response = client.post(
        "/api/uploads/source",
        files={"file": ("malformed.epub", b"not really epub", "application/epub+zip")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["preflight"] == {
        "title": "malformed",
        "author": None,
        "chapter_count": 0,
        "source_type": "epub",
        "sample": "",
        "warnings": ["Source may be malformed or unreadable: malformed source"],
        "size_bytes": len(b"not really epub"),
        "requires_ocr": False,
        "pdf_text_layer": None,
    }
    stored_name = payload["source_handle"].removeprefix("upload:")
    assert (upload_root(controller.config.temp_dir) / stored_name).exists()


def test_text_upload_rejects_empty_text(tmp_path):
    client, controller = _make_upload_client(tmp_path)

    response = client.post("/api/uploads/text", json={"text": "   \n\t", "title": "Notes"})

    assert response.status_code == 400
    assert response.json()["detail"] == "Pasted text is empty"
    assert list(upload_root(controller.config.temp_dir).glob("*")) == []


def test_text_upload_rejects_payloads_over_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "MAX_UPLOAD_BYTES", 10)
    client, controller = _make_upload_client(tmp_path)

    response = client.post("/api/uploads/text", json={"text": "01234567890", "title": "Notes"})

    assert response.status_code == 413
    assert response.json()["detail"] == "Pasted text exceeds 10 bytes"
    assert list(upload_root(controller.config.temp_dir).glob("*")) == []


def test_text_upload_stores_pasted_content_as_markdown(tmp_path):
    client, controller = _make_upload_client(tmp_path)

    response = client.post(
        "/api/uploads/text",
        json={"text": "Opening paragraph.\n\nSecond paragraph.", "title": "Web Notes"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["source_handle"].startswith("upload:")
    assert payload["original_name"].endswith(".md")
    assert payload["preflight"]["source_type"] == "md"
    assert payload["preflight"]["title"] == "Web Notes"
    assert payload["preflight"]["chapter_count"] == 1
    assert "Opening paragraph." in payload["preflight"]["sample"]

    stored_name = payload["source_handle"].removeprefix("upload:")
    stored_path = upload_root(controller.config.temp_dir) / stored_name
    assert stored_path.suffix == ".md"
    assert stored_path.read_text(encoding="utf-8").startswith("# Web Notes\n\nOpening paragraph.")


def test_text_upload_rejects_oversized_content_length_before_body(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "MAX_UPLOAD_BYTES", 10)
    monkeypatch.setattr(uploads, "CONTENT_LENGTH_SLACK_BYTES", 0)
    client, controller = _make_upload_client(tmp_path)
    calls = []
    monkeypatch.setattr(controller, "upload_text_source", lambda **kwargs: calls.append(kwargs))

    response = client.post("/api/uploads/text", json={"text": "0" * 64, "title": "Notes"})

    assert response.status_code == 413
    assert calls == []


def test_text_upload_rejects_oversized_chunked_body(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "MAX_UPLOAD_BYTES", 10)
    monkeypatch.setattr(uploads, "CONTENT_LENGTH_SLACK_BYTES", 0)
    client, controller = _make_upload_client(tmp_path)
    calls = []
    monkeypatch.setattr(controller, "upload_text_source", lambda **kwargs: calls.append(kwargs))

    def body_chunks():
        yield b'{"text":"'
        yield b"0" * 64
        yield b'"}'

    response = client.post(
        "/api/uploads/text",
        content=body_chunks(),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert calls == []


def test_text_upload_bounds_title_and_text_fields(tmp_path):
    from modules.web.routes.uploads import MAX_TEXT_CHARS, TextUploadRequest

    client, controller = _make_upload_client(tmp_path)
    del controller
    title_response = client.post("/api/uploads/text", json={"text": "ok", "title": "x" * 201})
    assert title_response.status_code == 422
    assert MAX_TEXT_CHARS == uploads.MAX_UPLOAD_BYTES
    assert TextUploadRequest.model_fields["text"].metadata[1].max_length == MAX_TEXT_CHARS


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not enforced on Windows")
def test_managed_upload_directory_and_file_are_owner_only(tmp_path):
    import stat

    client, controller = _make_upload_client(tmp_path)
    response = client.post("/api/uploads/text", json={"text": "private"})
    assert response.status_code == 200
    root = upload_root(controller.config.temp_dir)
    stored = root / response.json()["source_handle"].removeprefix("upload:")
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600


def test_source_upload_rejects_oversized_content_length_before_reading_body(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "MAX_UPLOAD_BYTES", 10)
    monkeypatch.setattr(uploads, "CONTENT_LENGTH_SLACK_BYTES", 0)
    client, controller = _make_upload_client(tmp_path)

    calls = []
    monkeypatch.setattr(
        controller, "upload_source", lambda **kwargs: calls.append(kwargs)
    )

    response = client.post(
        "/api/uploads/source",
        files={"file": ("oversized.txt", b"0" * 64, "text/plain")},
    )

    assert response.status_code == 413
    assert "Upload body exceeds" in response.json()["detail"]
    # The declared Content-Length is rejected before the handler ever runs.
    assert calls == []
    assert list(upload_root(controller.config.temp_dir).glob("*")) == []
