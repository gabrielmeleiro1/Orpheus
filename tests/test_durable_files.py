"""Durable file replacement and persisted JSON formats."""

import hashlib
import os
from pathlib import Path

import pytest

from modules.contracts import durable_files


def test_file_sync_uses_write_access_without_changing_contents(tmp_path, monkeypatch):
    import errno

    fcntl = pytest.importorskip("fcntl")
    path = tmp_path / "file"
    path.write_bytes(b"keep me")
    original_fsync = os.fsync
    synced = []

    def writable_fsync(descriptor):
        mode = fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE
        if mode == os.O_RDONLY:
            raise OSError(errno.EBADF, "File sync requires write access")
        synced.append(descriptor)
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", writable_fsync)
    with path.open("rb") as stream, pytest.raises(OSError, match="requires write access"):
        os.fsync(stream.fileno())
    durable_files.sync_file(path)
    assert len(synced) == 1
    assert path.read_bytes() == b"keep me"


def test_directory_sync_does_not_open_directories_on_windows(tmp_path, monkeypatch):
    from types import SimpleNamespace

    def fail_open(*args):
        pytest.fail("Directory must not be opened")

    monkeypatch.setattr(durable_files, "os", SimpleNamespace(name="nt", open=fail_open))
    durable_files.sync_directory(tmp_path)


@pytest.mark.parametrize(("writer", "payload"), [
    (durable_files.atomic_write_bytes, b"bytes"),
    (durable_files.atomic_write_text, "text"),
    (durable_files.atomic_write_json, {"a": 1}),
])
def test_atomic_writers_support_file_sync_requiring_write_access(tmp_path, monkeypatch, writer, payload):
    import errno

    fcntl = pytest.importorskip("fcntl")
    original_fsync = os.fsync
    synced = []

    def writable_fsync(descriptor):
        mode = fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE
        if mode == os.O_RDONLY:
            raise OSError(errno.EBADF, "File sync requires write access")
        synced.append(descriptor)
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", writable_fsync)
    writer(tmp_path / "output", payload, sync_parent=lambda parent: None)
    assert len(synced) == 1


@pytest.mark.parametrize("sync", [durable_files.sync_directory, durable_files.sync_directory_if_openable])
def test_directory_sync_closes_handle_when_sync_fails(tmp_path, monkeypatch, sync):
    from types import SimpleNamespace

    closed = []

    def fail_sync(descriptor):
        assert descriptor == 123
        raise OSError("sync failed")

    monkeypatch.setattr(durable_files, "os", SimpleNamespace(
        name="posix", O_RDONLY=os.O_RDONLY, open=lambda *args: 123,
        fsync=fail_sync, close=closed.append,
    ))
    with pytest.raises(OSError, match="sync failed"):
        sync(tmp_path)
    assert closed == [123]


@pytest.mark.parametrize("platform", ["posix", "nt"])
def test_optional_directory_sync_tolerates_open_failure_on_every_platform(tmp_path, monkeypatch, platform):
    from types import SimpleNamespace

    opened = []

    def fail_open(path, flags):
        opened.append(path)
        raise OSError("cannot open directory")

    monkeypatch.setattr(durable_files, "os", SimpleNamespace(
        name=platform, O_RDONLY=os.O_RDONLY, open=fail_open,
    ))
    durable_files.sync_directory_if_openable(tmp_path)
    assert opened == [tmp_path]


