from types import SimpleNamespace
from unittest.mock import Mock
import os
import time

import pytest
from fastapi.testclient import TestClient

from modules.app.services.library_service import LibraryService
from modules.pipeline.recovery import prepare_recovery_source
from modules.storage.maintenance import audit_storage_state, prune_stale_records_and_files
from modules.storage.models import BookCreate, ChapterCreate, SourceType
from modules.storage.sqlite_repo import SQLiteRepository
from modules.web.server import create_app
from tests.test_web_api import _make_client_with_controller


@pytest.mark.parametrize("locked_artifact", ["book", "source"])
def test_delete_keeps_all_files_and_book_when_an_artifact_is_locked(tmp_path, monkeypatch, locked_artifact):
    output = tmp_path / "output"
    book_dir = output / "Book"
    source = tmp_path / "data" / "sources" / "book.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"original source")
    originals = {
        source: b"original source",
        book_dir / ".orpheus-recovery.json": b"inventory",
        book_dir / ".orpheus-source" / "book.txt": b"recovery source",
        book_dir / "chapters" / "chapter_01.mp3": b"first chapter",
        book_dir / "Book.m4b": b"audiobook",
    }
    for path, content in originals.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    repo = SQLiteRepository(tmp_path / "data" / "library.db")
    service = LibraryService(repo, output_dir=output, temp_dir=tmp_path / "temp")
    try:
        book = repo.create_book(BookCreate(
            title="Book", source_path=str(source), source_type=SourceType.TXT,
            playback_path=str(book_dir / "Book.m4b"),
        ))
        locked = book_dir if locked_artifact == "book" else source
        rename = os.rename
        replace = os.replace

        def fail_when_locked(operation):
            def move(src, dst, *args, **kwargs):
                if os.fspath(src) == str(locked):
                    raise PermissionError("File is in use")
                return operation(src, dst, *args, **kwargs)
            return move

        monkeypatch.setattr(os, "rename", fail_when_locked(rename))
        monkeypatch.setattr(os, "replace", fail_when_locked(replace))
        response = _make_client_with_controller(service).delete(f"/api/library/{book.id}")
        assert response.status_code == 409
        assert response.json()["detail"] == "Some files are in use. Stop playback and try again."
        assert all(path.read_bytes() == content for path, content in originals.items())
        assert not any((tmp_path / "temp" / "delete_staging").iterdir())
        assert repo.get_book(book.id) is not None
    finally:
        repo.close()


@pytest.mark.parametrize("first_audio", [False, True])
@pytest.mark.parametrize("source_exists", [False, True])
def test_cleanup_keeps_repair_inventory(tmp_path, first_audio, source_exists):
    output = tmp_path / "output"
    book_dir = output / "Interrupted"
    chapters_dir = book_dir / "chapters"
    chapters_dir.mkdir(parents=True)
    source = tmp_path / "source.txt"
    source.write_text("Original source", encoding="utf-8")
    source_chapters = [SimpleNamespace(number=n, title=f"Chapter {n}") for n in (1, 2)]
    durable = prepare_recovery_source(source, SimpleNamespace(output_dir=book_dir, chapters_dir=chapters_dir), source_chapters, emit_mp3=True)
    if not source_exists:
        durable.unlink()
    audio = chapters_dir / "01.mp3"
    if first_audio:
        audio.write_bytes(b"ID3audio")
    db = tmp_path / "data" / "library.db"
    repo = SQLiteRepository(db)
    try:
        book, chapters = repo.create_book_with_chapters(
            BookCreate(title="Interrupted", source_path=str(durable), source_type=SourceType.TXT, total_chapters=2),
            [ChapterCreate(book_id=0, chapter_number=n, title=f"Chapter {n}", mp3_path=str(audio) if n == 1 and first_audio else None) for n in (1, 2)],
        )
        audit = audit_storage_state(db, output, tmp_path / "temp", grace_seconds=0)
        assert audit["stale_chapter_rows"] == []
        report = prune_stale_records_and_files(db, output, tmp_path / "temp", aggressive=True, grace_seconds=0)
        kept_book, kept_chapters = repo.get_book_with_chapters(book.id)
        assert kept_book.id == book.id
        assert [c.id for c in kept_chapters] == [c.id for c in chapters]
        assert report["actions"]["deleted_book_rows"] == []
        assert (book_dir / ".orpheus-recovery.json").is_file()
        assert durable.exists() == source_exists
    finally:
        repo.close()


