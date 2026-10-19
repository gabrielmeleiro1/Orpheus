import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from modules.storage.maintenance import backup_database, restore_database_backup
from modules.storage.models import BookCreate, SourceType
from modules.storage.sqlite_repo import SQLiteRepository
from tests.test_backup_restore import controller as controller_fixture
from tests.test_web_api import _make_client_with_controller

controller = controller_fixture


@pytest.mark.parametrize("version", [0, 8])
def test_library_identity_survives_reopen_and_legacy_upgrade(tmp_path, version):
    path = tmp_path / "library.db"
    repo = SQLiteRepository(path)
    repo.create_book(BookCreate(title="Saved", source_path="source.txt", source_type=SourceType.TXT))
    repo.close()
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DROP TABLE IF EXISTS library_metadata")
        conn.execute("DELETE FROM schema_migrations")
        if version:
            conn.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
    repo = SQLiteRepository(path)
    identity = repo.get_library_identity()
    assert identity["library_id"] and identity["library_epoch"]
    assert repo.get_book(1).title == "Saved"
    repo.close()
    repo = SQLiteRepository(path)
    assert repo.get_library_identity() == identity
    repo.close()


@pytest.mark.parametrize("legacy", [False, True])
def test_restore_publishes_new_epoch_before_reopening(tmp_path, monkeypatch, legacy):
    repo = SQLiteRepository(tmp_path / "library.db")
    original = repo.get_library_identity()
    backup = backup_database(repo.db_path)
    if legacy:
        with closing(sqlite3.connect(backup)) as conn, conn:
            conn.execute("DROP TABLE library_metadata")
            conn.execute("DELETE FROM schema_migrations")
            conn.execute("INSERT INTO schema_migrations(version) VALUES (8)")
    replace = Path.replace
    published = []

    def capture_publish(path, target):
        if Path(target) == repo.db_path:
            with closing(sqlite3.connect(path)) as conn:
                published.append(dict(conn.execute("SELECT key, value FROM library_metadata WHERE key IN ('library_id', 'library_epoch')")))
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", capture_publish)
    restore_database_backup(repo, backup.name)
    restored = repo.get_library_identity()
    assert published == [restored]
    assert restored["library_epoch"] != original["library_epoch"]
    if not legacy:
        assert restored["library_id"] == original["library_id"]
    repo.close()
    repo = SQLiteRepository(repo.db_path)
    assert repo.get_library_identity() == restored
    repo.close()


def test_failed_restore_preserves_library_identity(tmp_path, monkeypatch):
    repo = SQLiteRepository(tmp_path / "library.db")
    identity = repo.get_library_identity()
    backup = backup_database(repo.db_path)
    replace = Path.replace

    def refuse_publish(path, target):
        if Path(target) == repo.db_path:
            raise PermissionError("locked")
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", refuse_publish)
    with pytest.raises(RuntimeError):
        restore_database_backup(repo, backup.name)
    assert repo.get_library_identity() == identity
    repo.close()


def test_library_envelopes_and_diagnostics_share_durable_identity(controller):
    book = controller.repository.create_book(BookCreate(title="Saved", source_path="source.txt", source_type=SourceType.TXT))
    identity = controller.repository.get_library_identity()
    client = _make_client_with_controller(controller)
    for url in ("/api/library", f"/api/library/{book.id}", "/api/maintenance/diagnostics"):
        response = client.get(url)
        assert response.status_code == 200
        assert {key: response.json()[key] for key in identity} == identity


def test_in_process_interruption_after_restore_publication_reopens_with_replacement_epoch(tmp_path, monkeypatch):
    repo = SQLiteRepository(tmp_path / "library.db")
    identity = repo.get_library_identity()
    backup = backup_database(repo.db_path)
    replace = Path.replace

    def interrupt_after_publish(path, target):
        result = replace(path, target)
        if Path(target) == repo.db_path:
            raise SystemExit("interrupted")
        return result

    monkeypatch.setattr(Path, "replace", interrupt_after_publish)
    with pytest.raises(SystemExit):
        restore_database_backup(repo, backup.name)
    monkeypatch.setattr(Path, "replace", replace)
    reopened = SQLiteRepository(repo.db_path)
    restored = reopened.get_library_identity()
    assert restored["library_id"] == identity["library_id"]
    assert restored["library_epoch"] != identity["library_epoch"]
    reopened.close()


def test_failed_restore_reopen_rolls_back_identity_with_book_data(tmp_path, monkeypatch):
    repo = SQLiteRepository(tmp_path / "library.db")
    identity = repo.get_library_identity()
    backup = backup_database(repo.db_path)
    connect = sqlite3.connect
    failed = False

    def fail_live_reopen(path, *args, **kwargs):
        nonlocal failed
        if path == repo.db_path and not failed:
            failed = True
            raise sqlite3.OperationalError("reopen failed")
        return connect(path, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", fail_live_reopen)
    with pytest.raises(sqlite3.OperationalError, match="reopen failed"):
        restore_database_backup(repo, backup.name)
    assert repo.get_library_identity() == identity
    repo.close()
