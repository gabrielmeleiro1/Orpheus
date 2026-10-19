from contextlib import closing
from pathlib import Path
import json
import os
import sqlite3

import pytest

from modules.contracts import durable_files
from modules.storage import maintenance_reconcile
from modules.storage.maintenance import (
    backup_database,
    prune_stale_records_and_files,
    restore_database_backup,
)
from modules.storage.models import BookCreate, PlaybackStatus, SourceType
from modules.storage.sqlite_repo import SQLiteRepository


def _library_with_upload(root):
    repo = SQLiteRepository(root / "data" / "library.db")
    source = root / "temp" / "web_uploads" / "shared.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"shared source")
    audio = root / "output" / "saved" / "book.m4b"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"audio")
    book = repo.create_book(BookCreate(
        title="Saved", source_path=str(source), source_type=SourceType.TXT,
        playback_path=str(audio), playback_status=PlaybackStatus.PACKAGED,
    ))
    return repo, source, book


def _cleanup(repo, root):
    return prune_stale_records_and_files(
        repo.db_path, output_dir=root / "output", temp_dir=root / "temp",
        aggressive=True, grace_seconds=0,
    )


def test_restore_succeeds_when_relocation_history_is_unreadable(tmp_path, caplog):
    repo, source, book = _library_with_upload(tmp_path)
    try:
        backup = backup_database(repo.db_path)
        _cleanup(repo, tmp_path)
        relocated = Path(repo.get_book(book.id).source_path)
        repo.update_book(book.id, title="Current library")
        history = maintenance_reconcile._source_relocations_path(repo.db_path)
        history.write_text("{", encoding="utf-8")

        restore_error = None
        try:
            safety = restore_database_backup(repo, backup.name)
        except ValueError as error:
            restore_error = error
        assert restore_error is None, f"Restore rejected source repair failure: {restore_error}"
        assert safety.is_file()
        assert repo.get_book(book.id).title == "Saved"
        assert repo.get_book(book.id).source_path == str(source)
        assert repo.library_generation == 1
        assert "source" in caplog.text.lower()
        with pytest.raises(ValueError, match="Cannot read source relocation history"):
            _cleanup(repo, tmp_path)
        assert relocated.read_bytes() == b"shared source"
        assert repo.get_book(book.id).title == "Saved"
    finally:
        repo.close()


def test_restore_rolls_back_partial_source_repair_without_reverting_library(tmp_path, monkeypatch):
    from modules.storage import maintenance

    repo, _, book = _library_with_upload(tmp_path)
    try:
        backup = backup_database(repo.db_path)
        repo.update_book(book.id, title="Current library")

        def fail_repair(conn, *args):
            conn.execute("UPDATE books SET title='Partial repair' WHERE id=?", (book.id,))
            raise OSError("cannot repair source")

        monkeypatch.setattr(maintenance, "_repair_relocated_upload_sources", fail_repair)
        restore_error = None
        try:
            restore_database_backup(repo, backup.name)
        except RuntimeError as error:
            restore_error = error
        assert restore_error is None, f"Restore rejected source repair failure: {restore_error}"
        assert repo.get_book(book.id).title == "Saved"
        assert not repo._conn.in_transaction
    finally:
        repo.close()


@pytest.mark.parametrize("collision", [False, True])
def test_historical_restore_keeps_relocated_source_available_after_cleanup(tmp_path, collision):
    repo, source, book = _library_with_upload(tmp_path)
    try:
        backup = backup_database(repo.db_path)
        if collision:
            occupied = repo.db_path.parent / "sources" / source.name
            occupied.parent.mkdir(parents=True, exist_ok=True)
            occupied.write_bytes(b"different source")
        _cleanup(repo, tmp_path)
        relocated = Path(repo.get_book(book.id).source_path)
        assert relocated.name == ("shared_1.txt" if collision else "shared.txt")
        assert not source.exists()

        restore_database_backup(repo, backup.name)
        assert repo.get_book(book.id).source_path == str(relocated)
        _cleanup(repo, tmp_path)
        assert relocated.read_bytes() == b"shared source"
        assert repo.get_book(book.id).source_path == str(relocated)
    finally:
        repo.close()


