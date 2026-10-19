from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3

import pytest

from modules.app.services.library_service import LibraryService
from modules.storage.maintenance import backup_database, restore_database_backup
from modules.storage.migrations import FutureSchemaError, LATEST_SCHEMA_VERSION
from modules.storage.models import BookCreate, ChapterCreate, SourceType
from modules.storage.sqlite_repo import SQLiteRepository


@pytest.fixture
def repository(tmp_path):
    repo = SQLiteRepository(tmp_path / "data" / "library.db")
    try:
        yield repo
    finally:
        repo.close()


def _service(repo, root):
    return LibraryService(
        repo, output_dir=root / "output", temp_dir=root / "temp", storage_root=root,
    )


def _saved_book(repo, root):
    source = root / "data" / "sources" / "source.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source text")
    audio = root / "output" / "saved" / "book.m4b"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"audio bytes")
    book, _ = repo.create_book_with_chapters(
        BookCreate(title="Saved", source_path=str(source), source_type=SourceType.TXT,
                   playback_path=str(audio), total_chapters=1),
        [ChapterCreate(book_id=0, chapter_number=1, title="First", mp3_path=str(audio))],
    )
    return book, source, audio


def _journal(root, book_id, *, committed=False, preserved=False):
    original = root / "output" / "saved.m4b"
    staged = original.parent / ".orpheus-delete-staging" / "saved" / "0-saved.m4b"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"staged audio")
    entry = root / "temp" / "delete_staging" / "saved"
    entry.mkdir(parents=True)
    # Persist the legacy on-disk shape independently of the current writer.
    (entry / "transaction.json").write_text(json.dumps({
        "book_id": book_id,
        "artifacts": [{"original": str(original), "staged": str(staged)}],
    }))
    if committed:
        (entry / ".committed").touch()
    if preserved:
        (entry / ".preserve").touch()
    return entry, original, staged


@pytest.mark.parametrize("row_exists,committed,preserved,replaced,outcome", [
    (True, False, False, False, "restore"),
    (False, False, False, False, "remove"),
    (True, True, False, False, "remove"),
    (False, True, False, False, "remove"),
    (False, False, True, False, "preserve"),
    (False, False, False, True, "preserve"),
    (True, False, False, True, "restore"),
    (False, True, True, True, "remove"),
])
def test_deletion_recovery_uses_rows_markers_and_replacement_evidence(
    tmp_path, repository, row_exists, committed, preserved, replaced, outcome,
):
    repo = repository
    book = repo.create_book(BookCreate(title="Saved", source_path="", source_type=SourceType.TXT))
    if not row_exists:
        repo.delete_book(book.id)
    if replaced:
        repo.mark_database_replaced()
    entry, original, staged = _journal(
        tmp_path, book.id, committed=committed, preserved=preserved,
    )
    # current behaviour: absent rows imply commit even for unmarked legacy journals.
    service = _service(repo, tmp_path)
    assert original.exists() is (outcome == "restore")
    assert staged.exists() is (outcome == "preserve")
    assert entry.exists() is (outcome == "preserve")
    assert (repo.get_book(book.id) is not None) is row_exists
    if outcome == "restore":
        assert original.read_bytes() == b"staged audio"
    if outcome == "preserve":
        assert (entry / ".preserve").is_file()
        assert staged.read_bytes() == b"staged audio"
    assert service.sweep_delete_staging() == 0
    reopened = SQLiteRepository(repo.db_path)
    try:
        _service(reopened, tmp_path)
        assert staged.exists() is (outcome == "preserve")
    finally:
        reopened.close()


def test_deletion_recovery_preserves_both_files_when_original_has_been_recreated(tmp_path, repository):
    book = repository.create_book(BookCreate(title="Saved", source_path="", source_type=SourceType.TXT))
    entry, original, staged = _journal(tmp_path, book.id)
    original.write_bytes(b"replacement audio")
    service = _service(repository, tmp_path)
    assert original.read_bytes() == b"replacement audio"
    assert staged.read_bytes() == b"staged audio"
    assert (entry / "transaction.json").is_file()
    assert service.sweep_delete_staging() == 0


