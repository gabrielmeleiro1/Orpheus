from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

from modules.storage.maintenance import backup_database
from modules.storage.models import BookCreate, SourceType
from modules.web.server import create_app
from tests.test_backup_restore import controller as controller_fixture

controller = controller_fixture


@pytest.fixture
def audio_library(controller):
    audio = controller.config.output_dir / "book.mp3"
    audio.write_bytes(b"0123456789")
    book = controller.repository.create_book(BookCreate(
        title="Saved", source_path="source.txt", source_type=SourceType.TXT, playback_path=str(audio),
    ))
    client = TestClient(create_app(controller=controller, desktop_mode=True, desktop_api_token="secret"))
    return client, book, audio


def mint(client, book_id, epoch):
    return client.post("/api/desktop/query-token", headers={"Authorization": "Bearer secret"},
                       json={"scope": "audio", "book_id": book_id, "expected_epoch": epoch})


def test_audio_tokens_require_selected_epoch_and_expire_on_restore(controller, audio_library):
    client, book, _ = audio_library
    epoch = controller.repository.get_library_identity()["library_epoch"]
    assert mint(client, book.id, None).status_code == 422
    response = mint(client, book.id, epoch)
    assert response.status_code == 200
    token = response.json()["token"]
    url = f"/api/library/{book.id}/audio?token={token}"
    assert client.get(url).content == b"0123456789"
    backup = backup_database(controller.repository.db_path)
    controller.restore_database_backup(backup.name)
    assert client.get(url).status_code == 401
    assert mint(client, book.id, epoch).status_code == 409
    new_epoch = controller.repository.get_library_identity()["library_epoch"]
    new_token = mint(client, book.id, new_epoch).json()["token"]
    new_url = f"/api/library/{book.id}/audio?token={new_token}"
    assert client.head(new_url).status_code == 200
    response = client.get(new_url, headers={"Range": "bytes=2-5"})
    assert response.status_code == 206
    assert response.content == b"2345"
    assert client.get(new_url, headers={"Range": "bytes=0-1,8-9"}).status_code == 206


def test_audio_revalidates_after_middleware_before_resolving_file(controller, audio_library, monkeypatch):
    from modules.web.routes import library

    client, book, _ = audio_library
    epoch = controller.repository.get_library_identity()["library_epoch"]
    token = mint(client, book.id, epoch).json()["token"]
    backup = backup_database(controller.repository.db_path)
    snapshot = library.library_snapshot
    replaced = False

    def replace_before_resolution(service, *args, **kwargs):
        nonlocal replaced
        if not replaced:
            replaced = True
            controller.restore_database_backup(backup.name)
        return snapshot(service, *args, **kwargs)

    monkeypatch.setattr(library, "library_snapshot", replace_before_resolution)
    assert client.get(f"/api/library/{book.id}/audio?token={token}").status_code == 401


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot replace an open audio file")
def test_open_audio_stream_keeps_original_file_after_restore(controller, audio_library, monkeypatch):
    from modules.web.routes import library

    client, book, audio = audio_library
    epoch = controller.repository.get_library_identity()["library_epoch"]
    token = mint(client, book.id, epoch).json()["token"]
    backup = backup_database(controller.repository.db_path)
    original = library.FileResponse._handle_simple

    async def replace_after_open(response, send, header_only, pathsend):
        controller.restore_database_backup(backup.name)
        replacement = Path(str(audio) + ".new")
        replacement.write_bytes(b"replacement")
        replacement.replace(audio)
        return await original(response, send, header_only, pathsend)

    monkeypatch.setattr(library.FileResponse, "_handle_simple", replace_after_open)
    assert client.get(f"/api/library/{book.id}/audio?token={token}").content == b"0123456789"