@pytest.mark.parametrize("collision", [False, True])
@pytest.mark.parametrize("audio_exists", [False, True])
def test_cleanup_repairs_missing_upload_pointer_before_deleting_durable_source(tmp_path, collision, audio_exists):
    repo, source, book = _library_with_upload(tmp_path)
    try:
        if collision:
            occupied = repo.db_path.parent / "sources" / source.name
            occupied.parent.mkdir(parents=True, exist_ok=True)
            occupied.write_bytes(b"different source")
        _cleanup(repo, tmp_path)
        relocated = Path(repo.get_book(book.id).source_path)
        repo.update_book(book.id, source_path="temp/web_uploads/shared.txt")
        if not audio_exists:
            Path(repo.get_book(book.id).playback_path).unlink()
        _cleanup(repo, tmp_path)
        assert relocated.exists(), "cleanup deleted the only relocated source"
        assert repo.get_book(book.id).source_path == str(relocated)
    finally:
        repo.close()


def test_source_relocation_syncs_copy_and_directory_before_committing(tmp_path, monkeypatch):
    repo, source, book = _library_with_upload(tmp_path)
    events = []
    destination = repo.db_path.parent / "sources" / source.name
    connect = maintenance_reconcile._connect

    class RecordingConnection(sqlite3.Connection):
        def commit(self):
            events.append("commit")
            super().commit()

    def recording_connect(path):
        return sqlite3.connect(path, factory=RecordingConnection)

    def sync_copy(path):
        if path == destination:
            assert path.read_bytes() == b"shared source"
            with closing(connect(repo.db_path)) as conn:
                assert conn.execute("SELECT source_path FROM books WHERE id=?", (book.id,)).fetchone()[0] == str(source)
            events.append("file")

    def sync_parent(path):
        if path == destination.parent:
            events.append("directory")

    unlink = Path.unlink

    def observe_unlink(path, *args, **kwargs):
        if path == source:
            events.append("unlink")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(maintenance_reconcile, "_connect", recording_connect)
    monkeypatch.setattr(maintenance_reconcile, "sync_file", sync_copy, raising=False)
    monkeypatch.setattr(maintenance_reconcile, "sync_directory", sync_parent, raising=False)
    monkeypatch.setattr(Path, "unlink", observe_unlink)
    try:
        maintenance_reconcile._move_referenced_upload_sources(
            repo.db_path, tmp_path, tmp_path / "output", tmp_path / "temp", destination.parent,
        )
        assert events == ["file", "directory", "directory", "commit", "unlink"]
    finally:
        repo.close()


@pytest.mark.parametrize("barrier", ["sync_file", "sync_directory"])
def test_source_relocation_keeps_original_and_pointers_when_sync_fails(tmp_path, monkeypatch, barrier):
    repo, source, book = _library_with_upload(tmp_path)
    destination = repo.db_path.parent / "sources" / source.name

    def fail_sync(path):
        raise OSError("cannot sync source")

    monkeypatch.setattr(maintenance_reconcile, "sync_file", durable_files.sync_file, raising=False)
    monkeypatch.setattr(maintenance_reconcile, "sync_directory", durable_files.sync_directory, raising=False)
    monkeypatch.setattr(maintenance_reconcile, barrier, fail_sync)
    try:
        with pytest.raises(OSError, match="cannot sync source"):
            maintenance_reconcile._move_referenced_upload_sources(
                repo.db_path, tmp_path, tmp_path / "output", tmp_path / "temp", destination.parent,
            )
        assert source.read_bytes() == b"shared source"
        assert repo.get_book(book.id).source_path == str(source)
        assert not destination.exists()
    finally:
        repo.close()


def test_cleanup_keeps_ambiguous_relocated_copies_and_book_without_audio(tmp_path):
    repo, source, book = _library_with_upload(tmp_path)
    try:
        Path(book.playback_path).unlink()
        source.unlink()
        sources_dir = repo.db_path.parent / "sources"
        sources_dir.mkdir(parents=True, exist_ok=True)
        candidates = [sources_dir / "shared.txt", sources_dir / "shared_1.txt"]
        for index, path in enumerate(candidates):
            path.write_bytes(f"source {index}".encode())
        for _ in range(2):
            _cleanup(repo, tmp_path)
            assert repo.get_book(book.id) is not None, "cleanup removed the recoverable source reference"
            assert all(path.exists() for path in candidates)
    finally:
        repo.close()