@pytest.mark.parametrize("restore_blocked", [False, True])
def test_database_delete_failure_rolls_back_rows_and_retains_recoverable_files(
    tmp_path, repository, monkeypatch, restore_blocked,
):
    repo = repository
    book, source, audio = _saved_book(repo, tmp_path)
    service = _service(repo, tmp_path)
    with closing(sqlite3.connect(repo.db_path)) as conn, conn:
        conn.execute("""
            CREATE TRIGGER reject_book_delete BEFORE DELETE ON books
            BEGIN SELECT RAISE(ABORT, 'book is retained'); END
        """)
    replace = os.replace

    def replace_unless_restoring(src, dst):
        if ".orpheus-delete-staging" in Path(src).parts:
            raise PermissionError("restore destination is locked")
        return replace(src, dst)

    with monkeypatch.context() as patch:
        if restore_blocked:
            patch.setattr(os, "replace", replace_unless_restoring)
        with pytest.raises(sqlite3.IntegrityError, match="book is retained"):
            service.delete_book(book.id)
    assert repo.get_book(book.id) is not None
    assert len(repo.get_chapters(book.id)) == 1
    journals = list((tmp_path / "temp" / "delete_staging").glob("*/transaction.json"))
    assert len(journals) == int(restore_blocked)
    assert source.exists() is (not restore_blocked)
    assert audio.exists() is (not restore_blocked)
    if restore_blocked:
        assert not (journals[0].parent / ".committed").exists()
    _service(repo, tmp_path)
    assert source.read_bytes() == b"source text"
    assert audio.read_bytes() == b"audio bytes"
    assert not list((tmp_path / "temp" / "delete_staging").glob("*/transaction.json"))


def test_committed_delete_reports_pending_files_and_startup_finishes_removal(
    tmp_path, repository, monkeypatch,
):
    repo = repository
    book, source, audio = _saved_book(repo, tmp_path)
    service = _service(repo, tmp_path)
    unlink = Path.unlink

    def unlink_unless_staged(path, *args, **kwargs):
        if ".orpheus-delete-staging" in path.parts:
            raise PermissionError("staged file is locked")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", unlink_unless_staged)
        result = service.delete_book(book.id)
    entry = tmp_path / "temp" / "delete_staging" / result["path"]
    assert result == {"deleted": True, "files_pending": True, "path": entry.name}
    assert repo.get_book(book.id) is None
    assert repo.get_chapters(book.id) == []
    assert not source.exists()
    assert not audio.exists()
    assert (entry / ".committed").is_file()
    inventory = json.loads((entry / "transaction.json").read_text())["artifacts"]
    assert any(Path(item["staged"]).exists() for item in inventory)
    _service(repo, tmp_path)
    assert not entry.exists()
    assert all(not Path(item["staged"]).exists() for item in inventory)


def test_restore_preserves_unmarked_deletion_evidence_across_reopen(tmp_path, repository):
    repo = repository
    backup = backup_database(repo.db_path)
    entry, original, staged = _journal(tmp_path, 1)
    restore_database_backup(repo, backup.name)
    assert (entry / ".preserve").is_file()
    repo.close()
    reopened = SQLiteRepository(repo.db_path)
    try:
        assert reopened.database_replaced is False
        _service(reopened, tmp_path)
        assert staged.read_bytes() == b"staged audio"
        assert not original.exists()
        assert (entry / "transaction.json").is_file()
    finally:
        reopened.close()


def _quarantine_intent(db, phase):
    target = db.with_name(db.name + ".corrupt-saved")
    payload = {
        "version": 1, "phase": phase,
        "status": {"path": str(target), "created_at": "2026-01-01T00:00:00"},
        "files": [
            {"source": str(db) + suffix, "destination": str(target) + suffix,
             "required": suffix == ""}
            for suffix in ("", "-wal", "-shm")
        ],
    }
    intent = db.with_name(db.name + ".quarantine-intent.json")
    intent.write_text(json.dumps(payload))
    return intent, payload


