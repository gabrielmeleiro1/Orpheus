"""Root claims remain exclusive across aliases and competing processes."""

import os
import json
from queue import Empty, Queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from modules.storage import managed_roots
from modules.storage.managed_roots import STORAGE_OWNER_NAME, StorageOwnershipError, claim_managed_roots


@pytest.mark.skipif(os.name != "nt", reason="Windows account profile API")
def test_windows_gate_ignores_environment_before_first_native_call(tmp_path):
    script = "from modules.storage.managed_roots import _claim_gate_directory; print(_claim_gate_directory())"
    gates = []
    for name in ("first", "second"):
        home = tmp_path / name
        local = home / "AppData" / "Local"
        local.mkdir(parents=True)
        result = subprocess.run(
            [sys.executable, "-c", script], check=True, capture_output=True, text=True,
            env={**os.environ, "HOME": str(home), "USERPROFILE": str(home),
                 "LOCALAPPDATA": str(local), "APPDATA": str(home / "AppData" / "Roaming"),
                 "TMPDIR": str(home), "TMP": str(home), "TEMP": str(home)},
        )
        gate = Path(result.stdout.strip())
        assert not gate.is_relative_to(home)
        gates.append(gate)
    assert gates[0] == gates[1]


@pytest.mark.skipif(os.name != "nt", reason="Windows 0700 mkdir ACL")
def test_windows_gate_directory_inherits_the_profile_temp_acl(tmp_path, monkeypatch):
    # A 0700 gate created from a sandboxed token would lock the account out.
    gate = tmp_path / "gate"
    monkeypatch.setattr(managed_roots, "_claim_gate_directory", lambda: gate)
    real_mkdir = Path.mkdir
    modes = []

    def record_mkdir(path, mode=0o777, parents=False, exist_ok=False):
        if path == gate:
            modes.append(mode)
        return real_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", record_mkdir)
    with managed_roots._claim_gate():
        pass
    assert modes == [0o777]


def test_stored_database_owner_does_not_follow_a_retargeted_filename(tmp_path):
    first, second = tmp_path / "a.db", tmp_path / "b.db"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    root = tmp_path / "output"
    claim_managed_roots(first, root)
    before = (root / STORAGE_OWNER_NAME).read_bytes()
    first.unlink()
    try:
        first.symlink_to(second)
    except OSError as error:
        pytest.skip(f"Database symlinks are unavailable: {error}")

    with pytest.raises(StorageOwnershipError, match="belongs to"):
        claim_managed_roots(second, root)
    assert (root / STORAGE_OWNER_NAME).read_bytes() == before