@pytest.mark.parametrize(("writer", "payload", "expected"), [
    (durable_files.atomic_write_bytes, b"\x00\xff\n", b"\x00\xff\n"),
    (durable_files.atomic_write_text, "café\n", "café".encode() + os.linesep.encode()),
    (durable_files.atomic_write_json, {"z": "café", "a": 1}, b'{"z": "caf\\u00e9", "a": 1}'),
])
def test_atomic_replacement_syncs_file_before_replace_and_directory_after(
    tmp_path, monkeypatch, writer, payload, expected,
):
    path = tmp_path / "output"
    path.write_bytes(b"old")
    events = []
    original_fsync, original_replace = os.fsync, os.replace

    def sync(descriptor):
        assert path.read_bytes() == b"old"
        events.append("file sync")
        original_fsync(descriptor)

    def replace(source, destination):
        assert source.parent == path.parent
        assert source.name.startswith(".output.") and source.name.endswith(".tmp")
        assert source.read_bytes() == expected
        events.append("replace")
        original_replace(source, destination)

    def sync_parent(parent):
        assert parent == path.parent
        assert path.read_bytes() == expected
        events.append("directory sync")

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(os, "replace", replace)
    writer(path, payload, sync_parent=sync_parent)
    assert events == ["file sync", "replace", "directory sync"]
    assert list(tmp_path.iterdir()) == [path]


def test_json_formatting_and_text_encoding_are_explicit(tmp_path):
    path = tmp_path / "output"
    durable_files.atomic_write_json(
        path, {"z": "café", "a": 1}, sort_keys=True, ensure_ascii=False,
        indent=2, separators=(",", ": "), trailing_newline=True,
    )
    assert path.read_bytes() == '{\n  "a": 1,\n  "z": "café"\n}\n'.replace("\n", os.linesep).encode()
    durable_files.atomic_write_text(path, "café", encoding="latin-1")
    assert path.read_bytes() == b"caf\xe9"


def test_json_serialization_failure_retains_partial_temp_when_requested(tmp_path):
    path, pending = tmp_path / "output", tmp_path / "output.tmp"
    path.write_bytes(b"previous")
    with pytest.raises(TypeError):
        durable_files.atomic_write_json(
            path, {"first": 1, "second": object()}, temporary_path=pending, cleanup="none",
        )
    assert path.read_bytes() == b"previous"
    assert pending.read_bytes() == b'{"first": 1, "second": '


@pytest.mark.parametrize("failure", ["write", "file_sync", "permissions", "replace", "directory_sync"])
@pytest.mark.parametrize("cleanup", ["always", "before_replace", "none"])
def test_atomic_failure_respects_cleanup_and_replacement_policy(tmp_path, monkeypatch, failure, cleanup):
    path, pending = tmp_path / "output", tmp_path / "output.tmp"
    path.write_bytes(b"previous")

    def fail(*args):
        raise OSError("write failed")

    options = {"temporary_path": pending, "cleanup": cleanup}
    if failure == "write":
        options["before_write"] = fail
    elif failure == "file_sync":
        monkeypatch.setattr(os, "fsync", fail)
    elif failure == "permissions":
        options["permissions"] = 0o600
        monkeypatch.setattr(os, "chmod", fail)
    elif failure == "replace":
        monkeypatch.setattr(os, "replace", fail)
    else:
        options["sync_parent"] = fail

    with pytest.raises(OSError, match="write failed"):
        durable_files.atomic_write_bytes(path, b"new", **options)
    assert path.read_bytes() == (b"new" if failure == "directory_sync" else b"previous")
    assert pending.exists() == (cleanup == "none" and failure != "directory_sync")


def test_permissions_hook_runs_before_payload_and_mode_is_applied_after_sync(tmp_path, monkeypatch):
    path, pending = tmp_path / "output", tmp_path / "output.tmp"
    events = []
    original_fsync = os.fsync

    def before_write(temporary):
        assert temporary == pending
        assert temporary.read_bytes() == b""
        events.append("before write")

    def sync(descriptor):
        assert pending.read_bytes() == b"payload"
        events.append("sync")
        original_fsync(descriptor)

    def chmod(temporary, mode):
        assert temporary == pending and mode == 0o600
        events.append("permissions")

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(os, "chmod", chmod)
    durable_files.atomic_write_bytes(
        path, b"payload", temporary_path=pending, before_write=before_write,
        permissions=0o600, sync_parent=lambda parent: None,
    )
    assert events == ["before write", "sync", "permissions"]


