from datetime import datetime
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import threading

import pytest

from modules.app.config import AppConfig
from modules.app.controller import AppController, JobStatus
from modules.storage.migrations import LATEST_SCHEMA_VERSION
from modules.storage.maintenance import backup_database
from modules.storage.models import BookCreate, ChapterCreate, SourceType
from modules.storage.sqlite_repo import SQLiteRepository
from tests.test_web_api import _make_client_with_controller


@pytest.fixture
def controller(tmp_path):
    config = AppConfig(
        data_dir=tmp_path / "data", output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp", db_path=tmp_path / "data" / "library.db",
        worker_pool_reuse_enabled=False,
    )
    service = AppController(config)
    yield service
    service.repository.close()


def saved_book(repo):
    book, chapters = repo.create_book_with_chapters(
        BookCreate(title="Saved", author="Author", source_path="source.txt", source_type=SourceType.TXT, total_chapters=1),
        [ChapterCreate(book_id=0, chapter_number=1, title="Chapter", duration_ms=10000)],
    )
    repo.update_book(book.id, tags="history", favorite=True)
    repo.update_book_playback_position(book.id, 4.5)
    return book, chapters


def test_corrupt_library_advertises_quarantine_and_restores_backup(controller):
    repo = controller.repository
    book, chapters = saved_book(repo)
    backup = backup_database(repo.db_path)
    repo.close()
    repo.db_path.write_bytes(b"not a sqlite database")
    repo = SQLiteRepository(repo.db_path)
    controller.repository = repo
    controller._library_service._repository = repo
    controller._conversion_service._repository = repo
    client = _make_client_with_controller(controller)

    diagnostics = client.get("/api/maintenance/diagnostics").json()
    quarantine = diagnostics["quarantine"]
    assert Path(quarantine["path"]).read_bytes() == b"not a sqlite database"
    datetime.fromisoformat(quarantine["created_at"])
    repo.close()
    repo = SQLiteRepository(repo.db_path)
    controller.repository = repo
    controller._library_service._repository = repo
    controller._conversion_service._repository = repo
    assert client.get("/api/maintenance/diagnostics").json()["quarantine"] == quarantine

    listed = client.get("/api/maintenance/backups")
    assert listed.status_code == 200
    item = next(item for item in listed.json()["items"] if item["id"] == backup.name)
    assert item["filename"] == backup.name
    assert item["size"] == backup.stat().st_size
    assert item["book_count"] == 1
    datetime.fromisoformat(item["created_at"])
    response = client.post(f"/api/maintenance/backups/{backup.name}/restore")
    assert response.status_code == 200
    restored, restored_chapters = repo.get_book_with_chapters(book.id)
    assert restored.author == "Author"
    assert restored.tags == "history"
    assert restored.favorite
    assert restored.playback_position_sec == 4.5
    assert restored_chapters == chapters
    assert client.get("/api/maintenance/diagnostics").json()["quarantine"] is None


def test_restore_preserves_current_database_as_safety_copy(controller):
    repo = controller.repository
    book, _ = saved_book(repo)
    backup = backup_database(repo.db_path)
    repo.update_book(book.id, title="Current")
    response = _make_client_with_controller(controller).post(f"/api/maintenance/backups/{backup.name}/restore")
    assert response.status_code == 200
    safety = Path(response.json()["safety_copy_path"])
    assert safety != backup
    assert safety.parent == backup.parent
    with closing(sqlite3.connect(safety)) as conn:
        assert conn.execute("SELECT title FROM books").fetchone()[0] == "Current"
    assert repo.get_book(book.id).title == "Saved"
    repo.update_book(book.id, title="Writable")
    assert repo.get_book(book.id).title == "Writable"


@pytest.mark.parametrize("active_reader", [False, True])
def test_restore_refuses_external_connections_without_losing_current_data(controller, active_reader):
    repo = controller.repository
    book, _ = saved_book(repo)
    backup = backup_database(repo.db_path)
    repo.update_book(book.id, title="Current")
    repo._conn.execute("PRAGMA busy_timeout=1")
    with closing(sqlite3.connect(repo.db_path)) as external:
        if active_reader:
            external.execute("BEGIN")
        assert external.execute("SELECT title FROM books").fetchone()[0] == "Current"
        response = _make_client_with_controller(controller).post(f"/api/maintenance/backups/{backup.name}/restore")
        assert response.status_code == 409
        assert "close" in response.json()["detail"].lower()
        assert repo.get_book(book.id).title == "Current"
        assert external.execute("SELECT title FROM books").fetchone()[0] == "Current"
    safety = next(backup.parent.glob("*safety*"))
    with closing(sqlite3.connect(safety)) as conn:
        assert conn.execute("SELECT title FROM books").fetchone()[0] == "Current"
    controller.restore_database_backup(backup.name)
    assert repo.get_book(book.id).title == "Saved"


