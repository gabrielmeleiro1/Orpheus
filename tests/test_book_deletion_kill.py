import hashlib
import json
import os
from queue import Queue
import subprocess
import sys
from threading import Thread

import pytest

from modules.storage.deletion import preserve_delete_transaction
from modules.storage.sqlite_repo import SQLiteRepository


_CHILD = r'''
import json
import os
from pathlib import Path
import runpy
import sys
from modules.app.services import library_service
from modules.storage import deletion
from modules.storage.sqlite_repo import SQLiteRepository

root, position = Path(sys.argv[1]), sys.argv[2]
helpers = runpy.run_path("tests/test_book_deletion_durability.py")

def pause(name):
    if name == position:
        print(name, flush=True)
        sys.stdin.buffer.read(1)
        raise AssertionError("boundary released without termination")

def rows(repo):
    row = repo._conn.execute("SELECT * FROM books WHERE id = 1").fetchone()
    return {"book": dict(row) if row else None,
            "chapters": [dict(row) for row in repo._conn.execute("SELECT * FROM chapters ORDER BY id")]}

if position == "recover":
    repo = SQLiteRepository(root / "data" / "library.db")
    helpers["_service"](repo, root)
    print(json.dumps({"row": repo.get_book(1) is not None, "rows": rows(repo),
                      "integrity": repo._conn.execute("PRAGMA integrity_check").fetchone()[0],
                      "foreign_keys": list(repo._conn.execute("PRAGMA foreign_key_check"))}), flush=True)
    repo.close()
    sys.exit(0)

repo, service, book = helpers["_create_book"](root)
(root / "expected.json").write_text(json.dumps(rows(repo)))
write = library_service.write_delete_transaction
def manifest(*args):
    write(*args)
    pause("manifest")
library_service.write_delete_transaction = manifest

replace = os.replace
moves = rollbacks = 0
def move(src, dst):
    global moves, rollbacks
    result = replace(src, dst)
    if ".orpheus-delete-staging" in Path(dst).parts:
        moves += 1
        pause(f"move_{moves}")
    elif ".orpheus-delete-staging" in Path(src).parts:
        rollbacks += 1
        pause(f"rollback_{rollbacks}")
    return result
os.replace = move

connection = repo._conn
class Connection:
    deleting = False
    def __getattr__(self, name):
        return getattr(connection, name)
    def execute(self, sql, *args):
        result = connection.execute(sql, *args)
        if sql.startswith("DELETE FROM books"):
            self.deleting = True
        if sql.upper() == "PRAGMA WAL_CHECKPOINT(FULL)":
            pause("checkpoint")
        return result
    def commit(self):
        if self.deleting:
            pause("before_commit")
        connection.commit()
        if self.deleting:
            self.deleting = False
            pause("after_commit")
repo._conn = Connection()

delete = repo.delete_book
def delete_book(book_id):
    if position.startswith("rollback_"):
        raise RuntimeError("SQL deletion refused")
    return delete(book_id)
repo.delete_book = delete_book

touch = Path.touch
def marker(path, *args, **kwargs):
    result = touch(path, *args, **kwargs)
    if path.name == ".committed":
        pause("marker")
    return result
Path.touch = marker
sync_directory = deletion.sync_directory
def synced_marker(path):
    sync_directory(path)
    if (path / ".committed").exists():
        pause("marker_synced")
deletion.sync_directory = synced_marker

remove = deletion.shutil.rmtree
unlink = Path.unlink
removals = 0
def removed():
    global removals
    removals += 1
    pause(f"remove_{removals}")
def remove_tree(path, *args, **kwargs):
    staged = ".orpheus-delete-staging" in Path(path).parts
    if staged and position == "during_remove":
        next(Path(path).rglob("*.mp3")).unlink()
        pause("during_remove")
    result = remove(path, *args, **kwargs)
    if staged:
        removed()
    return result
def remove_file(path, *args, **kwargs):
    result = unlink(path, *args, **kwargs)
    if ".orpheus-delete-staging" in path.parts:
        removed()
    return result
deletion.shutil.rmtree = remove_tree
Path.unlink = remove_file
service.delete_book(book.id)
raise AssertionError("deletion did not reach boundary")
'''


