"""Library and storage behaviours: wire shapes, folders, staged deletions, backups."""

from __future__ import annotations

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import threading
from uuid import uuid4

import pytest

import modules.app.services.library_service as library_service_module
import modules.storage.maintenance as maintenance
import modules.storage.maintenance_reconcile as reconcile
from modules.app.config import AppConfig
from modules.app.controller import AppController
from modules.storage.deletion import LOCAL_DELETE_STAGING, write_delete_transaction
from modules.storage.models import BookCreate, ChapterCreate, PlaybackStatus, SourceType
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


def _source(controller, name: str, text: str) -> Path:
    source = controller.config.data_dir / "sources" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(text, encoding="utf-8")
    return source


def _chapter_book(controller, title: str, *, chapter_file: bool = True, source: Path | None = None):
    """A chapter-audio book with no recorded playback file."""
    folder = controller.config.output_dir / title
    (folder / "chapters").mkdir(parents=True)
    chapter = folder / "chapters" / "01_One.mp3"
    if chapter_file:
        chapter.write_bytes(b"chapter audio")
    source = source or _source(controller, f"{title}.txt", f"{title} text")
    book, _ = controller.repository.create_book_with_chapters(
        BookCreate(title=title, source_path=str(source), source_type=SourceType.TXT, total_chapters=1),
        [ChapterCreate(book_id=0, chapter_number=1, title="One", mp3_path=str(chapter))],
    )
    return book, folder


def _age(path: Path) -> None:
    for item in [path, *path.rglob("*")]:
        os.utime(item, (1, 1))


# ---------------------------------------------------------------- uploads


def test_uploads_report_existing_books_inside_the_preflight(controller) -> None:
    client = _make_client_with_controller(controller)
    source = _source(controller, "Ninety.txt", "Ninety text")
    book = controller.repository.create_book(
        BookCreate(title="Ninety", source_path=str(source), source_type=SourceType.TXT)
    )

    uploaded = client.post("/api/uploads/source", files={"file": ("copy.txt", b"Ninety text", "text/plain")})
    pasted = client.post("/api/uploads/text", json={"text": "Other words", "title": "Ninety"})

    assert uploaded.status_code == 200, uploaded.text
    assert pasted.status_code == 200, pasted.text
    same_file = [{"id": book.id, "title": "Ninety", "match": "same_file"}]
    same_title = [{"id": book.id, "title": "Ninety", "match": "same_title"}]
    assert uploaded.json()["preflight"]["existing_books"] == same_file
    assert pasted.json()["preflight"]["existing_books"] == same_title
    # Earlier callers read the top-level key.
    assert uploaded.json()["existing_books"] == same_file
    assert pasted.json()["existing_books"] == same_title


def test_pasted_text_falls_back_when_inspection_fails(controller, monkeypatch) -> None:
    service = controller._library_service

    def broken(_path):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(service, "preflight_source", broken)
    response = _make_client_with_controller(controller).post(
        "/api/uploads/text", json={"text": "Some words", "title": "Notes"},
    )

    assert response.status_code == 200, response.text
    preflight = response.json()["preflight"]
    assert preflight["title"] == "Notes"
    assert preflight["warnings"] and "parser exploded" in preflight["warnings"][0]
    stored = controller.config.temp_dir / "web_uploads" / response.json()["source_handle"].removeprefix("upload:")
    assert stored.is_file()


@pytest.mark.parametrize("kind", ["file", "text"])
def test_an_upload_that_fails_after_writing_leaves_no_file(controller, monkeypatch, kind) -> None:
    service = controller._library_service

    def failing(*_args, **_kwargs):
        raise RuntimeError("library unavailable")

    monkeypatch.setattr(service, "find_existing_books", failing)
    with pytest.raises(RuntimeError, match="library unavailable"):
        if kind == "file":
            import io

            service.upload_source(filename="a.txt", file_obj=io.BytesIO(b"words"))
        else:
            service.upload_text_source(text="words", title="A")

    uploads = controller.config.temp_dir / "web_uploads"
    assert (list(uploads.iterdir()) if uploads.exists() else []) == []


def test_uploads_sweep_stale_uploads_at_most_once_per_interval(controller, monkeypatch) -> None:
    service = controller._library_service
    calls = []
    monkeypatch.setattr(service, "sweep_stale_uploads", lambda *args: calls.append(1) or 0)
    service._last_upload_sweep = None

    service.upload_text_source(text="one", title="One")
    service.upload_text_source(text="two", title="Two")
    assert len(calls) == 1

    service._last_upload_sweep -= library_service_module.UPLOAD_SWEEP_INTERVAL_SECONDS
    service.upload_text_source(text="three", title="Three")
    assert len(calls) == 2


def test_existing_book_lookup_does_not_aggregate_chapters(controller, monkeypatch) -> None:
    source = _source(controller, "Ninety.txt", "Ninety text")
    book = controller.repository.create_book(
        BookCreate(title="Ninety", source_path=str(source), source_type=SourceType.TXT)
    )
    monkeypatch.setattr(controller.repository, "list_books", lambda *_: pytest.fail("summary query used"))

    assert controller.find_existing_books(source, "ninety") == [
        {"id": book.id, "title": "Ninety", "match": "same_file"},
    ]