@pytest.mark.parametrize("locked_file", ["database", "wal", "shm"])
def test_restore_preserves_current_data_when_a_database_file_is_locked(controller, monkeypatch, locked_file):
    repo = controller.repository
    book, _ = saved_book(repo)
    backup = backup_database(repo.db_path)
    repo.update_book(book.id, title="Current")
    locked_path = repo.db_path if locked_file == "database" else repo.db_path.with_name(repo.db_path.name + "-" + locked_file)
    replace, unlink = Path.replace, Path.unlink

    def replace_unless_locked(path, target):
        if Path(target) == locked_path:
            raise PermissionError("The file is being used by another process")
        return replace(path, target)

    def unlink_unless_locked(path, *args, **kwargs):
        if path == locked_path:
            raise PermissionError("The file is being used by another process")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", replace_unless_locked)
        patch.setattr(Path, "unlink", unlink_unless_locked)
        response = _make_client_with_controller(controller).post(f"/api/maintenance/backups/{backup.name}/restore")
    assert response.status_code == 409
    assert "safety" in response.json()["detail"].lower()
    assert repo.get_book(book.id).title == "Current"
    safety = next(backup.parent.glob("*safety*"))
    with closing(sqlite3.connect(safety)) as conn:
        assert conn.execute("SELECT title FROM books").fetchone()[0] == "Current"
    controller.restore_database_backup(backup.name)
    assert repo.get_book(book.id).title == "Saved"


@pytest.mark.parametrize("operation", ["conversion", "repair", "cleanup"])
def test_restore_refuses_active_storage_work(controller, operation):
    book, _ = saved_book(controller.repository)
    backup = backup_database(controller.config.db_path)
    conversion = controller._conversion_service
    if operation == "cleanup":
        conversion.begin_storage_cleanup()
    else:
        conversion._active_job = SimpleNamespace(status=JobStatus.RUNNING, book_id=book.id if operation == "repair" else None)
    try:
        response = _make_client_with_controller(controller).post(f"/api/maintenance/backups/{backup.name}/restore")
        assert response.status_code == 409
        assert controller.repository.get_book(book.id) is not None
    finally:
        if operation == "cleanup":
            conversion.end_storage_cleanup()
        else:
            conversion._active_job = None


@pytest.mark.parametrize("backup_id", ["missing.db", "..%2Foutside.db", "%2Ftmp%2Foutside.db", "linked.db"])
def test_restore_rejects_unknown_paths_and_symlinks(controller, backup_id):
    saved_book(controller.repository)
    backups = controller.config.db_path.parent / "backups"
    backups.mkdir(exist_ok=True)
    (backups / "linked.db").symlink_to(controller.config.db_path)
    response = _make_client_with_controller(controller).post(f"/api/maintenance/backups/{backup_id}/restore")
    assert response.status_code == 404
    assert controller.repository.count_books() == 1


@pytest.mark.parametrize("damage", ["corrupt", "future_schema", "incompatible_schema"])
def test_restore_rejects_invalid_backups_without_changing_current_library(controller, damage):
    book, _ = saved_book(controller.repository)
    backup = backup_database(controller.config.db_path)
    if damage == "corrupt":
        backup.write_bytes(b"broken backup")
    else:
        with closing(sqlite3.connect(backup)) as conn, conn:
            if damage == "future_schema":
                conn.execute("INSERT INTO schema_migrations(version) VALUES (999)")
            else:
                conn.execute("DROP TABLE chapters")
    response = _make_client_with_controller(controller).post(f"/api/maintenance/backups/{backup.name}/restore")
    assert response.status_code == 400
    assert controller.repository.get_book(book.id).title == "Saved"
    assert list(backup.parent.glob("*safety*")) == []


def test_restore_migrates_older_backups(controller):
    saved_book(controller.repository)
    backup = backup_database(controller.config.db_path)
    with closing(sqlite3.connect(backup)) as conn, conn:
        conn.execute("DELETE FROM schema_migrations WHERE version >= 8")
        conn.execute("ALTER TABLE chapters DROP COLUMN packaged_start_ms")
    response = _make_client_with_controller(controller).post(f"/api/maintenance/backups/{backup.name}/restore")
    assert response.status_code == 200
    with closing(sqlite3.connect(controller.config.db_path)) as conn:
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == LATEST_SCHEMA_VERSION
        assert "packaged_start_ms" in [row[1] for row in conn.execute("PRAGMA table_info(chapters)")]


