"""Database ownership across repository, maintenance and fixture entry points."""

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from modules.storage.maintenance import audit_storage_state, backup_database, restore_database_backup
from modules.storage.maintenance_paths import _connect
from modules.storage.sqlite_repo import SQLiteRepository
from tools.release_qualification.fixtures import (
    FixtureRoot, create_scenario_copy, generate_large_library,
)


@contextmanager
def paused_process(script, *args):
    process = subprocess.Popen(
        [sys.executable, "-c", script, *map(str, args)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    ready = queue.Queue()
    threading.Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True).start()
    try:
        assert ready.get(timeout=20) == "ready\n"
        yield process
    finally:
        process.kill()
        process.communicate(timeout=10)


LOCK_OWNER = """
import sys
from pathlib import Path
from apps.launchers.desktop_backend import _open_process_lock
handle = _open_process_lock(Path(sys.argv[1] + '.lock'), blocking=False)
print('ready', flush=True)
sys.stdin.read(1)
"""


def test_repository_refuses_owner_before_recovery_or_sqlite_open(tmp_path, monkeypatch):
    path = tmp_path / "library.db"
    def resume(self):
        pytest.fail("quarantine recovery ran before ownership")
    monkeypatch.setattr(SQLiteRepository, "_resume_quarantine", resume)
    with paused_process(LOCK_OWNER, path), pytest.raises(BlockingIOError):
        SQLiteRepository(path)


def test_maintenance_refuses_another_process_owner(tmp_path):
    path = tmp_path / "library.db"
    with paused_process(LOCK_OWNER, path), pytest.raises(BlockingIOError):
        connection = _connect(path)
        connection.close()
    assert not path.exists()


def test_maintenance_does_not_create_database_with_pending_quarantine(tmp_path):
    path = tmp_path / "library.db"
    path.with_suffix(".db.quarantine-intent.json").write_text('{}')
    with pytest.raises((OSError, RuntimeError)):
        connection = _connect(path)
        connection.close()
    assert not path.exists()


@pytest.mark.parametrize("error", [errno.EACCES, errno.EPERM, errno.EROFS])
def test_audit_reads_database_when_adjacent_lock_is_not_writable(tmp_path, monkeypatch, error):
    path = tmp_path / "library.db"
    SQLiteRepository(path).close()
    lockfile = Path(str(path) + ".lock")
    lockfile.unlink()
    open_file = Path.open

    def open_without_lock(self, *args, **kwargs):
        if self == lockfile:
            raise OSError(error, os.strerror(error))
        return open_file(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_without_lock)
    report = audit_storage_state(path)
    assert report["row_counts"] == {"books": 0, "chapters": 0}
    assert not lockfile.exists()
    with pytest.raises(OSError) as failure:
        SQLiteRepository(path)
    assert failure.value.errno == error
    with pytest.raises(OSError) as failure:
        _connect(path)
    assert failure.value.errno == error


@pytest.mark.parametrize("lock_error", [None, errno.EACCES, errno.EROFS])
def test_read_only_connection_rejects_writes(tmp_path, monkeypatch, lock_error):
    from modules.storage import maintenance_paths

    path = tmp_path / "library #1%.db"
    SQLiteRepository(path).close()
    if lock_error is not None:
        Path(str(path) + ".lock").unlink()
        def unavailable(_path):
            raise OSError(lock_error, os.strerror(lock_error))
        monkeypatch.setattr(maintenance_paths, "acquire_database", unavailable)
    connection = _connect(path, read_only=True)
    try:
        assert connection.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("CREATE TABLE forbidden (id INTEGER)")
    finally:
        connection.close()


@pytest.mark.parametrize("error", [errno.EACCES, errno.EPERM, errno.EROFS])
def test_read_only_connection_refuses_unreadable_existing_lock(tmp_path, monkeypatch, error):
    path = tmp_path / "library.db"
    SQLiteRepository(path).close()
    lockfile = Path(str(path) + ".lock")
    open_file = Path.open
    ownership_error = OSError(error, os.strerror(error))

    def open_without_lock(self, *args, **kwargs):
        if self == lockfile:
            raise ownership_error
        return open_file(self, *args, **kwargs)

    with paused_process(LOCK_OWNER, path):
        monkeypatch.setattr(Path, "open", open_without_lock)
        with pytest.raises(OSError) as failure:
            connection = _connect(path, read_only=True)
            connection.close()
        assert failure.value is ownership_error


@pytest.mark.parametrize("error", [errno.EACCES, errno.EPERM, errno.EROFS])
def test_read_only_connection_without_lock_refuses_pending_quarantine(tmp_path, monkeypatch, error):
    path = tmp_path / "library.db"
    SQLiteRepository(path).close()
    lockfile = Path(str(path) + ".lock")
    lockfile.unlink()
    Path(str(path) + ".quarantine-intent.json").write_text('{}')
    open_file = Path.open

    def open_without_lock(self, *args, **kwargs):
        if self == lockfile:
            raise OSError(error, os.strerror(error))
        return open_file(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_without_lock)
    with pytest.raises(RuntimeError, match="Database quarantine recovery is pending"):
        connection = _connect(path, read_only=True)
        connection.close()


@pytest.mark.parametrize("error", [BlockingIOError(errno.EACCES, "Locked"), OSError(errno.EIO, "I/O error")])
def test_read_only_connection_propagates_ownership_errors(tmp_path, monkeypatch, error):
    from modules.storage import maintenance_paths

    path = tmp_path / "library.db"
    SQLiteRepository(path).close()
    def unavailable(_path):
        raise error
    monkeypatch.setattr(maintenance_paths, "acquire_database", unavailable)
    with pytest.raises(type(error)) as failure:
        _connect(path, read_only=True)
    assert failure.value is error


@pytest.mark.parametrize("operation", ["generate", "copy"])
def test_qualification_refuses_another_process_owner(tmp_path, operation):
    fixtures = FixtureRoot.create(SimpleNamespace(path=tmp_path, release_id="ownership"))
    runtime = fixtures.create_runtime("source")
    SQLiteRepository(runtime.db_path).close()
    with paused_process(LOCK_OWNER, runtime.db_path), pytest.raises(BlockingIOError):
        if operation == "generate":
            generate_large_library(runtime, seed=1, count=1,
                                   expected_qualification_root=tmp_path, release_id="ownership")
        else:
            create_scenario_copy(runtime, fixtures.scenarios_root, "corrupt-db",
                                 expected_qualification_root=tmp_path, release_id="ownership",
                                 backups_root=fixtures.backups_root)
    assert runtime.db_path.exists()


def test_repository_keeps_resolved_path_when_alias_changes(tmp_path):
    target = tmp_path / "real.db"
    alias = tmp_path / "alias.db"
    alias.symlink_to(target)
    repo = SQLiteRepository(alias)
    try:
        alias.unlink()
        alias.symlink_to(tmp_path / "other.db")
        assert repo.db_path == target
        with pytest.raises(AttributeError):
            repo.db_path = alias
    finally:
        repo.close()


def test_restore_through_symlink_preserves_database_and_lock_identity(tmp_path):
    target = tmp_path / "real.db"
    alias = tmp_path / "alias.db"
    alias.symlink_to(target)
    repo = SQLiteRepository(alias)
    try:
        backup = backup_database(repo.db_path)
        restore_database_backup(repo, backup.name)
        assert alias.is_symlink()
        assert repo.db_path == target
        result = subprocess.run(
            [sys.executable, "-c", """
import sys
from pathlib import Path
from apps.launchers.desktop_backend import _open_process_lock
try:
    handle = _open_process_lock(Path(str(Path(sys.argv[1]).resolve()) + '.lock'), blocking=False)
except BlockingIOError:
    sys.exit(0)
sys.exit(1)
""", str(alias)], capture_output=True, text=True, timeout=20,
        )
        assert result.returncode == 0, result.stderr
    finally:
        repo.close()


def _can_acquire_in_subprocess(path, *, env=None):
    result = subprocess.run(
        [sys.executable, "-c", """
import sys
from modules.storage.db_ownership import acquire_database
try:
    owner = acquire_database(sys.argv[1])
except BlockingIOError:
    sys.exit(73)
owner.close()
""", str(path)], capture_output=True, text=True, timeout=20, env=env,
    )
    assert result.returncode in (0, 73), result.stderr
    return result.returncode == 0


def test_launcher_repository_and_connection_retain_ownership_until_last_close(tmp_path):
    from modules.storage.db_ownership import acquire_database

    path = tmp_path / "library.db"
    launcher = acquire_database(path)
    repo = SQLiteRepository(path)
    other = SQLiteRepository(path)
    connection = _connect(path)
    try:
        launcher.close()
        repo.close()
        repo.close()
        assert not _can_acquire_in_subprocess(path)
        other.close()
        assert not _can_acquire_in_subprocess(path)
        connection.close()
        assert _can_acquire_in_subprocess(path)
        assert Path(str(path) + ".lock").exists()
    finally:
        launcher.close()
        repo.close()
        other.close()
        connection.close()


def test_failed_initialization_releases_ownership_without_waiting_for_collection(tmp_path, monkeypatch):
    path = tmp_path / "library.db"
    def fail(self):
        assert not _can_acquire_in_subprocess(path)
        raise ValueError("invalid recovery record")
    monkeypatch.setattr(SQLiteRepository, "_resume_quarantine", fail)
    with pytest.raises(ValueError) as error:
        SQLiteRepository(path)
    assert error.value.__traceback__ is not None
    assert _can_acquire_in_subprocess(path)


def test_discarded_repository_and_connection_release_ownership(tmp_path):
    import gc

    path = tmp_path / "library.db"
    repo = SQLiteRepository(path)
    assert not _can_acquire_in_subprocess(path)
    del repo
    gc.collect()
    assert _can_acquire_in_subprocess(path)
    connection = _connect(path)
    assert not _can_acquire_in_subprocess(path)
    del connection
    gc.collect()
    assert _can_acquire_in_subprocess(path)


def test_repositories_serialize_operations_across_instances(tmp_path):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError

    first = SQLiteRepository(tmp_path / "library.db")
    second = SQLiteRepository(first.db_path)
    entered = threading.Event()
    def count():
        entered.set()
        return second.count_books()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with first.library_snapshot():
                future = pool.submit(count)
                assert entered.wait(5)
                with pytest.raises(TimeoutError):
                    future.result(timeout=0.05)
            assert future.result(timeout=5) == 0
    finally:
        first.close()
        second.close()


@pytest.mark.skipif(not hasattr(__import__('os'), 'fork'), reason="Requires fork")
def test_forked_child_must_acquire_its_own_ownership(tmp_path):
    import os

    repo = SQLiteRepository(tmp_path / "library.db")
    child = os.fork()
    if child == 0:
        try:
            SQLiteRepository(repo.db_path)
        except BlockingIOError:
            os._exit(0)
        except BaseException:
            os._exit(2)
        os._exit(1)
    try:
        _, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        assert not _can_acquire_in_subprocess(repo.db_path)
    finally:
        repo.close()


def test_failed_connection_close_retains_process_ownership(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import sqlite3

    path = tmp_path / "library.db"
    connection = _connect(path)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(sqlite3.ProgrammingError):
                pool.submit(connection.close).result(timeout=5)
        assert not _can_acquire_in_subprocess(path)
    finally:
        connection.close()


def test_discarded_repository_closes_sqlite_before_releasing_ownership(tmp_path, monkeypatch):
    import gc
    import sqlite3
    from modules.storage import db_ownership

    closed = []
    release = db_ownership._release
    def observe(owner):
        try:
            connection.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            closed.append(True)
        else:
            closed.append(False)
        release(owner)
    monkeypatch.setattr(db_ownership, "_release", observe)
    repo = SQLiteRepository(tmp_path / "library.db")
    connection = repo._conn
    del repo
    gc.collect()
    try:
        assert closed == [True]
    finally:
        connection.close()


def test_pristine_backup_refuses_another_process_owning_destination(tmp_path):
    import hashlib
    from tools.release_qualification.fixtures import _preserve_pristine_backup

    fixtures = FixtureRoot.create(SimpleNamespace(path=tmp_path, release_id="ownership"))
    runtime = fixtures.create_runtime("source")
    SQLiteRepository(runtime.db_path).close()
    digest = hashlib.sha256(runtime.db_path.read_bytes()).hexdigest()
    destination = fixtures.backups_root / f"{digest}.db"
    with paused_process(LOCK_OWNER, destination), pytest.raises(BlockingIOError):
        _preserve_pristine_backup(runtime, fixtures.backups_root,
                                 expected_qualification_root=tmp_path, release_id="ownership")
    assert not destination.exists()


def test_hard_linked_database_name_cannot_share_ownership(tmp_path):
    from modules.storage.managed_roots import StorageOwnershipError

    repo = SQLiteRepository(tmp_path / "data" / "library.db")
    alias = repo.db_path.with_name("other.db")
    try:
        repo._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        try:
            os.link(repo.db_path, alias)
        except OSError as error:
            pytest.skip(f"Hard links are unavailable: {error}")
        with pytest.raises(StorageOwnershipError, match="belongs to"):
            other = SQLiteRepository(alias, output_dir=repo._output_dir, temp_dir=repo._temp_dir)
            other.close()
    finally:
        repo.close()


@pytest.mark.skipif(os.name == "nt", reason="Windows folds path case and NTFS cannot hold case-variant entries in one directory")
def test_case_variant_hard_links_on_case_sensitive_filesystems_are_refused(tmp_path, monkeypatch):
    from modules.storage.db_ownership import same_database_file
    from modules.storage.managed_roots import StorageOwnershipError

    database = tmp_path / "data" / "Library.db"
    repo = SQLiteRepository(database)
    alias = database.with_name("library.db")
    try:
        repo._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if alias.exists():
            listdir, samefile = os.listdir, os.path.samefile

            def list_case_variants(path):
                names = listdir(path)
                if Path(path) == database.parent:
                    names = list(dict.fromkeys([*names, database.name, alias.name]))
                return names

            def same_inode(first, second):
                if {Path(first), Path(second)} == {database, alias}:
                    return True
                return samefile(first, second)

            monkeypatch.setattr(os, "listdir", list_case_variants)
            monkeypatch.setattr(os.path, "samefile", same_inode)
        else:
            try:
                os.link(database, alias)
            except OSError as error:
                pytest.skip(f"Hard links are unavailable: {error}")
        assert not same_database_file(database, alias), "distinct case-variant entries share ownership"
        repo.close()
        with pytest.raises(StorageOwnershipError, match="belongs to"):
            other = SQLiteRepository(alias, output_dir=repo._output_dir, temp_dir=repo._temp_dir)
            other.close()
    finally:
        repo.close()


def test_case_variant_ancestor_hard_links_do_not_share_database_ownership(tmp_path):
    from modules.storage.db_ownership import same_database_file

    database = tmp_path / "DATA" / "library.db"
    alias = tmp_path / "data" / "library.db"
    database.parent.mkdir()
    database.touch()
    target = tmp_path / "another.db" if alias.exists() else alias
    target.parent.mkdir(exist_ok=True)
    try:
        os.link(database, target)
    except OSError as error:
        pytest.skip(f"Hard links are unavailable: {error}")
    assert database.stat().st_nlink == 2
    assert not same_database_file(database, alias), "case-variant ancestor hard links share ownership"