# ---------------------------------------------------------------- library reads


def test_library_list_and_detail_agree_on_missing_chapter_audio(controller) -> None:
    client = _make_client_with_controller(controller)
    playable, _ = _chapter_book(controller, "Playable")
    missing, _ = _chapter_book(controller, "Missing", chapter_file=False)

    items = {item["id"]: item for item in client.get("/api/library").json()["items"]}

    for book in (playable, missing):
        detail = client.get(f"/api/library/{book.id}").json()
        assert items[book.id]["audio_available"] is detail["audio_available"]
    assert items[playable.id]["audio_available"] is True
    assert items[missing.id]["audio_available"] is False
    assert client.get(f"/api/library/{missing.id}/audio").status_code == 404


def test_library_list_checks_files_without_the_repository_lock(controller, monkeypatch) -> None:
    book, _ = _chapter_book(controller, "Playable")
    service = controller._library_service
    real_exists = service._source_path_exists
    saved = threading.Event()
    saved_during_checks = []

    def exists_while_saving(raw):
        writer = threading.Thread(
            target=lambda: (controller.update_book_playback_position(book.id, 5.0), saved.set()),
            daemon=True,
        )
        writer.start()
        saved_during_checks.append(saved.wait(3))
        return real_exists(raw)

    monkeypatch.setattr(service, "_source_path_exists", exists_while_saving)
    payload = _make_client_with_controller(controller).get("/api/library").json()

    assert saved_during_checks == [True], "a playback save waited for the library's file checks"
    assert payload["library_epoch"] == controller.repository.get_library_identity()["library_epoch"]
    assert [item["library_epoch"] for item in payload["items"]] == [payload["library_epoch"]]


def test_library_list_reads_again_when_the_library_is_replaced_meanwhile() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from modules.web.deps import get_controller
    from modules.web.routes.library import router

    epochs = iter(["first", "second", "second", "second"])
    current = {"epoch": next(epochs)}
    reads = []

    class Controller:
        @contextmanager
        def library_snapshot(self, expected_epoch=None, *, mutation=False):
            yield {"library_id": "library", "library_epoch": current["epoch"]}

        def get_library_books(self, **_filters):
            reads.append(current["epoch"])
            # A restore lands while the first read checks its files.
            current["epoch"] = next(epochs)
            return []

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_controller] = Controller
    payload = TestClient(app).get("/api/library").json()

    assert reads == ["first", "second"]
    assert payload["library_epoch"] == "second"


def test_metadata_update_returns_the_book_detail(controller) -> None:
    client = _make_client_with_controller(controller)
    book, _ = _chapter_book(controller, "Chapters Only")
    epoch = controller.repository.get_library_identity()["library_epoch"]

    patched = client.patch(f"/api/library/{book.id}", json={"title": "Renamed", "expected_epoch": epoch})
    detail = client.get(f"/api/library/{book.id}")

    assert patched.status_code == 200, patched.text
    assert patched.json() == detail.json()
    assert patched.json()["title"] == "Renamed"
    assert patched.json()["audio_available"] is True
    assert patched.json()["completed_chapters"] == 1
    assert len(patched.json()["chapters"]) == 1
    assert patched.json()["playback_url"] == f"/api/library/{book.id}/audio"


def test_open_folder_never_opens_the_internal_sources_folder(controller) -> None:
    opened = []
    controller._library_service._folder_opener = opened.append
    source = _source(controller, "Kept.txt", "Kept text")
    book = controller.repository.create_book(
        BookCreate(title="Kept", source_path=str(source), source_type=SourceType.TXT)
    )

    folder = controller.open_book_folder(book.id)

    assert folder == controller.config.output_dir / "Kept"
    assert opened == [folder]
    assert folder.is_dir()


def test_restore_report_lists_missing_audio_without_per_book_queries(controller, monkeypatch) -> None:
    playable, _ = _chapter_book(controller, "Playable")
    missing, _ = _chapter_book(controller, "Missing", chapter_file=False)
    monkeypatch.setattr(
        controller.repository, "get_book_with_chapters", lambda *_: pytest.fail("per-book query"),
    )

    files = controller._library_service.books_with_missing_files()

    assert files["books"] == 2
    assert files["missing_audio"] == [{"id": missing.id, "title": "Missing"}]
    assert files["missing_source"] == []
    assert playable.id not in [entry["id"] for entry in files["missing_audio"]]


# ---------------------------------------------------------------- staged deletions


def _staged_deletion(controller, book_id: int, folder: Path) -> tuple[Path, Path]:
    entry = controller.config.temp_dir / "delete_staging" / uuid4().hex
    entry.mkdir(parents=True)
    original = folder.resolve()
    staged = original.parent / LOCAL_DELETE_STAGING / entry.name / f"0-{original.name}"
    write_delete_transaction(entry, book_id, [(original, staged)])
    staged.parent.mkdir(parents=True)
    os.replace(original, staged)
    return entry, staged