def test_missing_upload_is_not_repointed_without_relocation_record(tmp_path):
    repo, source, book = _library_with_upload(tmp_path)
    try:
        source.unlink()
        Path(book.playback_path).unlink()
        candidate = repo.db_path.parent / "sources" / source.name
        candidate.write_bytes(b"unrelated upload")
        _cleanup(repo, tmp_path)
        assert repo.get_book(book.id).source_path == str(source)
        assert candidate.read_bytes() == b"unrelated upload"
    finally:
        repo.close()


def test_cleanup_keeps_sources_for_missing_absolute_paths_outside_current_roots(tmp_path):
    repo, source, book = _library_with_upload(tmp_path)
    try:
        foreign = tmp_path / "previous-library" / "sources" / source.name
        repo.update_book(book.id, source_path=str(foreign))
        Path(book.playback_path).unlink()
        candidates = [repo.db_path.parent / "sources" / name for name in ("shared.txt", "shared_1.txt")]
        for candidate in candidates:
            candidate.write_bytes(b"possible source")
        for _ in range(2):
            _cleanup(repo, tmp_path)
            assert all(candidate.exists() for candidate in candidates), "cleanup deleted a possible durable source"
            assert repo.get_book(book.id).source_path == str(foreign)
    finally:
        repo.close()


def test_relocation_record_with_mismatched_content_is_not_trusted(tmp_path):
    repo, source, book = _library_with_upload(tmp_path)
    try:
        _cleanup(repo, tmp_path)
        candidate = Path(repo.get_book(book.id).source_path)
        candidate.write_bytes(b"replacement content")
        repo.update_book(book.id, source_path=str(source))
        Path(book.playback_path).unlink()
        _cleanup(repo, tmp_path)
        assert repo.get_book(book.id).source_path == str(source)
        assert candidate.read_bytes() == b"replacement content"
    finally:
        repo.close()


def test_relocation_history_survives_database_rename(tmp_path):
    repo, source, book = _library_with_upload(tmp_path)
    backup = backup_database(repo.db_path)
    (repo.db_path.parent / "sources" / source.name).write_bytes(b"other upload")
    _cleanup(repo, tmp_path)
    candidate = Path(repo.get_book(book.id).source_path)
    assert candidate.name == "shared_1.txt"
    candidate.with_name(source.name).write_bytes(b"other upload")
    database = repo.db_path
    repo.close()
    renamed = database.with_name("renamed.db")
    database.rename(renamed)
    repo = SQLiteRepository(renamed)
    try:
        restore_database_backup(repo, backup.name)
        assert repo.get_book(book.id).source_path == str(candidate)
        _cleanup(repo, tmp_path)
        assert candidate.read_bytes() == b"shared source"
    finally:
        repo.close()


def test_relocation_syncs_a_read_only_upload_copy(tmp_path, monkeypatch):
    repo, source, book = _library_with_upload(tmp_path)
    source.chmod(0o444)
    sync_file = maintenance_reconcile.sync_file
    synced = []

    def require_writable_copy(path):
        assert path.stat().st_mode & 0o200, "managed copy is not owner-writable before sync"
        sync_file(path)
        synced.append(path)

    monkeypatch.setattr(maintenance_reconcile, "sync_file", require_writable_copy)
    try:
        _cleanup(repo, tmp_path)
        candidate = Path(repo.get_book(book.id).source_path)
        assert candidate in synced
        assert candidate.read_bytes() == b"shared source"
        assert not source.exists()
    finally:
        if source.exists():
            source.chmod(0o600)
        repo.close()