def test_restore_rolls_back_when_reopening_fails(controller, monkeypatch):
    from modules.storage import maintenance

    book, _ = saved_book(controller.repository)
    backup = backup_database(controller.config.db_path)
    controller.repository.update_book(book.id, title="Current")
    migrate = maintenance.run_migrations
    calls = 0

    def fail_first_reopen(conn):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("Cannot reopen restored database")
        return migrate(conn)

    monkeypatch.setattr(maintenance, "run_migrations", fail_first_reopen)
    client = _make_client_with_controller(controller)
    response = client.post(f"/api/maintenance/backups/{backup.name}/restore")
    assert calls >= 2
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "internal_error"
    assert "Cannot reopen" not in response.text
    assert controller.repository.get_book(book.id).title == "Current"
    assert not controller._conversion_service._storage_cleanup_active
    assert list(backup.parent.glob("*safety*"))


def stage_interrupted_deletion(controller, monkeypatch):
    source = controller.config.data_dir / "sources" / "book.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source bytes")
    audio = controller.config.output_dir / "Saved" / "book.m4b"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"audio bytes")
    book, _ = saved_book(controller.repository)
    controller.repository.update_book(book.id, source_path=str(source), playback_path=str(audio))
    backup = backup_database(controller.config.db_path)

    def interrupt_delete(book_id):
        raise KeyboardInterrupt()

    with monkeypatch.context() as patch:
        patch.setattr(controller.repository, "delete_book", interrupt_delete)
        with pytest.raises(KeyboardInterrupt):
            controller.delete_book(book.id)
    entry = next((controller.config.temp_dir / "delete_staging").iterdir())
    assert not source.exists()
    assert not audio.exists()
    return book, backup, source, audio, entry


def test_quarantined_library_preserves_staged_files_until_backup_restore(controller, monkeypatch):
    from modules.storage.deletion import read_delete_transaction

    book, backup, source, audio, entry = stage_interrupted_deletion(controller, monkeypatch)
    _, artifacts = read_delete_transaction(
        entry, allowed_roots=(controller.config.output_dir, controller.config.temp_dir, source.parent),
    )
    controller.repository.close()
    controller.config.db_path.write_bytes(b"not a sqlite database")
    for restart in range(2):
        restarted = AppController(controller.config)
        try:
            assert all(staged.exists() for _, staged in artifacts)
            assert entry.exists()
            if restart == 1:
                restarted.restore_database_backup(backup.name)
                assert restarted.repository.get_book(book.id) is not None
                assert source.read_bytes() == b"source bytes"
                assert audio.read_bytes() == b"audio bytes"
                assert not entry.exists()
        finally:
            restarted.repository.close()


