from contextlib import contextmanager
import sqlite3

import pytest

from modules.app.services import library_service
from modules.app.services.library_service import LibraryService
from modules.storage import deletion
from modules.storage.models import BookCreate, ChapterCreate, SourceType
from modules.storage.sqlite_repo import SQLiteRepository


def _service(repo, root):
    return LibraryService(repo, output_dir=root / "output", temp_dir=root / "temp", storage_root=root)


def _create_book(root):
    source = root / "data" / "sources" / "book.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source text")
    audio = root / "output" / "Book" / "chapters" / "01.mp3"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"chapter audio")
    (audio.parent / "02.mp3").write_bytes(b"second chapter audio")
    repo = SQLiteRepository(root / "data" / "library.db")
    book, _ = repo.create_book_with_chapters(
        BookCreate(title="Book", source_path=str(source), source_type=SourceType.TXT,
                   playback_path=str(audio)),
        [ChapterCreate(book_id=0, chapter_number=1, title="First", mp3_path=str(audio))],
    )
    return repo, _service(repo, root), book


def _record_deletion(monkeypatch, repo, events, failure=None):
    connection = repo._connection
    delete = repo.delete_book
    sync_file = deletion.sync_file
    sync_directory = deletion.sync_directory
    remove = library_service.remove_staged_artifacts

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def execute(self, sql, *args):
            if sql.upper() == "PRAGMA WAL_CHECKPOINT(FULL)":
                events.append("checkpoint")
                if failure == "checkpoint_error":
                    raise sqlite3.OperationalError("disk I/O error")
                if failure == "checkpoint_busy":
                    return self.conn.execute("SELECT 1, 4, 2")
                result = self.conn.execute(sql, *args).fetchone()
                assert result[0] == 0 and result[1] == result[2]
                events.append("durable")
                return self.conn.execute("SELECT ?, ?, ?", tuple(result))
            return self.conn.execute(sql, *args)

    @contextmanager
    def recorded_connection():
        with connection() as conn:
            yield Connection(conn)

    def committed(book_id):
        result = delete(book_id)
        events.append("commit")
        return result

    def synced_file(path):
        if path.name == ".committed":
            events.append("marker_file")
            if failure == "marker_file":
                raise OSError("file sync failed")
        return sync_file(path)

    def synced_directory(path):
        if (path / ".committed").exists():
            events.append("marker_directory")
            if failure == "marker_directory":
                raise OSError("directory sync failed")
        return sync_directory(path)

    def removed(artifacts):
        events.append("remove")
        return remove(artifacts)

    monkeypatch.setattr(repo, "_connection", recorded_connection)
    monkeypatch.setattr(repo, "delete_book", committed)
    monkeypatch.setattr(deletion, "sync_file", synced_file)
    monkeypatch.setattr(deletion, "sync_directory", synced_directory)
    monkeypatch.setattr(library_service, "remove_staged_artifacts", removed)


def test_deletion_syncs_sql_and_marker_before_removing_files(tmp_path, monkeypatch):
    repo, service, book = _create_book(tmp_path)
    try:
        events = []
        _record_deletion(monkeypatch, repo, events)
        assert service.delete_book(book.id) is True
        assert events == ["commit", "checkpoint", "durable", "marker_file", "marker_directory", "remove"]
        assert repo._conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    finally:
        repo.close()


@pytest.mark.parametrize("failure", ["checkpoint_busy", "checkpoint_error", "marker_file", "marker_directory"])
def test_failed_durability_barrier_retains_files_until_recovery(tmp_path, monkeypatch, failure):
    repo, service, book = _create_book(tmp_path)
    try:
        events = []
        with monkeypatch.context() as patch:
            _record_deletion(patch, repo, events, failure)
            result = service.delete_book(book.id)
            assert isinstance(result, dict) and result["files_pending"]
            assert "remove" not in events
            assert repo.get_book(book.id) is None
            entry = next((tmp_path / "temp" / "delete_staging").iterdir())
            _, artifacts = deletion.read_delete_transaction(entry, allowed_roots=(tmp_path,))
            assert all(staged.exists() for _, staged in artifacts)
            if failure.startswith("checkpoint"):
                assert not (entry / ".committed").exists()
            _service(repo, tmp_path)
            assert "remove" not in events
            assert all(staged.exists() for _, staged in artifacts)
        events.clear()
        _record_deletion(monkeypatch, repo, events)
        _service(repo, tmp_path)
        assert events == ["checkpoint", "durable", "marker_file", "marker_directory", "remove"]
        assert not entry.exists()
        assert all(not staged.exists() for _, staged in artifacts)
        _service(repo, tmp_path)
        assert events.count("remove") == 1
    finally:
        repo.close()


@pytest.mark.parametrize("locked_name", ["transaction.json", ".committed", ".preserve"])
def test_finishing_deletion_defers_locked_journal_cleanup(tmp_path, monkeypatch, locked_name):
    entry = tmp_path / "transaction"
    entry.mkdir()
    for name in ("transaction.json", ".committed", ".preserve"):
        (entry / name).write_text("", encoding="utf-8")
    unlink = type(entry).unlink

    def deny_locked_file(path, *args, **kwargs):
        if path == entry / locked_name:
            raise PermissionError("File is in use")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(type(entry), "unlink", deny_locked_file)
        assert deletion.finish_delete_transaction(entry, []) is False
    assert entry.exists()
    assert (entry / locked_name).exists()
    assert deletion.finish_delete_transaction(entry, []) is True
    assert not entry.exists()