def test_cleanup_and_restore_keep_source_coordination_files(tmp_path):
    repo, _, _ = _library_with_upload(tmp_path)
    try:
        backup = backup_database(repo.db_path)
        history = repo.db_path.parent / "sources" / ".orpheus-source-relocations.json"
        history.write_text("{}", encoding="utf-8")
        other = history.with_name(".orpheus-source-state.json")
        other.write_bytes(b"coordination")
        report = _cleanup(repo, tmp_path)
        assert str(history) not in report["audit_before"]["orphan_durable_sources"]
        assert str(other) not in report["audit_before"]["orphan_durable_sources"]
        before = history.read_bytes()
        restore_database_backup(repo, backup.name)
        assert history.read_bytes() == before
        assert other.read_bytes() == b"coordination"
    finally:
        repo.close()


@pytest.mark.parametrize("with_digest", [False, True])
def test_cleanup_migrates_database_sidecar_history_into_owned_sources(tmp_path, with_digest):
    repo, source, book = _library_with_upload(tmp_path)
    try:
        _cleanup(repo, tmp_path)
        candidate = Path(repo.get_book(book.id).source_path)
        history = maintenance_reconcile._source_relocations_path(repo.db_path)
        records = json.loads(history.read_text())
        if not with_digest:
            records = {key: value["name"] for key, value in records.items()}
        legacy = repo.db_path.with_name(repo.db_path.name + ".source-relocations.json")
        history.rename(legacy)
        legacy.write_text(json.dumps(records), encoding="utf-8")
        repo.update_book(book.id, source_path=str(source))
        Path(book.playback_path).unlink()
        _cleanup(repo, tmp_path)
        assert not legacy.exists()
        assert json.loads(history.read_text()) == records
        assert repo.get_book(book.id).source_path == str(candidate if with_digest else source)
        assert candidate.read_bytes() == b"shared source"
    finally:
        repo.close()


def test_historical_restore_after_moving_the_library_keeps_the_relocated_source(tmp_path):
    original, moved = tmp_path / "original", tmp_path / "moved"
    repo, source, book = _library_with_upload(original)
    backup = backup_database(repo.db_path)
    _cleanup(repo, original)
    destination = Path(repo.get_book(book.id).source_path)
    repo.close()
    original.rename(moved)
    destination = moved / destination.relative_to(original)
    with closing(SQLiteRepository(moved / "data" / "library.db")) as repo:
        restore_database_backup(repo, backup.name)
        _cleanup(repo, moved)
        assert destination.exists(), "cleanup deleted the only relocated source"
        assert destination.read_bytes() == b"shared source"
        assert repo.get_book(book.id).source_path == str(destination)


def test_cleanup_protects_verified_source_history_outside_current_temp_root(tmp_path):
    repo, source, book = _library_with_upload(tmp_path)
    try:
        _cleanup(repo, tmp_path)
        destination = Path(repo.get_book(book.id).source_path)
        foreign = tmp_path / "previous" / "web_uploads" / source.name
        history = maintenance_reconcile._source_relocations_path(repo.db_path)
        records = json.loads(history.read_text())
        records[os.path.normcase(str(foreign))] = records[os.path.normcase(str(source))]
        history.write_text(json.dumps(records))
        repo.update_book(book.id, source_path=str(foreign))
        _cleanup(repo, tmp_path)
        assert destination.exists(), "cleanup deleted a source verified by its history"
        assert repo.get_book(book.id).source_path == str(destination)
    finally:
        repo.close()


@pytest.mark.parametrize("referenced", [False, True])
def test_cleanup_removes_read_only_uploads_on_windows(tmp_path, monkeypatch, referenced):
    import os
    import stat
    from types import SimpleNamespace

    repo, source, book = _library_with_upload(tmp_path)
    if not referenced:
        repo.update_book(book.id, source_path="unavailable.txt")
    source.chmod(0o444)
    unlink = Path.unlink
    denied = []

    def windows_unlink(path, *args, **kwargs):
        if path == source and not path.stat().st_mode & stat.S_IWRITE:
            denied.append(path)
            raise PermissionError("read-only upload")
        return unlink(path, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(durable_files, "os", SimpleNamespace(**(vars(os) | {"name": "nt"})))
            patch.setattr(Path, "unlink", windows_unlink)
            _cleanup(repo, tmp_path)
        assert denied
        assert not source.exists(), "read-only upload was not deleted"
    finally:
        if source.exists():
            source.chmod(0o600)
        repo.close()
