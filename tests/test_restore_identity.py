from contextlib import closing
import json
import sqlite3
import threading

import pytest

from modules.storage.maintenance import backup_database, restore_database_backup
from modules.storage.models import BookCreate, SourceType
from modules.storage.sqlite_repo import LibraryReplacedError, SQLiteRepository


def test_restore_repairs_missing_library_epoch_before_publishing(tmp_path, monkeypatch):
    repo = SQLiteRepository(tmp_path / "data" / "library.db")
    try:
        book = repo.create_book(BookCreate(
            title="Saved", source_path="source.txt", source_type=SourceType.TXT,
        ))
        original = repo.get_library_identity()
        backup = backup_database(repo.db_path)
        with closing(sqlite3.connect(backup)) as conn, conn:
            conn.execute("DELETE FROM library_metadata WHERE key='library_epoch'")
        replace = type(backup).replace

        def check_identity_before_publish(path, target):
            if target == repo.db_path:
                with closing(sqlite3.connect(path)) as conn:
                    identity = dict(conn.execute("SELECT key, value FROM library_metadata"))
                assert identity.get("library_epoch"), "published database lacks library_epoch"
                assert identity["library_id"] == original["library_id"]
            return replace(path, target)

        monkeypatch.setattr(type(backup), "replace", check_identity_before_publish)
        restore_database_backup(repo, backup.name)
        identity = repo.get_library_identity()
        assert identity["library_epoch"] != original["library_epoch"]
        with repo.library_snapshot(identity["library_epoch"], mutation=True):
            repo.update_book(book.id, title="Restored")
        assert repo.get_book(book.id).title == "Restored"
    finally:
        repo.close()


@pytest.mark.parametrize("missing_id", [True, False])
def test_restore_creates_library_id_only_when_absent_or_empty(tmp_path, missing_id):
    repo = SQLiteRepository(tmp_path / "data" / "library.db")
    try:
        backup = backup_database(repo.db_path)
        with closing(sqlite3.connect(backup)) as conn, conn:
            if missing_id:
                conn.execute("DELETE FROM library_metadata WHERE key='library_id'")
            else:
                conn.execute("UPDATE library_metadata SET value='' WHERE key='library_id'")
        restore_database_backup(repo, backup.name)
        assert repo.get_library_identity().get("library_id"), "restored library lacks a non-empty library_id"
    finally:
        repo.close()


def test_restore_rejects_ambiguous_library_identity_before_touching_live_library(tmp_path):
    repo = SQLiteRepository(tmp_path / "data" / "library.db")
    try:
        original = repo.get_library_identity()
        backup = backup_database(repo.db_path)
        with closing(sqlite3.connect(backup)) as conn, conn:
            conn.execute("ALTER TABLE library_metadata RENAME TO old_metadata")
            conn.execute("CREATE TABLE library_metadata (key TEXT, value TEXT)")
            conn.execute("INSERT INTO library_metadata SELECT * FROM old_metadata")
            conn.execute("INSERT INTO library_metadata VALUES ('library_id', 'another-library')")
            conn.execute("DROP TABLE old_metadata")
        with pytest.raises(ValueError, match="identity|schema"):
            restore_database_backup(repo, backup.name)
        assert repo.get_library_identity() == original
        assert repo.library_generation == 0
        assert not list(backup.parent.glob("*_safety_*.db"))
    finally:
        repo.close()


@pytest.mark.parametrize("identity", [None, ""])
def test_restore_without_identity_keeps_the_live_library_id_and_stays_movable(tmp_path, identity):
    from modules.storage.managed_roots import STORAGE_OWNER_NAME

    original = tmp_path / "original"
    repo = SQLiteRepository(original / "data" / "library.db")
    try:
        library_id = repo.get_library_identity()["library_id"]
        backup = backup_database(repo.db_path)
        with closing(sqlite3.connect(backup)) as conn, conn:
            if identity is None:
                conn.execute("DELETE FROM library_metadata WHERE key='library_id'")
            else:
                conn.execute("UPDATE library_metadata SET value=? WHERE key='library_id'", (identity,))
        restore_database_backup(repo, backup.name)
        assert repo.get_library_identity()["library_id"] == library_id
        for root in (repo.db_path.parent / "sources", repo._output_dir, repo._temp_dir):
            marker = json.loads((root / STORAGE_OWNER_NAME).read_text())
            assert marker["library_id"] == library_id
    finally:
        repo.close()
    moved = tmp_path / "moved"
    original.rename(moved)
    repo = SQLiteRepository(moved / "data" / "library.db")
    try:
        assert repo.get_library_identity()["library_id"] == library_id
    finally:
        repo.close()