def test_open_pending_deletion_folder_opens_the_staged_files(controller, monkeypatch) -> None:
    opened = []
    monkeypatch.setattr(library_service_module, "_default_folder_opener", opened.append)
    folder = controller.config.output_dir / "Locked Book"
    folder.mkdir(parents=True)
    (folder / "book.m4b").write_bytes(b"audio")
    entry, staged = _staged_deletion(controller, 12345, folder)
    client = _make_client_with_controller(controller)

    response = client.post("/api/maintenance/delete-staging/open-folder", json={"path": entry.name})

    assert response.status_code == 200, response.text
    assert opened == [staged.parent]


def test_open_pending_deletion_folder_falls_back_to_its_record(controller, monkeypatch) -> None:
    opened = []
    monkeypatch.setattr(library_service_module, "_default_folder_opener", opened.append)
    entry = controller.config.temp_dir / "delete_staging" / uuid4().hex
    entry.mkdir(parents=True)
    (entry / "transaction.json").write_text(json.dumps({
        "book_id": 1,
        "artifacts": [{"original": "/etc/passwd", "staged": f"/etc/{LOCAL_DELETE_STAGING}/{entry.name}/0-passwd"}],
    }))

    response = _make_client_with_controller(controller).post(
        "/api/maintenance/delete-staging/open-folder", json={"path": entry.name},
    )

    assert response.status_code == 200
    assert opened == [entry]


def test_cleanup_settles_an_old_deletion_that_was_never_marked_committed(controller) -> None:
    folder = controller.config.output_dir / "Gone"
    folder.mkdir(parents=True)
    (folder / "book.m4b").write_bytes(b"audio")
    # The row is already gone, but making the deletion durable failed.
    entry, staged = _staged_deletion(controller, 12345, folder)
    _age(entry)

    diagnostics = controller.get_storage_diagnostics()
    assert diagnostics["cleanup_preview"]["removable"]["pending_deletions"] == 1

    report = controller.cleanup_storage(confirm="cleanup")

    assert report["actions"]["deleted_delete_staging_dirs"] == [str(entry)]
    assert not staged.exists()
    assert not entry.exists()


def test_cleanup_restores_an_uncommitted_deletion_whose_book_still_exists(controller) -> None:
    folder = controller.config.output_dir / "Kept"
    folder.mkdir(parents=True)
    (folder / "book.m4b").write_bytes(b"audio")
    source = _source(controller, "Kept.txt", "Kept text")
    book = controller.repository.create_book(BookCreate(
        title="Kept", source_path=str(source), source_type=SourceType.TXT,
        playback_path=str(folder / "book.m4b"), playback_status=PlaybackStatus.PACKAGED,
    ))
    entry, staged = _staged_deletion(controller, book.id, folder)
    _age(entry)

    controller.cleanup_storage(confirm="cleanup")

    assert (folder / "book.m4b").read_bytes() == b"audio"
    assert not staged.exists()
    assert controller.repository.get_book(book.id) is not None


# ---------------------------------------------------------------- quarantine and restore


def test_quarantine_moves_a_rollback_journal_with_the_damaged_database(tmp_path, monkeypatch) -> None:
    db = tmp_path / "data" / "library.db"
    SQLiteRepository(db).close()
    db.write_bytes(b"not a sqlite database" * 64)
    journal = db.with_name(db.name + "-journal")
    import modules.storage.sqlite_repo as sqlite_repo_module

    real_preserve = sqlite_repo_module.preserve_delete_transactions

    def preserve_with_journal(root):
        # SQLite itself replays or discards a journal it finds while the
        # database is open; this one appears after the connection closed.
        journal.write_bytes(b"journal of the damaged database")
        return real_preserve(root)

    monkeypatch.setattr(sqlite_repo_module, "preserve_delete_transactions", preserve_with_journal)
    repo = SQLiteRepository(db)
    try:
        quarantined = Path(repo.quarantine["path"])
        assert Path(str(quarantined) + "-journal").read_bytes() == b"journal of the damaged database"
        assert not journal.exists()
        assert repo.count_books() == 0
    finally:
        repo.close()


def test_quarantine_resumes_intents_written_before_journals_were_moved(tmp_path) -> None:
    db = tmp_path / "data" / "library.db"
    SQLiteRepository(db).close()
    db.write_bytes(b"damaged")
    for suffix in ("-wal", "-shm"):
        db.with_name(db.name + suffix).unlink(missing_ok=True)
    destination = db.with_name(db.name + ".corrupt-20260101_000000")
    intent = {
        "version": 1, "phase": "moving",
        "files": [
            {"source": str(db.with_name(db.name + suffix)), "destination": str(Path(str(destination) + suffix)),
             "required": suffix == ""}
            for suffix in ("", "-wal", "-shm")
        ],
        "status": {"path": str(destination), "created_at": "2026-01-01T00:00:00Z"},
    }
    db.with_name(db.name + ".quarantine-intent.json").write_text(json.dumps(intent))

    repo = SQLiteRepository(db)
    try:
        assert destination.read_bytes() == b"damaged"
        assert repo.quarantine["path"] == str(destination)
        assert repo.count_books() == 0
    finally:
        repo.close()