def test_patch_clears_explicit_null_but_preserves_omitted_metadata(tmp_path):
    repo = SQLiteRepository(tmp_path / "data" / "library.db")
    service = LibraryService(repo, output_dir=tmp_path / "output", temp_dir=tmp_path / "temp")
    try:
        book = repo.create_book(BookCreate(title="Title", author="Author", source_type=SourceType.TXT, source_path="source.txt"))
        repo.update_book(book.id, tags="tag")
        client = _make_client_with_controller(service)
        response = client.patch(f"/api/library/{book.id}", json={"favorite": True})
        assert response.status_code == 200
        assert response.json()["author"] == "Author"
        response = client.patch(f"/api/library/{book.id}", json={"author": None, "tags": None})
        assert response.status_code == 200
        assert response.json()["author"] is None
        assert response.json()["tags"] == ""
        unchanged = client.patch(f"/api/library/{book.id}", json={"title": None, "author": "Edited"})
        assert unchanged.status_code == 200
        assert unchanged.json()["title"] == "Title"
        assert unchanged.json()["author"] == "Edited"
    finally:
        repo.close()


def test_audio_route_serves_each_chapter_and_rejects_missing_or_external_files(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    first = output / "1.mp3"
    second = output / "2.mp3"
    external = tmp_path / "private.mp3"
    for path in (first, second, external):
        path.write_bytes(path.name.encode())
    repo = SQLiteRepository(tmp_path / "data" / "library.db")
    try:
        book, _ = repo.create_book_with_chapters(
            BookCreate(title="Playlist", source_path="source.txt", source_type=SourceType.TXT, playback_path=str(first)),
            [ChapterCreate(book_id=0, chapter_number=n, title=str(n), mp3_path=str(path)) for n, path in enumerate((first, second, external), 1)],
        )
        service = LibraryService(repo, output_dir=output, temp_dir=tmp_path / "temp")
        client = _make_client_with_controller(service)
        for n, path in enumerate((first, second), 1):
            response = client.get(f"/api/library/{book.id}/audio?chapter_number={n}")
            assert response.status_code == 200
            assert response.content == path.read_bytes()
            assert client.head(f"/api/library/{book.id}/audio?chapter_number={n}").status_code == 200
        for number in (3, 4):
            assert client.get(f"/api/library/{book.id}/audio?chapter_number={number}").status_code == 404
    finally:
        repo.close()


def test_partial_deletion_response_and_constrained_open_folder(tmp_path, monkeypatch):
    from modules.app.services import library_service

    root = tmp_path / "temp" / "delete_staging"
    handle = "a" * 32
    pending = root / handle
    pending.mkdir(parents=True)
    controller = SimpleNamespace(
        config=SimpleNamespace(temp_dir=tmp_path / "temp"),
        delete_book=lambda book_id: {"deleted": True, "files_pending": True, "path": handle},
    )
    opened = []
    monkeypatch.setattr(library_service, "_default_folder_opener", opened.append)
    client = _make_client_with_controller(controller)
    response = client.delete("/api/library/1")
    assert response.json() == {"book_id": 1, "deleted": True, "files_pending": True, "path": handle}
    opened_response = client.post("/api/maintenance/delete-staging/open-folder", json={"path": handle})
    assert opened_response.json() == {"folder_path": handle}
    assert opened == [pending.resolve()]
    assert client.post("/api/maintenance/delete-staging/open-folder", json={"path": str(tmp_path)}).status_code == 404


def test_startup_sweeps_old_delete_staging_but_keeps_recent_entries(tmp_path):
    root = tmp_path / "temp" / "delete_staging"
    stale = root / "old"
    recent = root / "recent"
    stale.mkdir(parents=True)
    recent.mkdir()
    artifact = stale / "audio.mp3"
    artifact.write_bytes(b"audio")
    (stale / ".committed").touch()
    (recent / ".committed").touch()
    old = time.time() - 3 * 86400
    for path in (artifact, stale / ".committed", stale):
        os.utime(path, (old, old))
    LibraryService(SimpleNamespace(), output_dir=tmp_path / "output", temp_dir=tmp_path / "temp")
    assert not stale.exists()
    assert recent.exists()


@pytest.mark.parametrize("title", ["", "   ", "\t\n"])
def test_metadata_patch_rejects_blank_title(title):
    update = Mock()
    with TestClient(create_app(controller=SimpleNamespace(update_book_metadata=update))) as client:
        response = client.patch("/api/library/1", json={"title": title})
    assert response.status_code == 422
    assert response.json()["detail"][0]["msg"] == "Value error, Title is required"
    update.assert_not_called()


@pytest.mark.parametrize("packaged", [False, True])
def test_v3_upgrade_is_immediately_playable(tmp_path, packaged):
    from tests.test_storage_migrations import _create_v3_schema, _insert_v3_book_with_chapter

    db = tmp_path / "data" / "library.db"
    db.parent.mkdir()
    _create_v3_schema(db)
    book_dir = tmp_path / "output" / "Original directory"
    chapter = book_dir / "chapters" / "01.mp3"
    chapter.parent.mkdir(parents=True)
    chapter.write_bytes(b"chapter audio")
    playback = book_dir / "Original.m4b" if packaged else chapter
    playback.write_bytes(b"playable audio")
    _insert_v3_book_with_chapter(db, title="Edited title", mp3_path=str(chapter))
    repo = SQLiteRepository(db)
    try:
        service = LibraryService(repo, output_dir=tmp_path / "output", temp_dir=tmp_path / "temp")
        client = _make_client_with_controller(service)
        response = client.get("/api/library/1")
        assert response.status_code == 200
        assert response.json()["playback_status"] == ("packaged" if packaged else "chapter_audio")
        assert response.json()["playback_url"] == "/api/library/1/audio"
        audio = client.get(response.json()["playback_url"])
        assert audio.status_code == 200
        assert audio.content == playback.read_bytes()
    finally:
        repo.close()


@pytest.mark.parametrize("first_audio", [False, True])
def test_cleanup_preserves_renamed_partial_book_and_chapter_identities(tmp_path, first_audio):
    output = tmp_path / "output"
    book_dir = output / "Interrupted"
    chapters_dir = book_dir / "chapters"
    chapters_dir.mkdir(parents=True)
    source = tmp_path / "source.txt"
    source.write_bytes(b"original source")
    inventory_chapters = [SimpleNamespace(number=n, title=f"Chapter {n}") for n in (1, 2)]
    durable = prepare_recovery_source(
        source, SimpleNamespace(output_dir=book_dir, chapters_dir=chapters_dir),
        inventory_chapters, emit_mp3=True,
    )
    audio = chapters_dir / "01_Chapter 1.mp3"
    if first_audio:
        audio.write_bytes(b"completed chapter")
    db = tmp_path / "data" / "library.db"
    repo = SQLiteRepository(db)
    try:
        book, chapters = repo.create_book_with_chapters(
            BookCreate(title="Interrupted", source_path=str(durable), source_type=SourceType.TXT, total_chapters=2),
            [ChapterCreate(book_id=0, chapter_number=n, title=f"Chapter {n}", mp3_path=str(audio) if n == 1 and first_audio else None) for n in (1, 2)],
        )
        moved = output / "Renamed"
        book_dir.rename(moved)
        report = prune_stale_records_and_files(db, output, tmp_path / "temp", aggressive=True, grace_seconds=0)
        detail = repo.get_book_with_chapters(book.id)
        assert detail is not None
        assert [c.id for c in detail[1]] == [c.id for c in chapters]
        assert [c.chapter_number for c in detail[1]] == [1, 2]
        assert report["actions"]["deleted_chapter_rows"] == []
        assert (moved / ".orpheus-recovery.json").is_file()
        assert (moved / ".orpheus-source" / durable.name).read_bytes() == b"original source"
        moved.rename(book_dir)
        assert repo.get_book_with_chapters(book.id)[1] == chapters
    finally:
        repo.close()


def test_legacy_playback_does_not_guess_a_directory_from_the_title(tmp_path):
    from tests.test_storage_migrations import _create_v3_schema, _insert_v3_book_with_chapter

    db = tmp_path / "data" / "library.db"
    db.parent.mkdir()
    _create_v3_schema(db)
    _insert_v3_book_with_chapter(db, title="Same title", mp3_path=None)
    unrelated = tmp_path / "output" / "Same title" / "audio.m4b"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"another book")
    repo = SQLiteRepository(db)
    try:
        service = LibraryService(repo, output_dir=tmp_path / "output", temp_dir=tmp_path / "temp")
        client = _make_client_with_controller(service)
        assert client.get("/api/library/1").json()["playback_url"] is None
        assert client.get("/api/library/1/audio").status_code == 404
    finally:
        repo.close()
