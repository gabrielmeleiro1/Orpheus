from contextlib import closing
from pathlib import Path
import json
import os
import sqlite3
import time

import pytest

from modules.contracts import durable_files
from modules.app.config import AppConfig
from modules.app.controller import AppController
from modules.storage import maintenance
from modules.storage import restore_recovery
from modules.storage.models import BookCreate, SourceType
from modules.storage.sqlite_repo import SQLiteRepository


@pytest.fixture
def library(tmp_path):
    repo = SQLiteRepository(tmp_path / "library.db")
    book = repo.create_book(BookCreate(
        title="Saved", author="Author", source_path="source.txt",
        source_type=SourceType.TXT, total_chapters=0,
    ))
    backup = maintenance.backup_database(repo.db_path)
    repo.update_book(book.id, title="Current")
    yield repo, book, backup
    repo.close()


@pytest.mark.parametrize("rollback", [False, True])
def test_restore_syncs_contents_before_publication_and_directory_before_reopen(library, monkeypatch, rollback):
    repo, book, backup = library
    events = []
    replace = Path.replace
    copyfile = maintenance.copyfile
    migrate = maintenance.run_migrations
    calls = 0

    def sync_file(path):
        if path.name.startswith(".restore-"):
            events.append("file sync")

    def sync_directory(path):
        if path == repo.db_path.parent:
            events.append("directory sync")

    def publish(path, target):
        if Path(target) == repo.db_path:
            events.append("replace")
        return replace(path, target)

    def copy(source, target):
        result = copyfile(source, target)
        events.append("rollback copy")
        return result

    def reopen(conn):
        nonlocal calls
        calls += 1
        if calls > 1:
            events.append("reopen")
        if rollback and calls == 2:
            raise sqlite3.OperationalError("Cannot reopen restored database")
        return migrate(conn)

    # Support either direct imports or module-qualified shared helpers.
    for module in (durable_files, maintenance, restore_recovery):
        monkeypatch.setattr(module, "sync_file", sync_file, raising=False)
        monkeypatch.setattr(module, "sync_directory", sync_directory, raising=False)
    monkeypatch.setattr(Path, "replace", publish)
    monkeypatch.setattr(maintenance, "copyfile", copy)
    monkeypatch.setattr(maintenance, "run_migrations", reopen)
    if rollback:
        with pytest.raises(sqlite3.OperationalError, match="Cannot reopen"):
            maintenance.restore_database_backup(repo, backup.name)
    else:
        maintenance.restore_database_backup(repo, backup.name)

    expected = ["file sync", "replace", "directory sync", "reopen"]
    if rollback:
        expected += ["directory sync", "rollback copy", "file sync", "replace", "directory sync", "reopen", "directory sync"]
    assert events == expected
    assert repo.get_book(book.id).title == ("Current" if rollback else "Saved")


@pytest.mark.parametrize("entry", ["startup", "restore"])
def test_recovery_sweeps_only_old_private_restore_files(tmp_path, monkeypatch, entry):
    db = tmp_path / ".restore-livedata.db"
    repo = SQLiteRepository(db)
    backup = maintenance.backup_database(db)
    repo.close()
    staging_dir = tmp_path / f".{db.name}.restore-staging"
    staging_dir.mkdir(mode=0o700)
    stale = [staging_dir / (f".restore-{db.name}-abcdefgh.db" + suffix) for suffix in ("", "-wal", "-shm", "-journal")]
    retained = [staging_dir / name for name in (
        f".restore-{db.name}-recent12.db", ".restore-not-a-stage.db", ".restore-abcdefgh.txt",
        "library.db-wal", "library.db-shm",
    )]
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(exist_ok=True)
    retained.append(backup_dir / ".restore-abcdefgh.db")
    for path in stale + retained:
        path.write_bytes(b"keep or sweep")
    cutoff = time.time() + 1
    monkeypatch.setattr(maintenance, "_RESTORE_PROCESS_STARTED_AT", cutoff, raising=False)
    os.utime(retained[0], (cutoff + 5, cutoff + 5))
    directory = staging_dir / f".restore-{db.name}-director.db"
    directory.mkdir()
    linked = staging_dir / f".restore-{db.name}-symlink1.db"
    linked.symlink_to(retained[1])
    if entry == "startup":
        controller = AppController(AppConfig(
            db_path=db, data_dir=tmp_path, output_dir=tmp_path / "output",
            temp_dir=tmp_path / "temp", worker_pool_reuse_enabled=False,
        ))
        repo = controller.repository
    else:
        repo = SQLiteRepository(db)
        maintenance.restore_database_backup(repo, backup.name)
    try:
        assert not any(path.exists() for path in stale)
        assert all(path.read_bytes() == b"keep or sweep" for path in retained)
        assert linked.is_symlink()
        assert directory.is_dir()
        assert repo.db_path.exists()
    finally:
        repo.close()


def test_restore_records_epoch_before_publication_and_syncs_status_removal(library, monkeypatch):
    repo, _, backup = library
    status_path = repo.db_path.with_name(repo.db_path.name + ".quarantine.json")
    status_path.write_text(json.dumps({"path": "archive.db", "created_at": "earlier"}))
    replace, unlink = Path.replace, Path.unlink
    events = []

    def publish(path, target):
        if Path(target) == repo.db_path:
            with closing(sqlite3.connect(path)) as conn:
                epoch = conn.execute("SELECT value FROM library_metadata WHERE key='library_epoch'").fetchone()[0]
            assert json.loads(status_path.read_text()).get("restored_library_epoch") == epoch
            events.append("publication")
        return replace(path, target)

    def remove(path, *args, **kwargs):
        result = unlink(path, *args, **kwargs)
        if path == status_path:
            events.append("status removal")
        return result

    def sync_directory(path):
        events.append("directory sync")

    monkeypatch.setattr(Path, "replace", publish)
    monkeypatch.setattr(Path, "unlink", remove)
    monkeypatch.setattr(maintenance, "sync_directory", sync_directory, raising=False)
    maintenance.restore_database_backup(repo, backup.name)
    assert not status_path.exists()
    assert events[-2:] == ["status removal", "directory sync"]


