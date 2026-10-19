"""Ownership and recovery across actual process termination."""

from pathlib import Path
import subprocess
import sys

import pytest

from tests.test_database_ownership import paused_process


@pytest.mark.parametrize("boundary", ["quarantine-main", "restore-close"])
@pytest.mark.parametrize("opener", ["repository", "maintenance"])
def test_owner_excludes_openers_until_killed_and_recovery_is_repeatable(tmp_path, boundary, opener):
    database = tmp_path / "library.db"
    script = """
import sys
from pathlib import Path
from modules.storage.sqlite_repo import SQLiteRepository
from modules.storage.maintenance import backup_database, restore_database_backup
path, boundary = Path(sys.argv[1]), sys.argv[2]
def pause():
    print('ready', flush=True)
    sys.stdin.read(1)
if boundary == 'quarantine-main':
    path.write_bytes(b'original corrupt database')
    replace = Path.replace
    def move(source, target):
        result = replace(source, target)
        if source == path:
            pause()
        return result
    Path.replace = move
    SQLiteRepository(path)
else:
    repo = SQLiteRepository(path)
    backup = backup_database(path)
    unlink = Path.unlink
    def remove(source, *args, **kwargs):
        if source == Path(str(path) + '-wal'):
            pause()
        return unlink(source, *args, **kwargs)
    Path.unlink = remove
    restore_database_backup(repo, backup.name)
"""
    with paused_process(script, database, boundary):
        contender = subprocess.run(
            [sys.executable, "-c", """
import sys
import sqlite3
from modules.storage.sqlite_repo import SQLiteRepository
from modules.storage.maintenance_paths import _connect
def forbidden(*args, **kwargs):
    raise AssertionError('SQLite opened before ownership')
sqlite3.connect = forbidden
try:
    if sys.argv[2] == 'repository':
        SQLiteRepository(sys.argv[1])
    else:
        _connect(sys.argv[1])
except BlockingIOError:
    sys.exit(0)
""", str(database), opener], capture_output=True, text=True, timeout=20,
        )
    assert contender.returncode == 0, contender.stderr
    if boundary == "quarantine-main":
        maintenance = subprocess.run(
            [sys.executable, "-c", """
import sys
from modules.storage.maintenance_paths import _connect
try:
    _connect(sys.argv[1])
except RuntimeError as error:
    assert 'quarantine' in str(error)
    sys.exit(0)
sys.exit(1)
""", str(database)], capture_output=True, text=True, timeout=20,
        )
        assert maintenance.returncode == 0, maintenance.stderr
        assert not database.exists()
    identities = []
    for _ in range(2):
        recovery = subprocess.run(
            [sys.executable, "-c", """
import sys
from modules.storage.sqlite_repo import SQLiteRepository
repo = SQLiteRepository(sys.argv[1])
assert repo._conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
assert repo.count_books() == 0
print(repo.get_library_identity())
repo.close()
""", str(database)], capture_output=True, text=True, timeout=20,
        )
        assert recovery.returncode == 0, recovery.stderr
        identities.append(recovery.stdout)
    assert identities[0] == identities[1]
    archives = list(tmp_path.glob("library.db.corrupt-*"))
    if boundary == "quarantine-main":
        assert len(archives) == 1
        assert archives[0].read_bytes() == b"original corrupt database"
    else:
        assert not archives
    assert not Path(str(database) + ".quarantine-intent.json").exists()
