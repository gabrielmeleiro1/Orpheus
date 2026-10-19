"""Regression coverage for library routes."""

from unittest.mock import Mock

from fastapi.testclient import TestClient

from modules.storage.models import BookCreate, SourceType
from modules.web.server import create_app

from tests import test_conversion_upload_lifecycle as _upload_lifecycle


controller = _upload_lifecycle.controller


def test_partial_book_deletion_uses_opaque_folder_handle(controller, monkeypatch):
    from modules.app.services import library_service

    source = controller.config.temp_dir / "web_uploads" / "book.txt"
    source.parent.mkdir()
    source.write_text("recoverable")
    book = controller.repository.create_book(BookCreate(title="Book", source_path=str(source), source_type=SourceType.TXT))
    monkeypatch.setattr(controller._library_service, "_commit_staged_artifacts", Mock(side_effect=OSError("locked")))
    opened = []
    monkeypatch.setattr(library_service, "_default_folder_opener", opened.append)
    with TestClient(create_app(controller=controller)) as client:
        response = client.delete(f"/api/library/{book.id}")
        assert response.status_code == 200
        handle = response.json()["path"]
        assert len(handle) == 32 and all(c in "0123456789abcdef" for c in handle)
        response = client.post("/api/maintenance/delete-staging/open-folder", json={"path": handle})
        assert response.status_code == 200
        assert response.json() == {"folder_path": handle}
        assert opened == [controller.config.temp_dir / "delete_staging" / handle]
        for invalid in (str(opened[0]), "..", "../" + handle):
            assert client.post("/api/maintenance/delete-staging/open-folder", json={"path": invalid}).status_code == 404