def test_interrupted_quarantine_preserves_staged_files_until_backup_restore(controller, monkeypatch):
    from modules.storage.deletion import read_delete_transaction

    book, backup, source, audio, entry = stage_interrupted_deletion(controller, monkeypatch)
    _, artifacts = read_delete_transaction(
        entry, allowed_roots=(controller.config.output_dir, controller.config.temp_dir, source.parent),
    )
    controller.repository.close()
    controller.config.db_path.write_bytes(b"not a sqlite database")
    open_file = Path.open

    def fail_quarantine_status(path, *args, **kwargs):
        if path == controller.config.db_path.with_name("library.db.quarantine.json.tmp"):
            raise OSError("Disk full")
        return open_file(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", fail_quarantine_status)
        with pytest.raises(OSError, match="Disk full"):
            AppController(controller.config)

    restarted = AppController(controller.config)
    try:
        assert all(staged.exists() for _, staged in artifacts)
        assert entry.exists()
        restarted.restore_database_backup(backup.name)
        assert restarted.repository.get_book(book.id) is not None
        assert source.read_bytes() == b"source bytes"
        assert audio.read_bytes() == b"audio bytes"
        assert not entry.exists()
    finally:
        restarted.repository.close()


def test_quarantine_keeps_database_when_preserving_deletions_is_not_durable(controller, monkeypatch):
    stage_interrupted_deletion(controller, monkeypatch)
    controller.repository.close()
    controller.config.db_path.write_bytes(b"not a sqlite database")

    def fail_sync(path):
        raise OSError("Disk full")

    monkeypatch.setattr("modules.storage.deletion.sync_directory", fail_sync)
    with pytest.raises(OSError, match="Disk full"):
        AppController(controller.config)
    assert controller.config.db_path.read_bytes() == b"not a sqlite database"
    assert not list(controller.config.data_dir.glob("library.db.corrupt-*"))


@pytest.mark.parametrize("locked_path", ["quarantine", "staging"])
def test_restore_returns_committed_success_when_housekeeping_fails(controller, monkeypatch, caplog, locked_path):
    book, _ = saved_book(controller.repository)
    backup = backup_database(controller.config.db_path)
    controller.repository.update_book(book.id, title="Current book")
    marker = controller.config.db_path.with_name("library.db.quarantine.json")
    marker.write_text("{}", encoding="utf-8")
    unlink = Path.unlink

    def deny_housekeeping(path, *args, **kwargs):
        if (locked_path == "quarantine" and path == marker) or (
            locked_path == "staging" and path.name.startswith(".restore-")
        ):
            raise PermissionError("File in use")
        return unlink(path, *args, **kwargs)

    client = _make_client_with_controller(controller)
    before = controller.repository.library_generation
    monkeypatch.setattr(Path, "unlink", deny_housekeeping)
    response = client.post(f"/api/maintenance/backups/{backup.name}/restore")
    assert response.status_code == 200
    assert controller.repository.get_book(book.id).title == "Saved"
    assert controller.repository.library_generation == before + 1
    assert "File in use" in caplog.text


def test_diagnostics_identifies_library_replacement_and_backend_restart(controller):
    saved_book(controller.repository)
    backup = backup_database(controller.config.db_path)
    before = controller.get_storage_diagnostics()["library_generation"]
    controller.restore_database_backup(backup.name)
    after = controller.get_storage_diagnostics()["library_generation"]
    assert before != after
    assert controller.get_storage_diagnostics()["library_generation"] == after
    controller.repository.close()
    restarted = AppController(controller.config)
    try:
        assert restarted.get_storage_diagnostics()["library_generation"] != after
    finally:
        restarted.repository.close()


def test_diagnostics_waits_for_pending_restore_before_reporting_generation(controller, monkeypatch):
    from modules.app import controller as controller_module

    saved_book(controller.repository)
    backup = backup_database(controller.config.db_path)
    before = controller.get_storage_diagnostics()["library_generation"]
    entered = threading.Event()
    release = threading.Event()
    checking = threading.Event()
    restore = controller_module.restore_database_backup

    def delayed_restore(repository, backup_id):
        entered.set()
        assert release.wait(5)
        return restore(repository, backup_id)

    def read_generation():
        checking.set()
        return controller.get_storage_diagnostics()["library_generation"]

    monkeypatch.setattr(controller_module, "restore_database_backup", delayed_restore)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(controller.restore_database_backup, backup.name)
        try:
            assert entered.wait(5)
            diagnostics = pool.submit(read_generation)
            assert checking.wait(5)
            with pytest.raises(TimeoutError):
                diagnostics.result(timeout=0.05)
        finally:
            release.set()
        pending.result(timeout=5)
        assert diagnostics.result(timeout=5) != before


@pytest.mark.parametrize("committed", [False, True])
def test_restore_reconciles_staged_deletion_using_commit_evidence(controller, monkeypatch, committed):
    from modules.storage.deletion import read_delete_transaction

    empty_backup = backup_database(controller.config.db_path)
    book, backup, source, audio, entry = stage_interrupted_deletion(controller, monkeypatch)
    _, artifacts = read_delete_transaction(
        entry, allowed_roots=(controller.config.output_dir, controller.config.temp_dir, source.parent),
    )
    if committed:
        (entry / ".committed").touch()
        controller.restore_database_backup(backup.name)
        assert controller.repository.get_book(book.id) is not None
        assert source.read_bytes() == b"source bytes"
        assert audio.read_bytes() == b"audio bytes"
        assert all(not staged.exists() for _, staged in artifacts)
        assert not entry.exists()
    else:
        controller.restore_database_backup(empty_backup.name)
        controller._library_service.sweep_delete_staging()
        assert all(staged.exists() for _, staged in artifacts)
        assert entry.exists()
        restarted = AppController(controller.config)
        try:
            assert all(staged.exists() for _, staged in artifacts)
            assert entry.exists()
        finally:
            restarted.repository.close()
        controller.restore_database_backup(backup.name)
        assert source.read_bytes() == b"source bytes"
        assert audio.read_bytes() == b"audio bytes"
        assert not entry.exists()


def test_restore_keeps_staged_files_when_reconciliation_is_interrupted(controller, monkeypatch):
    from modules.storage.deletion import read_delete_transaction

    empty_backup = backup_database(controller.config.db_path)
    _, backup, source, audio, entry = stage_interrupted_deletion(controller, monkeypatch)
    _, artifacts = read_delete_transaction(
        entry, allowed_roots=(controller.config.output_dir, controller.config.temp_dir, source.parent),
    )

    def interrupt_reconciliation():
        raise KeyboardInterrupt()

    with monkeypatch.context() as patch:
        patch.setattr(controller._library_service, "sweep_delete_staging", interrupt_reconciliation)
        with pytest.raises(KeyboardInterrupt):
            controller.restore_database_backup(empty_backup.name)
    controller.repository.close()
    restarted = AppController(controller.config)
    try:
        assert all(staged.exists() for _, staged in artifacts)
        restarted.restore_database_backup(backup.name)
        assert source.read_bytes() == b"source bytes"
        assert audio.read_bytes() == b"audio bytes"
        assert not entry.exists()
    finally:
        restarted.repository.close()


def test_restore_recovers_pending_deleted_artifacts_referenced_by_restored_book(controller, monkeypatch):
    source = controller.config.data_dir / "sources" / "saved.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source bytes")
    audio = controller.config.output_dir / "Saved" / "chapters" / "01.mp3"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"audio bytes")
    book, _ = controller.repository.create_book_with_chapters(
        BookCreate(title="Saved", source_path=str(source), source_type=SourceType.TXT,
                   playback_path=str(audio)),
        [ChapterCreate(book_id=0, chapter_number=1, title="First", mp3_path=str(audio))],
    )
    backup = backup_database(controller.config.db_path)

    def fail_removal(artifacts):
        raise PermissionError("File is in use")

    with monkeypatch.context() as patch:
        patch.setattr("modules.app.services.library_service.remove_staged_artifacts", fail_removal)
        result = controller.delete_book(book.id)
    assert result["files_pending"] is True
    entry = controller.config.temp_dir / "delete_staging" / result["path"]
    assert (entry / ".committed").is_file()
    assert controller.repository.get_book(book.id) is None
    assert not audio.exists()
    assert not source.exists()

    controller.restore_database_backup(backup.name)

    assert controller._library_service.get_book_playback_file(book.id, 1) == audio
    assert audio.read_bytes() == b"audio bytes"
    assert source.read_bytes() == b"source bytes"
    assert not entry.exists()