def test_restore_removes_a_rollback_journal_before_publishing_the_backup(controller, monkeypatch) -> None:
    controller.repository.create_book(BookCreate(title="Saved", source_path="a.txt", source_type=SourceType.TXT))
    backup = controller.backup_database()
    db = controller.repository.db_path
    journal = db.with_name(db.name + "-journal")
    seen = []
    real_sync = maintenance.sync_file
    real_preserve = maintenance.preserve_delete_transactions

    def preserve_with_journal(root):
        # Appears once the live database has left WAL mode, as another
        # process's interrupted write would leave it.
        journal.write_bytes(b"journal of the replaced database")
        return real_preserve(root)

    monkeypatch.setattr(maintenance, "preserve_delete_transactions", preserve_with_journal)

    def record(path):
        if Path(path).name.startswith(".restore-"):
            seen.append(journal.exists())
        return real_sync(path)

    monkeypatch.setattr(maintenance, "sync_file", record)
    controller.restore_database_backup(backup.name)

    assert seen == [False]
    assert [book.title for book in controller.repository.list_books()] == ["Saved"]


# ---------------------------------------------------------------- backups


def test_failed_backup_leaves_no_partial_backup(controller, monkeypatch) -> None:
    backups = controller.config.db_path.parent / "backups"

    def fail(_path):
        raise OSError("disk full")

    monkeypatch.setattr(maintenance, "_remove_backup_sidecars", fail)
    with pytest.raises(OSError, match="disk full"):
        maintenance.backup_database(controller.config.db_path)

    left = [path.name for path in backups.iterdir() if not path.name.startswith(".orpheus-")] if backups.exists() else []
    assert left == []
    assert maintenance.list_database_backups(controller.config.db_path) == []


def test_backups_in_progress_are_not_listed_and_old_ones_are_removed_at_startup(controller, monkeypatch) -> None:
    import time

    db = controller.repository.db_path
    backups = db.parent / "backups"
    backups.mkdir(exist_ok=True)
    interrupted = backups / f".{db.stem}_20200101_000000.abcd1234{db.suffix}.tmp"
    interrupted.write_bytes(b"half a database")
    sidecar = Path(str(interrupted) + "-journal")
    sidecar.write_bytes(b"journal")
    unrelated = backups / "notes.tmp"
    unrelated.write_bytes(b"not ours")

    assert maintenance.list_database_backups(db) == []
    # Written after this process started: possibly a copy in progress.
    monkeypatch.setattr(maintenance, "_RESTORE_PROCESS_STARTED_AT", time.time() - 60)
    maintenance.recover_restore_artifacts(controller.repository)
    assert interrupted.exists() and sidecar.exists()

    monkeypatch.setattr(maintenance, "_RESTORE_PROCESS_STARTED_AT", time.time() + 60)
    maintenance.recover_restore_artifacts(controller.repository)
    assert not interrupted.exists() and not sidecar.exists()
    assert unrelated.exists()


def test_backup_listing_reads_each_backup_once(controller, monkeypatch) -> None:
    controller.repository.create_book(BookCreate(title="Saved", source_path="a.txt", source_type=SourceType.TXT))
    controller.backup_database()
    opened = []
    real_uri = maintenance.readonly_sqlite_uri
    monkeypatch.setattr(maintenance, "readonly_sqlite_uri", lambda path: opened.append(path) or real_uri(path))

    first = maintenance.list_database_backups(controller.config.db_path)
    second = maintenance.list_database_backups(controller.config.db_path)

    assert first == second
    assert [item["book_count"] for item in first] == [1]
    assert len(opened) == 1


# ---------------------------------------------------------------- maintenance passes


def test_one_audit_pass_reports_both_graced_and_unowned_folders(controller) -> None:
    out = controller.config.output_dir
    (out / "Recent").mkdir(parents=True)
    (out / "Recent" / "notes.txt").write_text("x")
    (out / "Old").mkdir()
    (out / "Old" / "notes.txt").write_text("x")
    _age(out / "Old")
    args = (controller.config.db_path, out, controller.config.temp_dir)

    audit, unowned = maintenance.audit_with_unowned_folders(*args)

    assert audit == maintenance.audit_storage_state(*args)
    assert unowned == maintenance.unowned_output_folders(*args)
    assert [Path(raw).name for raw in audit["orphan_output_dirs"]] == ["Old"]
    assert maintenance.storage_cleanup_summary(*args, audit, unowned=unowned) == (
        maintenance.storage_cleanup_summary(*args, audit)
    )