def test_restore_with_a_different_library_id_updates_storage_markers(tmp_path):
    from modules.storage.managed_roots import STORAGE_OWNER_NAME

    original, moved = tmp_path / "original", tmp_path / "moved"
    repo = SQLiteRepository(original / "data" / "library.db")
    try:
        restored_id = repo.get_library_identity()["library_id"]
        backup = backup_database(repo.db_path)
        with repo._connection() as conn:
            conn.execute("UPDATE library_metadata SET value='replacement' WHERE key='library_id'")
        repo._claim_storage("replacement")
        restore_database_backup(repo, backup.name)
        for root in (repo.db_path.parent / "sources", repo._output_dir, repo._temp_dir):
            record = json.loads((root / STORAGE_OWNER_NAME).read_text())
            assert record["library_id"] == restored_id, "storage marker retains the replaced identity"
    finally:
        repo.close()
    original.rename(moved)
    with closing(SQLiteRepository(moved / "data" / "library.db")) as repo:
        assert repo.get_library_identity()["library_id"] == restored_id


def _interrupt_marker_refresh(monkeypatch):
    from modules.storage import managed_roots

    publish = managed_roots.atomic_write_json
    published = []

    def fail_after_first_write(marker, record):
        if published:
            raise OSError("marker disk full")
        publish(marker, record)
        published.append(marker)

    monkeypatch.setattr(managed_roots, "atomic_write_json", fail_after_first_write)


def test_restore_advances_generation_even_when_marker_refresh_fails(tmp_path, monkeypatch, caplog):
    database = tmp_path / "data" / "library.db"
    with closing(SQLiteRepository(database)) as repo:
        book = repo.create_book(BookCreate(title="Saved", source_path="source.txt", source_type=SourceType.TXT))
        restored_id = repo.get_library_identity()["library_id"]
        backup = backup_database(database)
        repo.update_book(book.id, title="Live")
        with repo._connection() as conn:
            conn.execute("UPDATE library_metadata SET value='replacement' WHERE key='library_id'")
        repo._claim_storage("replacement")
        epoch = repo.get_library_identity()["library_epoch"]
        generation = repo.library_generation
        error = safety = None
        with monkeypatch.context() as patch:
            _interrupt_marker_refresh(patch)
            try:
                safety = restore_database_backup(repo, backup.name)
            except OSError as caught:
                error = caught
        assert repo.library_generation == generation + 1, "published restore did not advance library generation"
        assert error is None, f"published restore rejected marker refresh failure: {error}"
        assert safety.is_file()
        assert repo.get_book(book.id).title == "Saved"
        assert repo.get_library_identity()["library_epoch"] != epoch
        assert "marker" in caplog.text.lower()
    with closing(SQLiteRepository(database)) as repo:
        from modules.storage.managed_roots import STORAGE_OWNER_NAME

        for root in (database.parent / "sources", repo._output_dir, repo._temp_dir):
            assert json.loads((root / STORAGE_OWNER_NAME).read_text())["library_id"] == restored_id


def test_repair_is_fenced_after_restore_with_failed_marker_refresh(tmp_path, monkeypatch):
    from modules.app.config import AppConfig
    from modules.app.controller import AppController

    config = AppConfig(
        data_dir=tmp_path / "data", output_dir=tmp_path / "output", temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "library.db", worker_pool_reuse_enabled=False,
    )
    first = AppController(config)
    second = AppController(config, repository=first.repository)
    repo, service = first.repository, first._conversion_service
    try:
        book = repo.create_book(BookCreate(title="Saved", source_path="source.txt", source_type=SourceType.TXT))
        backup = backup_database(repo.db_path)
        repo.update_book(book.id, title="Live")
        with repo._connection() as conn:
            conn.execute("UPDATE library_metadata SET value='replacement' WHERE key='library_id'")
        repo._claim_storage("replacement")
        epoch = repo.get_library_identity()["library_epoch"]
        launched, errors = [], []
        monkeypatch.setattr(service._runtime_options_resolver, "with_setup_defaults", lambda options: options)
        monkeypatch.setattr(service, "_start_pipeline_job", lambda **kwargs: launched.append(kwargs["job"]))

        def restore_during_validation(**kwargs):
            def restore():
                try:
                    second.restore_database_backup(backup.name)
                except Exception as error:
                    errors.append(error)

            with monkeypatch.context() as patch:
                _interrupt_marker_refresh(patch)
                thread = threading.Thread(target=restore, daemon=True)
                thread.start()
                thread.join(3)
                assert not thread.is_alive(), "restore could not finish during repair validation"

        monkeypatch.setattr(service, "_validate_requested_tts_runtime", restore_during_validation)
        with pytest.raises(LibraryReplacedError):
            first.repair_conversion(book.id, expected_epoch=epoch)
        assert not launched
        assert not errors
        assert repo.get_book(book.id).title == "Saved"
    finally:
        job = service.get_active_job()
        if job:
            service._job_coordinator.release_reservation(job)
        repo.close()