@pytest.mark.parametrize("name", [".restore-other.db-abcdefgh.db", ".restore-abcdefgh.db", ".restore-library.db-abcdefgh.db"])
def test_restore_recovery_preserves_files_without_this_database_identity(library, monkeypatch, name):
    repo, _, _ = library
    foreign = repo.db_path.parent / name
    foreign.write_bytes(b"belongs elsewhere")
    monkeypatch.setattr(maintenance, "_RESTORE_PROCESS_STARTED_AT", time.time() + 1)

    maintenance.recover_restore_artifacts(repo)

    assert foreign.read_bytes() == b"belongs elsewhere"


def _independent_repository(path, monkeypatch):
    from modules.storage import sqlite_repo

    claim_sources = sqlite_repo.claim_sources_root
    roots = path.parent / "independent-storage"

    def claim(database, root, **kwargs):
        # The constructor derives sources from the shared database directory.
        if Path(database) == path:
            root = roots / "sources"
        return claim_sources(database, root, **kwargs)

    monkeypatch.setattr(sqlite_repo, "claim_sources_root", claim)
    return SQLiteRepository(path, output_dir=roots / "output", temp_dir=roots / "temp")


def test_restore_recovery_preserves_another_databases_active_stage(library, monkeypatch):
    repo, _, backup = library
    other = _independent_repository(repo.db_path.parent / "other.db", monkeypatch)
    other_book = other.create_book(BookCreate(
        title="Independent", source_path="other.txt", source_type=SourceType.TXT,
    ))
    assert other.db_path.parent == repo.db_path.parent
    sync_file = maintenance.sync_file
    stages = []

    def sync_stage(path):
        if path.name.startswith(".restore-"):
            stages.append(path)
            monkeypatch.setattr(maintenance, "_RESTORE_PROCESS_STARTED_AT", time.time() + 1)
            maintenance.recover_restore_artifacts(other)
            assert path.is_file()
        sync_file(path)

    monkeypatch.setattr(maintenance, "sync_file", sync_stage)
    try:
        maintenance.restore_database_backup(repo, backup.name)
        assert stages
        assert other.get_book(other_book.id).title == "Independent"
        assert other.count_books() == 1
    finally:
        other.close()


def test_restore_stages_inside_owner_only_database_directory(library, monkeypatch):
    repo, _, backup = library
    sync_file = maintenance.sync_file
    stages = []

    def inspect(path):
        stages.append(path)
        assert path.parent == repo.db_path.parent / f".{repo.db_path.name}.restore-staging"
        if os.name != "nt":
            assert path.parent.stat().st_mode & 0o777 == 0o700
        sync_file(path)

    monkeypatch.setattr(maintenance, "sync_file", inspect)
    maintenance.restore_database_backup(repo, backup.name)
    assert stages


def test_restore_recovery_preserves_independent_database_with_legacy_stage_name(library, monkeypatch):
    repo, _, _ = library
    other_path = repo.db_path.with_name(".restore-library.db-abcdefgh.db")
    other = _independent_repository(other_path, monkeypatch)
    other_book = other.create_book(BookCreate(
        title="Independent", source_path="other.txt", source_type=SourceType.TXT,
    ))
    other.close()
    monkeypatch.setattr(maintenance, "_RESTORE_PROCESS_STARTED_AT", time.time() + 1)
    maintenance.recover_restore_artifacts(repo)
    assert other_path.exists()
    reopened = _independent_repository(other_path, monkeypatch)
    try:
        assert reopened.get_book(other_book.id).title == "Independent"
        assert reopened.count_books() == 1
    finally:
        reopened.close()


def test_restore_recovery_requires_live_database_ownership(library, monkeypatch):
    repo, _, _ = library
    staging_dir = repo.db_path.parent / f".{repo.db_path.name}.restore-staging"
    staging_dir.mkdir(mode=0o700)
    stage = staging_dir / ".restore-library.db-abcdefgh.db"
    stage.write_bytes(b"retain")
    repo.close()
    monkeypatch.setattr(maintenance, "_RESTORE_PROCESS_STARTED_AT", time.time() + 1)
    maintenance.recover_restore_artifacts(repo)
    assert stage.read_bytes() == b"retain"


def test_restore_does_not_follow_linked_staging_directory(library, monkeypatch, tmp_path):
    repo, _, backup = library
    outside = tmp_path / "foreign"
    outside.mkdir()
    retained = outside / ".restore-library.db-abcdefgh.db"
    retained.write_bytes(b"retain")
    (repo.db_path.parent / f".{repo.db_path.name}.restore-staging").symlink_to(outside)
    monkeypatch.setattr(maintenance, "_RESTORE_PROCESS_STARTED_AT", time.time() + 1)

    maintenance.recover_restore_artifacts(repo)
    with pytest.raises(ValueError, match="staging directory"):
        maintenance.restore_database_backup(repo, backup.name)

    assert retained.read_bytes() == b"retain"