def test_fileless_book_settlement_reads_chapters_in_one_query(controller) -> None:
    repo = controller.repository
    out = controller.config.output_dir
    playable, _ = _chapter_book(controller, "Playable")
    relinked, relinked_folder = _chapter_book(controller, "Relinked", chapter_file=False)
    (relinked_folder / "book.m4b").write_bytes(b"packaged")
    fileless, _ = _chapter_book(controller, "Fileless", chapter_file=False)
    (out / "Fileless").rename(out / "Elsewhere")  # nothing left to relink

    statements = []
    with closing(sqlite3.connect(repo.db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.set_trace_callback(statements.append)
        workspace = maintenance.workspace_root_for_db(repo.db_path)
        settled = maintenance._plan_fileless_book_rows(
            maintenance._read_prune_rows(conn), set(), workspace, out, controller.config.temp_dir,
        )
        conn.rollback()

    assert [row["book_id"] for row in settled["reconciled_playback"]] == [relinked.id]
    assert settled["reconciled_playback"][0]["playback_path"] == str((relinked_folder / "book.m4b").resolve())
    assert playable.id not in [book["id"] for book in settled["removable"]]
    assert settled["removable"] == [{"id": fileless.id, "title": "Fileless"}]
    # One read of every chapter path serves the unclaimed-audio check too.
    assert sum("FROM chapters" in statement for statement in statements) == 1


def test_library_service_walks_managed_roots_once_when_constructed(controller, monkeypatch) -> None:
    import modules.storage.managed_roots as managed_roots

    calls = []
    real_claim = managed_roots.claim_managed_roots
    monkeypatch.setattr(managed_roots, "claim_managed_roots", lambda *args, **kw: calls.append(args) or real_claim(*args, **kw))
    library_service_module.LibraryService(
        controller.repository, output_dir=controller.config.output_dir, temp_dir=controller.config.temp_dir,
        storage_root=maintenance.workspace_root_for_db(controller.config.db_path),
    )

    assert len(calls) == 1
    assert {Path(root) for root in calls[0][1:]} == {
        controller.config.output_dir, controller.config.temp_dir,
        (controller.config.db_path.parent / "sources").resolve(),
    }


def test_moving_an_upload_copies_it_before_taking_the_write_lock(controller, monkeypatch) -> None:
    repo = controller.repository
    upload = controller.config.temp_dir / "web_uploads" / "book.txt"
    upload.parent.mkdir(parents=True, exist_ok=True)
    upload.write_text("upload text")
    book = repo.create_book(BookCreate(title="Uploaded", source_path=str(upload), source_type=SourceType.TXT))
    real_copy = reconcile.copy2
    writable = []

    def copy_while_writing(source, destination):
        with closing(sqlite3.connect(repo.db_path, timeout=0)) as other:
            try:
                other.execute("BEGIN IMMEDIATE")
                other.rollback()
                writable.append(True)
            except sqlite3.OperationalError:
                writable.append(False)
        return real_copy(source, destination)

    monkeypatch.setattr(reconcile, "copy2", copy_while_writing)
    db = repo.db_path
    moved = reconcile._move_referenced_upload_sources(
        db_file=db, workspace_root=maintenance.workspace_root_for_db(db),
        output_root=controller.config.output_dir, temp_root=controller.config.temp_dir,
        canonical_sources_dir=maintenance.canonical_sources_dir(db),
    )

    assert writable == [True]
    destination = maintenance.canonical_sources_dir(db) / "book.txt"
    assert moved == [{"book_id": book.id, "from": str(upload.resolve()), "to": str(destination)}]
    assert destination.read_text() == "upload text"
    assert repo.get_book(book.id).source_path == str(destination)


def test_moving_an_upload_discards_its_copy_when_the_owner_changed_meanwhile(controller, monkeypatch) -> None:
    repo = controller.repository
    upload = controller.config.temp_dir / "web_uploads" / "book.txt"
    upload.parent.mkdir(parents=True, exist_ok=True)
    upload.write_text("upload text")
    book = repo.create_book(BookCreate(title="Uploaded", source_path=str(upload), source_type=SourceType.TXT))
    elsewhere = _source(controller, "elsewhere.txt", "other")
    real_copy = reconcile.copy2

    def copy_then_repoint(source, destination):
        result = real_copy(source, destination)
        repo.update_book(book.id, source_path=str(elsewhere))
        return result

    monkeypatch.setattr(reconcile, "copy2", copy_then_repoint)
    db = repo.db_path
    moved = reconcile._move_referenced_upload_sources(
        db_file=db, workspace_root=maintenance.workspace_root_for_db(db),
        output_root=controller.config.output_dir, temp_root=controller.config.temp_dir,
        canonical_sources_dir=maintenance.canonical_sources_dir(db),
    )

    assert moved == []
    assert upload.read_text() == "upload text"
    assert not (maintenance.canonical_sources_dir(db) / "book.txt").exists()
    assert repo.get_book(book.id).source_path == str(elsewhere)



def test_moving_an_upload_discards_its_copy_when_the_upload_changed_meanwhile(controller, monkeypatch) -> None:
    repo = controller.repository
    upload = controller.config.temp_dir / "web_uploads" / "book.txt"
    upload.parent.mkdir(parents=True, exist_ok=True)
    upload.write_text("upload text")
    book = repo.create_book(BookCreate(title="Uploaded", source_path=str(upload), source_type=SourceType.TXT))
    real_copy = reconcile.copy2

    def copy_then_append(source, destination):
        result = real_copy(source, destination)
        with open(source, "a", encoding="utf-8") as stream:
            stream.write(" and more")
        return result

    monkeypatch.setattr(reconcile, "copy2", copy_then_append)
    db = repo.db_path
    moved = reconcile._move_referenced_upload_sources(
        db_file=db, workspace_root=maintenance.workspace_root_for_db(db),
        output_root=controller.config.output_dir, temp_root=controller.config.temp_dir,
        canonical_sources_dir=maintenance.canonical_sources_dir(db),
    )

    assert moved == []
    assert upload.read_text() == "upload text and more"
    assert not (maintenance.canonical_sources_dir(db) / "book.txt").exists()
    assert repo.get_book(book.id).source_path == str(upload)


# ------------------------------------------------- managed-root ownership walks


def _plant_foreign_owner(tmp_path: Path, directory: Path) -> Path:
    """Make ``directory`` look like a root another library owns."""
    from modules.storage.managed_roots import STORAGE_OWNER_NAME

    other = tmp_path / "other-library" / "other.db"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.touch()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / STORAGE_OWNER_NAME).write_text(
        json.dumps({"version": 1, "database": str(other.resolve())}), encoding="utf-8",
    )
    (directory / "their.m4b").write_bytes(b"another library's audio")
    return directory


@contextmanager
def _recorded_ownership_walks(monkeypatch):
    import modules.storage.managed_roots as managed_roots

    tops: list[Path] = []
    real_walk = managed_roots.os.walk

    def walk(top, *args, **kwargs):
        tops.append(Path(top))
        return real_walk(top, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(managed_roots.os, "walk", walk)
        yield tops


def test_opening_a_library_walks_each_managed_root_once(controller, monkeypatch) -> None:
    config = controller.config
    controller.repository.close()
    roots = [(config.data_dir / "sources").resolve(), config.output_dir, config.temp_dir]

    with _recorded_ownership_walks(monkeypatch) as tops:
        reopened = AppController(config)
    try:
        assert sorted(map(str, tops)) == sorted(map(str, roots))
    finally:
        reopened.repository.close()


def test_restore_relies_on_its_own_claim_and_still_reports_foreign_storage(controller, tmp_path, monkeypatch) -> None:
    from modules.storage.managed_roots import StorageOwnershipError

    backup = maintenance.backup_database(controller.config.db_path)
    with _recorded_ownership_walks(monkeypatch) as tops:
        controller.restore_database_backup(backup.name)
    # The restore's claim walked each root once; the sweep after it did not repeat that.
    assert len(tops) == 3

    _plant_foreign_owner(tmp_path, controller.config.output_dir / "Foreign")
    with pytest.raises(StorageOwnershipError):
        controller.restore_database_backup(backup.name)


def _deletable_book(controller, title: str = "Doomed"):
    folder = controller.config.output_dir / title
    (folder / "chapters").mkdir(parents=True)
    audio = folder / "book.m4b"
    audio.write_bytes(b"audio")
    book = controller.repository.create_book(BookCreate(
        title=title, source_path=str(_source(controller, f"{title}.txt", "text")),
        source_type=SourceType.TXT, playback_path=str(audio),
    ))
    return book, folder


def test_book_deletion_refuses_another_librarys_root_inside_the_book(controller, tmp_path) -> None:
    from modules.storage.managed_roots import StorageOwnershipError

    book, folder = _deletable_book(controller)
    foreign = _plant_foreign_owner(tmp_path, folder / "chapters" / "nested")

    with pytest.raises(StorageOwnershipError):
        controller.delete_book(book.id)

    assert (foreign / "their.m4b").read_bytes() == b"another library's audio"
    assert (folder / "book.m4b").read_bytes() == b"audio"
    assert controller.repository.get_book(book.id) is not None
    assert not any((controller.config.temp_dir / "delete_staging").iterdir())


def test_book_deletion_does_not_follow_a_link_to_another_librarys_root(controller, tmp_path) -> None:
    book, folder = _deletable_book(controller)
    foreign = _plant_foreign_owner(tmp_path, tmp_path / "elsewhere" / "their-output")
    (folder / "chapters" / "link").symlink_to(foreign, target_is_directory=True)

    assert controller.delete_book(book.id) is True

    assert not folder.exists()
    assert (foreign / "their.m4b").read_bytes() == b"another library's audio"


def test_book_deletion_through_a_linked_book_folder_leaves_the_target_alone(controller, tmp_path) -> None:
    book, folder = _deletable_book(controller)
    foreign = _plant_foreign_owner(tmp_path, tmp_path / "elsewhere" / "their-output")
    (foreign / "book.m4b").write_bytes(b"their copy")
    (folder / "book.m4b").unlink()
    (folder / "chapters").rmdir()
    folder.rmdir()
    folder.symlink_to(foreign, target_is_directory=True)

    controller.delete_book(book.id)

    assert (foreign / "their.m4b").read_bytes() == b"another library's audio"
    assert (foreign / "book.m4b").read_bytes() == b"their copy"
    assert controller.repository.get_book(book.id) is None


def test_book_deletion_walks_only_what_it_removes(controller, tmp_path, monkeypatch) -> None:
    book, folder = _deletable_book(controller)
    # Another library's root elsewhere in the output folder is out of this deletion's reach.
    unrelated = _plant_foreign_owner(tmp_path, controller.config.output_dir / "Unrelated")
    roots = {(controller.config.data_dir / "sources").resolve(), controller.config.output_dir, controller.config.temp_dir}

    with _recorded_ownership_walks(monkeypatch) as tops:
        assert controller.delete_book(book.id) is True

    assert tops and not roots & set(tops)
    assert folder.resolve() in tops
    assert not folder.exists()
    assert (unrelated / "their.m4b").exists()


# ------------------------------------------------------------ storage cleanup


@contextmanager
def _counted_audits(monkeypatch):
    import modules.storage.maintenance_audit as maintenance_audit

    calls: list[float] = []
    real = maintenance_audit.audit_storage_state_with_unowned

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(maintenance_audit, "audit_storage_state_with_unowned", counted)
        patch.setattr(maintenance, "audit_storage_state_with_unowned", counted)
        yield calls


def test_storage_diagnostics_audit_the_storage_once(controller, monkeypatch) -> None:
    (controller.config.output_dir / "Leftover").mkdir(parents=True)

    with _counted_audits(monkeypatch) as calls:
        diagnostics = controller.get_storage_diagnostics()

    assert len(calls) == 1
    kept = {entry["category"]: entry for entry in diagnostics["cleanup_preview"]["kept"]}
    assert kept["unowned_output_folder"]["items"][0]["name"] == "Leftover"


def test_storage_cleanup_audits_before_during_and_after_only(controller, monkeypatch) -> None:
    (controller.config.output_dir / "Leftover").mkdir(parents=True)

    with _counted_audits(monkeypatch) as calls:
        report = controller.cleanup_storage(confirm="cleanup")

    # Before acting, before removing files, and once for the report and its summary.
    assert len(calls) == 3
    assert report["summary"]["kept"]


def _fileless_book(controller, title: str) -> int:
    """A book with neither audio nor a source to rebuild it from."""
    return controller.repository.create_book(BookCreate(
        title=title, source_path=str(controller.config.data_dir / "sources" / f"{title} (gone).txt"),
        source_type=SourceType.TXT, playback_path=str(controller.config.output_dir / title / "gone.m4b"),
    )).id


def _relocated_upload_book(controller, title: str) -> tuple[int, Path]:
    """A book whose missing upload has a verified copy in the sources folder."""
    import hashlib

    upload = (controller.config.temp_dir / "web_uploads" / f"{title}.txt").resolve()
    copy = _source(controller, f"{title}.txt", "upload bytes")
    record = reconcile._source_relocations_path(controller.repository.db_path)
    records = json.loads(record.read_text()) if record.exists() else {}
    records[os.path.normcase(str(upload))] = {
        "name": copy.name, "sha256": hashlib.sha256(copy.read_bytes()).hexdigest(),
    }
    record.write_text(json.dumps(records))
    book = controller.repository.create_book(BookCreate(
        title=title, source_path=str(upload), source_type=SourceType.TXT,
        playback_path=str(controller.config.output_dir / title / "gone.m4b"),
    ))
    return book.id, copy


def _prune(controller):
    return maintenance.prune_stale_records_and_files(
        controller.config.db_path, controller.config.output_dir, controller.config.temp_dir,
        aggressive=True, grace_seconds=0,
    )


def test_cleanup_preview_names_exactly_the_books_cleanup_removes(controller) -> None:
    out = controller.config.output_dir
    playable, _ = _chapter_book(controller, "Playable")
    relinked, relinked_folder = _chapter_book(controller, "Relinked", chapter_file=False)
    (relinked_folder / "book.m4b").write_bytes(b"packaged")
    titled = controller.repository.create_book(BookCreate(
        title="Titled", source_path=str(_source(controller, "Titled.txt", "t")), source_type=SourceType.TXT,
    ))
    (out / "Titled").mkdir()
    (out / "Titled" / "book.m4b").write_bytes(b"packaged")
    relocated, copy = _relocated_upload_book(controller, "Relocated")
    first = _fileless_book(controller, "Fileless One")
    second = _fileless_book(controller, "Fileless Two")
    audit = maintenance.audit_storage_state(
        controller.config.db_path, out, controller.config.temp_dir, grace_seconds=0,
    )

    preview = maintenance._books_cleanup_would_remove(
        controller.config.db_path, out, controller.config.temp_dir, audit,
    )
    report = _prune(controller)

    assert preview == [{"id": first, "title": "Fileless One"}, {"id": second, "title": "Fileless Two"}]
    assert report["actions"]["deleted_books"] == preview
    repo = controller.repository
    assert repo.get_book(playable.id) is not None
    assert repo.get_book(relinked.id).playback_path == str((relinked_folder / "book.m4b").resolve())
    assert repo.get_book(titled.id).playback_path == str((out / "Titled" / "book.m4b").resolve())
    assert repo.get_book(relocated).source_path == str(copy)
    assert relocated in report["actions"]["preserved_book_rows"]


def test_cleanup_scans_and_hashes_without_holding_the_write_lock(controller, monkeypatch) -> None:
    _relocated_upload_book(controller, "Relocated")
    doomed = _fileless_book(controller, "Fileless")
    writable: list[bool] = []
    applying: list[bool] = []
    real_apply = maintenance._apply_prune_plan

    def apply(*args, **kwargs):
        # Inside the write lock only cheap re-checks of the plan run.
        applying.append(True)
        try:
            return real_apply(*args, **kwargs)
        finally:
            applying.clear()

    monkeypatch.setattr(maintenance, "_apply_prune_plan", apply)

    def try_to_write():
        if applying:
            return
        with closing(sqlite3.connect(controller.repository.db_path, timeout=0)) as other:
            try:
                other.execute("BEGIN IMMEDIATE")
                other.rollback()
                writable.append(True)
            except sqlite3.OperationalError:
                writable.append(False)

    real_digest, real_search = reconcile._source_digest, maintenance._find_ondisk_playback_artifact
    monkeypatch.setattr(reconcile, "_source_digest", lambda path: try_to_write() or real_digest(path))
    monkeypatch.setattr(
        maintenance, "_find_ondisk_playback_artifact", lambda **kwargs: try_to_write() or real_search(**kwargs),
    )

    report = _prune(controller)

    assert len(writable) >= 3 and all(writable), writable
    assert report["actions"]["deleted_book_rows"] == [doomed]


@pytest.mark.parametrize("changed", ["same_book", "other_book"])
def test_cleanup_keeps_books_whose_rows_changed_while_it_was_deciding(controller, monkeypatch, changed) -> None:
    doomed = _fileless_book(controller, "Fileless")
    bystander = _fileless_book(controller, "Bystander") if changed == "other_book" else doomed
    found = controller.config.output_dir / "Found" / "book.m4b"
    found.parent.mkdir(parents=True)
    found.write_bytes(b"packaged")
    real_search = maintenance._find_ondisk_playback_artifact
    edited: list[bool] = []

    def search_then_edit(**kwargs):
        result = real_search(**kwargs)
        if not edited:
            # A playback relink committed by someone else after this cleanup read the rows.
            controller.repository.update_book(bystander, playback_path=str(found))
            edited.append(True)
        return result

    monkeypatch.setattr(maintenance, "_find_ondisk_playback_artifact", search_then_edit)

    report = _prune(controller)

    assert edited
    assert report["actions"]["deleted_book_rows"] == []
    assert controller.repository.get_book(doomed) is not None
    assert controller.repository.get_book(bystander).playback_path == str(found)


def test_cleanup_skips_a_source_repair_whose_row_changed_meanwhile(controller, monkeypatch) -> None:
    book_id, copy = _relocated_upload_book(controller, "Relocated")
    elsewhere = _source(controller, "elsewhere.txt", "other")
    real_digest = reconcile._source_digest

    def digest_then_repoint(path):
        result = real_digest(path)
        if path == copy:
            controller.repository.update_book(book_id, source_path=str(elsewhere))
        return result

    monkeypatch.setattr(reconcile, "_source_digest", digest_then_repoint)

    report = _prune(controller)

    assert controller.repository.get_book(book_id).source_path == str(elsewhere)
    assert copy.read_text() == "upload bytes"
    assert book_id not in report["actions"]["deleted_book_rows"]


def test_cleanup_keeps_fileless_books_when_unclaimed_audio_appears_before_it_deletes(controller, monkeypatch) -> None:
    doomed = _fileless_book(controller, "Fileless")
    renamed = controller.config.output_dir / "Renamed Book"
    real_apply = maintenance._apply_prune_plan

    def apply_after_a_folder_lands(*args, **kwargs):
        # A renamed audiobook folder appears after planning, before the write lock.
        renamed.mkdir(parents=True)
        (renamed / "book.m4b").write_bytes(b"packaged")
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(maintenance, "_apply_prune_plan", apply_after_a_folder_lands)

    report = _prune(controller)

    assert report["actions"]["deleted_book_rows"] == []
    assert report["actions"]["book_rows_kept_for_unclaimed_audio"] == [doomed]
    assert report["actions"]["unclaimed_audio_dirs"] == [str(renamed.resolve())]
    assert controller.repository.get_book(doomed) is not None
    assert (renamed / "book.m4b").exists()


def test_cleanup_keeps_a_fileless_book_whose_source_copy_appears_before_it_deletes(controller, monkeypatch) -> None:
    upload = (controller.config.temp_dir / "web_uploads" / "Lost.txt").resolve()
    doomed = controller.repository.create_book(BookCreate(
        title="Lost", source_path=str(upload), source_type=SourceType.TXT,
        playback_path=str(controller.config.output_dir / "Lost" / "gone.m4b"),
    )).id
    real_apply = maintenance._apply_prune_plan

    def apply_after_a_copy_lands(*args, **kwargs):
        _source(controller, "Lost.txt", "upload bytes")
        return real_apply(*args, **kwargs)

    monkeypatch.setattr(maintenance, "_apply_prune_plan", apply_after_a_copy_lands)

    report = _prune(controller)

    assert report["actions"]["deleted_book_rows"] == []
    assert doomed in report["actions"]["preserved_book_rows"]
    assert controller.repository.get_book(doomed) is not None


def test_a_failed_storage_claim_leaves_no_earlier_claim_to_rely_on(tmp_path, monkeypatch) -> None:
    import modules.storage.sqlite_repo as sqlite_repo
    from modules.storage.managed_roots import StorageOwnershipError

    repo = SQLiteRepository(tmp_path / "data" / "library.db", output_dir=tmp_path / "output", temp_dir=tmp_path / "temp")
    try:
        def refuse(*args, **kwargs):
            raise StorageOwnershipError("refused")

        monkeypatch.setattr(sqlite_repo, "claim_sources_root", refuse)
        with pytest.raises(StorageOwnershipError):
            repo._claim_storage(repo.get_library_identity()["library_id"])

        assert repo.take_fresh_storage_claim() == ()
    finally:
        repo.close()