@pytest.mark.parametrize("cleanup", ["always", "before_replace", "none"])
def test_cleanup_policy_controls_temp_name_after_replacement(tmp_path, cleanup):
    path, pending = tmp_path / "output", tmp_path / "output.tmp"

    def sync_parent(parent):
        pending.write_bytes(b"another writer")
        raise OSError("directory sync failed")

    with pytest.raises(OSError, match="directory sync failed"):
        durable_files.atomic_write_bytes(
            path, b"new", temporary_path=pending, cleanup=cleanup, sync_parent=sync_parent,
        )
    assert path.read_bytes() == b"new"
    assert pending.exists() == (cleanup != "always")


@pytest.mark.parametrize("cleanup", ["always", "before_replace"])
def test_interrupted_write_removes_unique_temp_and_keeps_destination(tmp_path, cleanup):
    path = tmp_path / "output"
    path.write_bytes(b"previous")

    def interrupt(pending):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        durable_files.atomic_write_bytes(path, b"new", before_write=interrupt, cleanup=cleanup)
    assert path.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [path]


def test_delete_manifest_preserves_json_bytes(tmp_path):
    from modules.storage.deletion import write_delete_transaction

    entry = tmp_path / "staging" / "entry"
    entry.mkdir(parents=True)
    write_delete_transaction(entry, 7, [(Path("café"), Path("staged"))])

    assert (entry / "transaction.json").read_bytes() == (
        b'{"book_id": 7, "artifacts": [{"original": "caf\\u00e9", "staged": "staged"}]}'
    )
    assert not (entry / "transaction.json.tmp").exists()


def test_quarantine_record_preserves_json_bytes_and_private_permissions(tmp_path):
    from modules.storage.sqlite_repo import _write_quarantine_record

    path = tmp_path / "quarantine.json"
    _write_quarantine_record(path, {"z": "café", "a": [1, True]})

    assert path.read_bytes() == b'{"z": "caf\\u00e9", "a": [1, true]}'
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_quarantine_record_restricts_permissions_before_writing(tmp_path, monkeypatch):
    from modules.storage import sqlite_repo

    path = tmp_path / "quarantine.json"
    restricted = []

    def owner_only(temporary):
        assert temporary == path.with_name(path.name + ".tmp")
        assert temporary.read_bytes() == b""
        restricted.append(temporary)

    monkeypatch.setattr(sqlite_repo, "owner_only", owner_only)
    sqlite_repo._write_quarantine_record(path, {"a": 1})
    assert len(restricted) == 1
    assert path.read_bytes() == b'{"a": 1}'


def test_delete_manifest_syncs_all_ancestor_directories_in_order(tmp_path, monkeypatch):
    from modules.storage import deletion

    entry = tmp_path / "staging" / "entry"
    entry.mkdir(parents=True)
    synced = []
    monkeypatch.setattr(deletion, "sync_directory", synced.append)
    deletion.write_delete_transaction(entry, 7, [])
    assert synced == [entry, entry.parent, entry.parent.parent]


def test_preserve_marker_keeps_existing_bytes_and_inode(tmp_path):
    from modules.storage.deletion import preserve_delete_transaction

    marker = tmp_path / ".preserve"
    marker.write_bytes(b"preserved")
    inode = marker.stat().st_ino
    preserve_delete_transaction(tmp_path)
    assert marker.read_bytes() == b"preserved"
    assert marker.stat().st_ino == inode


def test_runtime_state_preserves_json_bytes_and_private_permissions(tmp_path):
    from modules.runtime_setup.state import _atomic_write_json

    path = tmp_path / "nested" / "state.json"
    _atomic_write_json(path, {"z": "café", "a": [1, True]})

    assert path.read_bytes() == b'{"a":[1,true],"z":"caf\\u00e9"}' + os.linesep.encode()
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    assert list(path.parent.iterdir()) == [path]


