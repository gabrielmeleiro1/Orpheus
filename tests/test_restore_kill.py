from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from modules.storage import maintenance
from modules.storage.models import BookCreate, SourceType
from modules.storage.sqlite_repo import SQLiteRepository


_RESTORE = """
import os
from pathlib import Path
import sqlite3
import sys
from modules.storage import maintenance
from modules.storage.sqlite_repo import SQLiteRepository

db, backup, boundary = sys.argv[1:]
active = False
connect = sqlite3.connect
class Connection(sqlite3.Connection):
    def execute(self, sql, *args, **kwargs):
        result = super().execute(sql, *args, **kwargs)
        if active and self.path == Path(db):
            if sql == 'PRAGMA wal_checkpoint(TRUNCATE)':
                pause('checkpoint')
            elif sql == 'PRAGMA journal_mode=DELETE':
                pause('journal_mode')
        if active and sql.startswith(('UPDATE library_metadata', 'INSERT INTO library_metadata')):
            self.epoch_updated = True
        return result
    def commit(self):
        result = super().commit()
        if active and getattr(self, 'epoch_updated', False):
            pause('epoch_commit')
        return result
    def close(self):
        result = super().close()
        if active and self.path == Path(db):
            pause('close')
        return result
def connection(path, *args, **kwargs):
    kwargs['factory'] = Connection
    result = connect(path, *args, **kwargs)
    result.path = Path(path)
    return result
sqlite3.connect = connection
repo = SQLiteRepository(db)
def pause(name):
    if name == boundary:
        print(name, flush=True)
        os.read(sys.stdin.fileno(), 1)
        raise AssertionError('Parent did not kill the restore process')

migrate = maintenance.run_migrations
calls = 0
def migration(conn):
    global calls
    calls += 1
    if calls == 1:
        pause('staging_copy')
    if calls == 2 and boundary.startswith('rollback_'):
        raise sqlite3.OperationalError('Cannot reopen restored database')
    if calls == 3:
        pause('rollback_reopen')
    result = migrate(conn)
    if calls == 2:
        pause('reopen')
    return result
maintenance.run_migrations = migration
replace = Path.replace
publications = 0
def publish(path, target):
    global publications
    result = replace(path, target)
    if Path(target) == repo.db_path:
        publications += 1
        pause('publication' if publications == 1 else 'rollback_publication')
    return result
Path.replace = publish
unlink = Path.unlink
def remove(path, *args, **kwargs):
    result = unlink(path, *args, **kwargs)
    for suffix, name in [('-wal', 'wal_unlink'), ('-shm', 'shm_unlink'), ('.quarantine.json', 'status_unlink')]:
        if path == repo.db_path.with_name(repo.db_path.name + suffix):
            pause(name)
    return result
Path.unlink = remove
owner_only = maintenance.owner_only
def permissions(path):
    owner_only(path)
    if '_safety_' in path.name:
        pause('safety_backup')
maintenance.owner_only = permissions
copyfile = maintenance.copyfile
def copy(source, target):
    if boundary == 'rollback_copy':
        Path(target).write_bytes(Path(source).read_bytes()[:100])
        pause('rollback_copy')
    return copyfile(source, target)
maintenance.copyfile = copy
active = True
maintenance.restore_database_backup(repo, backup)
raise AssertionError('Restore did not reach the boundary')
"""

_RECOVER = """
import json
from pathlib import Path
import sys
from modules.app.config import AppConfig
from modules.app.controller import AppController

db = Path(sys.argv[1])
controller = AppController(AppConfig(
    db_path=db, data_dir=db.parent, output_dir=db.parent / 'output',
    temp_dir=db.parent / 'temp', worker_pool_reuse_enabled=False,
))
repo = controller.repository
with repo._lock:
    result = {
        'titles': [row[0] for row in repo._conn.execute('SELECT title FROM books')],
        'integrity': [row[0] for row in repo._conn.execute('PRAGMA integrity_check')],
        'foreign_keys': list(repo._conn.execute('PRAGMA foreign_key_check')),
        'epoch': repo.get_library_identity()['library_epoch'],
        'quarantine': repo.quarantine,
        'staging': sorted(p.name for p in (db.parent / f'.{db.name}.restore-staging').glob('*')),
    }
repo.close()
print(json.dumps(result), flush=True)
"""


def _kill_restore(db, backup, boundary):
    with ThreadPoolExecutor(max_workers=1) as reader, subprocess.Popen(
        [sys.executable, "-c", _RESTORE, str(db), backup.name, boundary],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    ) as child:
        try:
            signal = reader.submit(child.stdout.readline).result(timeout=30)
            assert signal.strip() == boundary
            child.kill()
            child.communicate(timeout=10)
            assert child.returncode != 0
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=10)


def _recover(db):
    result = subprocess.run(
        [sys.executable, "-c", _RECOVER, str(db)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("quarantined", [False, True])
@pytest.mark.parametrize("boundary", [
    "staging_copy", "epoch_commit", "safety_backup", "checkpoint", "journal_mode",
    "close", "wal_unlink", "shm_unlink", "publication", "reopen", "status_unlink",
    "rollback_copy", "rollback_publication", "rollback_reopen",
])
def test_restart_cleans_interrupted_restore_without_changing_published_library(tmp_path, boundary, quarantined):
    if boundary == "status_unlink" and not quarantined:
        pytest.skip("A healthy library has no quarantine status to unlink")
    db = tmp_path / "library.db"
    repo = SQLiteRepository(db)
    try:
        book = repo.create_book(BookCreate(
            title="Saved", author="Author", source_path="source.txt",
            source_type=SourceType.TXT, total_chapters=0,
        ))
        backup = maintenance.backup_database(db)
        saved_epoch = repo.get_library_identity()["library_epoch"]
        repo.update_book(book.id, title="Current")
    finally:
        repo.close()
    if quarantined:
        db.write_bytes(b"corrupt library")
    repo = SQLiteRepository(db)
    try:
        old_epoch = repo.get_library_identity()["library_epoch"]
        status = repo.quarantine
        if quarantined:
            archived = Path(status["path"])
            archived_bytes = archived.read_bytes()
    finally:
        repo.close()

    _kill_restore(db, backup, boundary)
    recovered = _recover(db)
    assert recovered["integrity"] == ["ok"]
    assert recovered["foreign_keys"] == []
    assert recovered["staging"] == []
    restored = boundary in {"publication", "reopen", "status_unlink"}
    old_titles = [] if quarantined else ["Current"]
    if not restored:
        assert recovered["titles"] == old_titles
        assert recovered["epoch"] == old_epoch
        if quarantined:
            assert all(recovered["quarantine"][key] == value for key, value in status.items())
        else:
            assert recovered["quarantine"] is None
    else:
        assert recovered["titles"] == ["Saved"]
        assert recovered["epoch"] not in {old_epoch, saved_epoch}
        assert recovered["quarantine"] is None
    if boundary not in {"staging_copy", "epoch_commit"}:
        safety = next(backup.parent.glob("*safety*.db"))
        with closing(sqlite3.connect(safety)) as conn:
            assert [row[0] for row in conn.execute("SELECT title FROM books")] == old_titles
    if quarantined:
        assert archived.read_bytes() == archived_bytes
    assert _recover(db) == recovered