@pytest.mark.parametrize("retarget", ["database", "parent"])
def test_live_database_identity_cannot_claim_the_alias_targets_storage(tmp_path, retarget):
    first_dir, second_dir = tmp_path / "a", tmp_path / "b"
    first_dir.mkdir()
    second_dir.mkdir()
    first, second = first_dir / "library.db", second_dir / "library.db"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    output = tmp_path / "output-b"
    claim_managed_roots(second, output)
    before = (output / STORAGE_OWNER_NAME).read_bytes()
    first.unlink()
    try:
        if retarget == "database":
            first.symlink_to(second)
        else:
            first_dir.rmdir()
            first_dir.symlink_to(second_dir, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Symlinks are unavailable: {error}")

    with pytest.raises(StorageOwnershipError, match="owned canonical"):
        claim_managed_roots(first, output)
    assert (output / STORAGE_OWNER_NAME).read_bytes() == before


_CLAIM = """
from pathlib import Path
import sys
from modules.storage import managed_roots as roots
database, root, pause, *identity = sys.argv[1:]
original = roots._check_overlaps
first = True
def check(root, database, **kwargs):
    global first
    rewrites = original(root, database, **kwargs)
    if first and pause == 'yes':
        first = False
        print('checked', flush=True)
        sys.stdin.readline()
    return rewrites
roots._check_overlaps = check
print('started', flush=True)
try:
    roots.claim_managed_roots(Path(database), Path(root), library_id=identity[0] if identity else None)
    print('claimed', flush=True)
except roots.StorageOwnershipError:
    print('refused', flush=True)
"""


@pytest.mark.parametrize("parent_first", [False, True])
@pytest.mark.parametrize("relocating", [False, True])
def test_overlapping_process_claims_do_not_leave_a_losing_owner_record(tmp_path, parent_first, relocating):
    parent, child = tmp_path / "managed", tmp_path / "managed" / "nested"
    first_root, second_root = (parent, child) if parent_first else (child, parent)
    first_db, second_db = tmp_path / "a.db", tmp_path / "b.db"
    identity = []
    if relocating:
        original = tmp_path / "original.db"
        original.touch()
        identity = ["shared-library"]
        claim_managed_roots(original, first_root, library_id=identity[0])
        original.unlink()
        first_db.touch()
        second_db.touch()
    processes = []
    try:
        for database, root, pause, name in (
            (first_db, first_root, "yes", "first"),
            (second_db, second_root, "no", "second"),
        ):
            # Different launch environments must still coordinate one user's
            # claims. Everything these processes manage stays in tmp_path.
            home = tmp_path / name
            home.mkdir()
            process = subprocess.Popen(
                [sys.executable, "-c", _CLAIM, str(database), str(root), pause, *identity],
                env={**os.environ, "HOME": str(home), "USERPROFILE": str(home), "TMPDIR": str(home), "TMP": str(home), "TEMP": str(home)},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            processes.append(process)
            assert process.stdout.readline().strip() == "started"
            if pause == "yes":
                assert process.stdout.readline().strip() == "checked"
        first, second = processes
        lines = Queue()
        reader = threading.Thread(target=lambda: lines.put(second.stdout.readline()), daemon=True)
        reader.start()
        with pytest.raises(Empty):
            lines.get(timeout=.25)
        assert not (second_root / STORAGE_OWNER_NAME).exists()
        first.stdin.write("\n")
        first.stdin.flush()
        assert first.stdout.readline().strip() == "claimed"
        assert lines.get(timeout=10).strip() == "refused"
        reader.join(timeout=1)
        for process in processes:
            process.wait(timeout=10)
            assert process.returncode == 0, process.stderr.read()
        assert not (second_root / STORAGE_OWNER_NAME).exists()
        claim_managed_roots(first_db, first_root)
        with pytest.raises(StorageOwnershipError):
            claim_managed_roots(second_db, second_root)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()


def test_claim_does_not_require_hardlink_support(tmp_path, monkeypatch):
    def no_links(*args, **kwargs):
        raise OSError("Filesystem has no hard-link support")

    monkeypatch.setattr(os, "link", no_links)
    claim_managed_roots(tmp_path / "library.db", tmp_path / "output")
    claim_managed_roots(tmp_path / "library.db", tmp_path / "output")


def test_unreadable_descendants_cannot_hide_another_owner(tmp_path, monkeypatch):
    root = tmp_path / "managed"
    root.mkdir()

    def unreadable(*args, **kwargs):
        kwargs["onerror"](PermissionError("nested directory is unreadable"))
        return iter(())

    with monkeypatch.context() as patch:
        patch.setattr(managed_roots.os, "walk", unreadable)
        with pytest.raises(StorageOwnershipError, match="Cannot inspect"):
            claim_managed_roots(tmp_path / "a.db", root)
    assert not (root / STORAGE_OWNER_NAME).exists()
    claim_managed_roots(tmp_path / "b.db", root)


def test_repeated_claim_still_inspects_descendants_after_initial_success(tmp_path):
    """A warm validation must notice ownership records added between calls."""
    database = tmp_path / "a.db"
    root = tmp_path / "output"
    claim_managed_roots(database, root)
    nested = root / "imported" / "another-library"
    nested.mkdir(parents=True)
    (nested / STORAGE_OWNER_NAME).write_text(json.dumps({
        "version": 1, "database": str(tmp_path / "b.db"),
    }), encoding="utf-8")

    with pytest.raises(StorageOwnershipError, match="belongs to"):
        claim_managed_roots(database, root)


def test_failed_claim_gate_acquisition_closes_handles_and_allows_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(managed_roots, "_claim_gate_directory", lambda: tmp_path / "gate")
    opened = []

    def fail_lock(path, *, blocking, on_open):
        handle = path.open("a+b")
        opened.append(handle)
        on_open(handle)
        raise PermissionError("Cannot acquire lock")

    with monkeypatch.context() as patch:
        patch.setattr(managed_roots, "open_process_lock", fail_lock)
        with pytest.raises(PermissionError):
            claim_managed_roots(tmp_path / "a.db", tmp_path / "output")
    assert opened and all(handle.closed for handle in opened)
    claim_managed_roots(tmp_path / "a.db", tmp_path / "output")


@pytest.mark.parametrize("link_kind", ["directory", "ancestor", "file"])
def test_claim_gate_refuses_linked_paths(tmp_path, monkeypatch, link_kind):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "gate"
    try:
        if link_kind in {"directory", "ancestor"}:
            alias.symlink_to(real, target_is_directory=True)
            gate_dir = alias if link_kind == "directory" else alias / "nested"
        else:
            alias.mkdir()
            payload = real / "untouched"
            payload.write_bytes(b"keep")
            (alias / "storage-claims.lock").symlink_to(payload)
            gate_dir = alias
    except OSError as error:
        pytest.skip(f"Symlinks are unavailable: {error}")
    monkeypatch.setattr(managed_roots, "_claim_gate_directory", lambda: gate_dir)
    with pytest.raises(StorageOwnershipError, match="must not be a link"):
        claim_managed_roots(tmp_path / "a.db", tmp_path / "output")
    assert not (tmp_path / "output").exists()
    if link_kind == "file":
        assert payload.read_bytes() == b"keep"
    else:
        assert list(real.iterdir()) == []