@pytest.mark.parametrize("phase,moved", [
    ("moving", 0), ("moving", 1), ("moving", 2), ("moving", 3), ("complete", 3),
])
def test_quarantine_startup_finishes_recorded_moves_before_creating_empty_library(
    tmp_path, phase, moved,
):
    db = tmp_path / "library.db"
    intent, payload = _quarantine_intent(db, phase)
    contents = [b"unreadable database", b"saved wal", b"saved shm"]
    for index, (entry, content) in enumerate(zip(payload["files"], contents)):
        path = Path(entry["destination"] if index < moved else entry["source"])
        path.write_bytes(content)
    repo = SQLiteRepository(db)
    try:
        assert repo.count_books() == 0
        assert repo.database_replaced is True
        assert repo.quarantine == payload["status"]
        assert not intent.exists()
        for entry, content in zip(payload["files"], contents):
            assert Path(entry["destination"]).read_bytes() == content
        identity = repo.get_library_identity()
    finally:
        repo.close()
    reopened = SQLiteRepository(db)
    try:
        assert reopened.get_library_identity() == identity
        assert reopened.quarantine == payload["status"]
    finally:
        reopened.close()


@pytest.mark.parametrize("moved", [False, True])
def test_quarantine_rollback_reopens_original_rows_and_identity(tmp_path, moved):
    db = tmp_path / "library.db"
    repo = SQLiteRepository(db)
    try:
        book = repo.create_book(BookCreate(title="Saved", source_path="", source_type=SourceType.TXT))
        identity = repo.get_library_identity()
    finally:
        repo.close()
    intent, payload = _quarantine_intent(db, "rollback")
    if moved:
        db.replace(payload["files"][0]["destination"])
    reopened = SQLiteRepository(db)
    try:
        assert reopened.get_book(book.id).title == "Saved"
        assert reopened.get_library_identity() == identity
        assert reopened.quarantine is None
        assert reopened.database_replaced is False
        assert not intent.exists()
        assert all(not Path(entry["destination"]).exists() for entry in payload["files"])
    finally:
        reopened.close()


@pytest.mark.parametrize("failure", ["conflict", "missing", "future_intent"])
def test_unresolved_quarantine_intent_refuses_startup_and_preserves_evidence(tmp_path, failure):
    db = tmp_path / "library.db"
    intent, payload = _quarantine_intent(db, "moving")
    destination = Path(payload["files"][0]["destination"])
    if failure == "conflict":
        db.write_bytes(b"live database")
        destination.write_bytes(b"saved database")
    elif failure == "future_intent":
        payload["version"] = 2
        intent.write_text(json.dumps(payload))
        db.write_bytes(b"live database")
    evidence = {path: path.read_bytes() for path in tmp_path.iterdir()}
    with pytest.raises((OSError, ValueError)):
        SQLiteRepository(db)
    lock = db.with_name(db.name + ".lock")
    assert lock.is_file()
    assert {path: path.read_bytes() for path in tmp_path.iterdir()
            if path.name not in {lock.name, ".orpheus-database-locks"}} == evidence


@pytest.mark.parametrize("operation", ["open", "restore"])
def test_future_schema_is_refused_without_replacing_or_quarantining_library(
    tmp_path, repository, operation,
):
    repo = repository
    book, _, _ = _saved_book(repo, tmp_path)
    identity = repo.get_library_identity()
    target = backup_database(repo.db_path) if operation == "restore" else repo.db_path
    if operation == "open":
        repo.close()
    with closing(sqlite3.connect(target)) as conn, conn:
        conn.execute("INSERT INTO schema_migrations(version) VALUES (?)", (LATEST_SCHEMA_VERSION + 1,))
    if operation == "restore":
        with pytest.raises(ValueError, match="newer version of Orpheus"):
            restore_database_backup(repo, target.name)
        assert repo.get_library_identity() == identity
        assert repo.get_book(book.id).title == "Saved"
        assert repo.library_generation == 0
        assert repo.database_replaced is False
        assert not list(repo.db_path.parent.glob(".restore-*"))
        assert not list(target.parent.glob("*safety*"))
    else:
        with pytest.raises(FutureSchemaError, match="newer version of Orpheus"):
            SQLiteRepository(target)
    with closing(sqlite3.connect(target)) as conn:
        assert conn.execute("SELECT title FROM books WHERE id=?", (book.id,)).fetchone() == ("Saved",)
        assert dict(conn.execute("SELECT key, value FROM library_metadata WHERE key IN ('library_id', 'library_epoch')")) == identity
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (LATEST_SCHEMA_VERSION + 1,)
    assert not list(repo.db_path.parent.glob("*.corrupt-*"))
    assert not list(repo.db_path.parent.glob("*.quarantine*.json"))