def test_model_commit_journal_preserves_runtime_state_json_format(tmp_path):
    from modules.app.services.model_storage_service import _commit_staged_models

    staged = tmp_path / "model"
    manifest = tmp_path / "model.json"
    manifest.write_text('{"z":"café","settings_committed":false}', encoding="utf-8")
    _commit_staged_models({"model": staged})
    assert manifest.read_bytes() == (
        b'{"settings_committed":true,"z":"caf\\u00e9"}' + os.linesep.encode()
    )


def test_download_receipt_preserves_json_bytes_and_private_permissions(tmp_path):
    from modules.runtime_setup.receipts import write_download_receipt

    (tmp_path / "model.bin").write_bytes(b"weights")
    path = write_download_receipt(tmp_path, model_id="owner/model", revision="a" * 40)

    digest = hashlib.sha256(b"weights").hexdigest()
    expected = (
        '{"files":[{"path":"model.bin","sha256":"' + digest
        + '","size":7}],"model_id":"owner/model","revision":"' + "a" * 40
        + '","schema_version":2}' + os.linesep
    )
    assert path.read_bytes() == expected.encode("utf-8")
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("writer", ["deletion", "quarantine", "state", "receipt"])
def test_failed_replacement_preserves_previous_file_and_temp_policy(tmp_path, monkeypatch, writer):
    from modules.runtime_setup.receipts import DOWNLOAD_RECEIPT_FILENAME, write_download_receipt
    from modules.runtime_setup.state import _atomic_write_json
    from modules.storage.deletion import write_delete_transaction
    from modules.storage.sqlite_repo import _write_quarantine_record

    names = {"deletion": "transaction.json", "quarantine": "quarantine.json",
             "state": "state.json", "receipt": DOWNLOAD_RECEIPT_FILENAME}
    path = tmp_path / names[writer]
    path.write_bytes(b"previous")
    if writer == "receipt":
        (tmp_path / "model.bin").write_bytes(b"weights")

    def fail_replace(*args):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        if writer == "deletion":
            write_delete_transaction(tmp_path, 7, [])
        elif writer == "quarantine":
            _write_quarantine_record(path, {"a": 1})
        elif writer == "state":
            _atomic_write_json(path, {"a": 1})
        else:
            write_download_receipt(tmp_path, model_id="owner/model", revision="a" * 40)

    assert path.read_bytes() == b"previous"
    remaining = [item.name for item in tmp_path.iterdir() if item != path and item.name != "model.bin"]
    assert remaining == ([path.name + ".tmp"] if writer in {"deletion", "quarantine"} else [])


@pytest.mark.parametrize("writer", ["state", "receipt"])
def test_runtime_writes_report_directory_sync_failure_after_replacement(tmp_path, monkeypatch, writer):
    import stat

    from modules.runtime_setup.receipts import DOWNLOAD_RECEIPT_FILENAME, write_download_receipt
    from modules.runtime_setup.state import _atomic_write_json

    path = tmp_path / ("state.json" if writer == "state" else DOWNLOAD_RECEIPT_FILENAME)
    path.write_bytes(b"previous")
    original_fsync = os.fsync

    def sync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("directory sync failed")
        original_fsync(descriptor)

    if os.name == "nt":
        pytest.skip("Directory handles are unavailable on Windows")
    monkeypatch.setattr(os, "fsync", sync)
    with pytest.raises(OSError, match="directory sync failed"):
        if writer == "state":
            _atomic_write_json(path, {"a": 1})
        else:
            (tmp_path / "model.bin").write_bytes(b"weights")
            write_download_receipt(tmp_path, model_id="owner/model", revision="a" * 40)

    assert path.read_bytes() != b"previous"
    assert not list(tmp_path.glob(".*.tmp"))