def _kill_at(root, position):
    process = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(root), position],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "HF_HOME": str(root / "hf"), "HF_HUB_CACHE": str(root / "hf" / "hub")},
    )
    messages = Queue()
    Thread(target=lambda: messages.put(process.stdout.readline()), daemon=True).start()
    try:
        message = messages.get(timeout=20).strip()
        if message != position:
            process.kill()
            process.wait(timeout=10)
            _, stderr = process.communicate(timeout=10)
            pytest.fail(f"Expected {position}, received {message!r}: {stderr}")
        process.kill()
        # Windows termination is asynchronous. Keep stdin open until death is
        # confirmed: communicate() closes it, which can release pause() and
        # let the child roll back the very interrupted transaction we inspect.
        process.wait(timeout=10)
        process.communicate(timeout=10)
        assert process.returncode != 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
            process.communicate(timeout=10)


def _recover(root):
    result = subprocess.run(
        [sys.executable, "-c", _CHILD, str(root), "recover"],
        capture_output=True, text=True, timeout=20,
        env={**os.environ, "HF_HOME": str(root / "hf"), "HF_HUB_CACHE": str(root / "hf" / "hub")},
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["integrity"] == "ok"
    assert payload["foreign_keys"] == []
    return payload


def _hashes(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for base in (root / "output", root / "data" / "sources")
        for path in base.rglob("*") if path.is_file() and path.name != ".orpheus-storage-owner.json"
    }


@pytest.mark.parametrize("position", [
    "manifest", "move_1", "move_2", "before_commit", "after_commit", "checkpoint", "marker", "marker_synced",
    "during_remove", "remove_1", "remove_2", "rollback_1", "rollback_2",
])
def test_restart_recovers_book_deletion_after_process_death(tmp_path, position):
    _kill_at(tmp_path, position)
    committed = position in {"after_commit", "checkpoint", "marker", "marker_synced", "during_remove", "remove_1", "remove_2"}
    expected_rows = {"book": None, "chapters": []} if committed else json.loads((tmp_path / "expected.json").read_text())
    expected = {} if committed else {
        name: hashlib.sha256(data).hexdigest() for name, data in {
            "data/sources/book.txt": b"source text",
            "output/Book/chapters/01.mp3": b"chapter audio",
            "output/Book/chapters/02.mp3": b"second chapter audio",
        }.items()
    }
    for _ in range(2):
        recovered = _recover(tmp_path)
        assert recovered["row"] is not committed
        assert recovered["rows"] == expected_rows
        assert _hashes(tmp_path) == expected
        for managed in (tmp_path / "output", tmp_path / "temp", tmp_path / "data" / "sources"):
            assert (managed / ".orpheus-storage-owner.json").is_file()
        assert not list((tmp_path / "temp" / "delete_staging").iterdir())
        assert not list(tmp_path.rglob(".orpheus-delete-staging"))


def test_restart_retains_preserved_files_when_sql_witness_is_absent(tmp_path):
    _kill_at(tmp_path, "move_2")
    entry = next((tmp_path / "temp" / "delete_staging").iterdir())
    preserve_delete_transaction(entry)
    repo = SQLiteRepository(tmp_path / "data" / "library.db")
    try:
        repo.delete_book(1)
    finally:
        repo.close()
    expected = _hashes(tmp_path)
    assert len(expected) == 3
    for _ in range(2):
        assert _recover(tmp_path)["row"] is False
        assert _hashes(tmp_path) == expected
        assert (entry / "transaction.json").exists()
        assert (entry / ".preserve").exists()