def test_restore_preserves_pending_artifacts_when_original_path_is_occupied(controller, monkeypatch):
    from modules.storage.deletion import DELETE_PRESERVE_MARKER, read_delete_transaction

    _, backup, source, audio, entry = stage_interrupted_deletion(controller, monkeypatch)
    (entry / ".committed").touch()
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"newer audio")
    _, artifacts = read_delete_transaction(
        entry, allowed_roots=(controller.config.output_dir, controller.config.temp_dir, source.parent),
    )

    controller.restore_database_backup(backup.name)
    controller._library_service.sweep_delete_staging()

    assert audio.read_bytes() == b"newer audio"
    assert entry.exists()
    assert (entry / DELETE_PRESERVE_MARKER).exists()
    assert any(staged.exists() for _, staged in artifacts)
    assert source.read_bytes() == b"source bytes"


def test_sweep_does_not_restore_artifacts_for_book_id_with_different_paths(controller, monkeypatch):
    book, _, source, audio, _ = stage_interrupted_deletion(controller, monkeypatch)
    controller.repository.update_book(
        book.id, title="New Book", source_path=str(controller.config.data_dir / "sources" / "new.txt"),
        playback_path=str(controller.config.output_dir / "New Book" / "new.m4b"),
    )

    controller._library_service.sweep_delete_staging()

    assert not source.exists()
    assert not audio.exists()


def test_sweep_preserves_journal_when_live_references_cannot_be_read(controller, monkeypatch):
    from modules.storage.deletion import DELETE_PRESERVE_MARKER, read_delete_transaction

    _, _, source, _, entry = stage_interrupted_deletion(controller, monkeypatch)
    (entry / ".committed").touch()
    _, artifacts = read_delete_transaction(
        entry, allowed_roots=(controller.config.output_dir, controller.config.temp_dir, source.parent),
    )

    def fail_references(book_id):
        raise sqlite3.OperationalError("Database is unavailable")

    monkeypatch.setattr(controller.repository, "list_artifact_paths", fail_references)
    controller._library_service.sweep_delete_staging()

    assert all(staged.exists() for _, staged in artifacts)
    assert (entry / DELETE_PRESERVE_MARKER).exists()
