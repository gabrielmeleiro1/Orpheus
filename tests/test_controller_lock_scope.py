"""Long controller operations must not stall library reads and writes."""

from __future__ import annotations

import threading

import modules.app.controller as controller_module
from modules.storage.models import BookCreate, SourceType
from tests.test_backup_restore import controller as controller_fixture

controller = controller_fixture


def test_storage_diagnostics_walk_does_not_hold_the_repository_lock(controller, monkeypatch) -> None:
    book = controller.repository.create_book(
        BookCreate(title="Saved", source_path="source.txt", source_type=SourceType.TXT)
    )
    real_audit = controller_module.audit_with_unowned_folders
    saved = threading.Event()

    def slow_audit(**kwargs):
        # A playback save must complete while the filesystem walk is running.
        writer = threading.Thread(
            target=lambda: (controller.update_book_playback_position(book.id, 7.0), saved.set()),
            daemon=True,
        )
        writer.start()
        writer.join(timeout=3)
        return real_audit(**kwargs)

    monkeypatch.setattr(controller_module, "audit_with_unowned_folders", slow_audit)

    diagnostics = controller.get_storage_diagnostics()

    assert saved.is_set(), "playback save waited for the storage audit"
    assert diagnostics["library_epoch"] == controller.repository.get_library_identity()["library_epoch"]
    assert diagnostics["audit"]["row_counts"]["books"] == 1


def test_book_deletion_removes_files_without_holding_the_repository_lock(controller, monkeypatch) -> None:
    folder = controller.config.output_dir / "Doomed"
    folder.mkdir(parents=True)
    (folder / "book.m4b").write_bytes(b"audio")
    doomed = controller.repository.create_book(BookCreate(
        title="Doomed", source_path="source.txt", source_type=SourceType.TXT,
        playback_path=str(folder / "book.m4b"),
    ))
    keep = controller.repository.create_book(
        BookCreate(title="Keep", source_path="other.txt", source_type=SourceType.TXT)
    )
    service = controller._library_service
    real_commit = service._commit_staged_artifacts
    saved = threading.Event()

    def commit_while_saving(staged):
        writer = threading.Thread(
            target=lambda: (controller.update_book_playback_position(keep.id, 3.0), saved.set()),
            daemon=True,
        )
        writer.start()
        writer.join(timeout=3)
        return real_commit(staged)

    monkeypatch.setattr(service, "_commit_staged_artifacts", commit_while_saving)
    epoch = controller.repository.get_library_identity()["library_epoch"]

    result = controller.delete_book_with_identity(doomed.id, expected_epoch=epoch)

    assert saved.is_set(), "playback save waited for staged files to be removed"
    assert result["deleted"] is True and result["library_epoch"] == epoch
    assert "files_pending" not in result
    assert not folder.exists()
    assert controller.repository.get_book(doomed.id) is None
    assert controller.repository.get_book(keep.id).playback_position_sec == 3.0


def test_book_deletion_keeps_restore_excluded_until_files_are_removed(controller, monkeypatch) -> None:
    doomed = controller.repository.create_book(
        BookCreate(title="Doomed", source_path="source.txt", source_type=SourceType.TXT)
    )
    service = controller._library_service
    real_commit = service._commit_staged_artifacts
    refused: list[Exception] = []

    def commit_and_probe(staged):
        try:
            controller._conversion_service.begin_storage_cleanup()
        except RuntimeError as error:
            refused.append(error)
        else:
            controller._conversion_service.end_storage_cleanup()
        return real_commit(staged)

    monkeypatch.setattr(service, "_commit_staged_artifacts", commit_and_probe)

    controller.delete_book_with_identity(doomed.id)

    assert refused, "restore/cleanup could start while a deletion was still committing"


def _race_during_file_removal(controller, monkeypatch, race):
    """Delete a book and run ``race`` while its staged files are being removed."""
    source = controller.config.data_dir / "sources" / "doomed.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source")
    folder = controller.config.output_dir / "Doomed"
    folder.mkdir(parents=True)
    (folder / "book.m4b").write_bytes(b"audio")
    doomed = controller.repository.create_book(BookCreate(
        title="Doomed", source_path=str(source), source_type=SourceType.TXT,
        playback_path=str(folder / "book.m4b"),
    ))
    service = controller._library_service
    real_commit = service._commit_staged_artifacts
    outcome: dict = {}

    def commit_with_race(staged):
        try:
            outcome["value"] = race(doomed, source)
        except Exception as error:  # noqa: BLE001 - the refusal is the assertion
            outcome["error"] = error
        return real_commit(staged)

    monkeypatch.setattr(service, "_commit_staged_artifacts", commit_with_race)
    result = controller.delete_book_with_identity(doomed.id)
    assert result["deleted"] is True and "files_pending" not in result
    assert not folder.exists() and not source.exists()
    assert controller.repository.get_book(doomed.id) is None
    return outcome


def test_restore_cannot_race_the_unlocked_file_removal(controller, monkeypatch) -> None:
    from modules.storage.maintenance import backup_database

    backup = backup_database(controller.config.db_path)
    outcome = _race_during_file_removal(
        controller, monkeypatch, lambda *_: controller.restore_database_backup(backup.name),
    )
    assert isinstance(outcome.get("error"), RuntimeError), outcome


def test_storage_cleanup_cannot_race_the_unlocked_file_removal(controller, monkeypatch) -> None:
    outcome = _race_during_file_removal(
        controller, monkeypatch, lambda *_: controller.cleanup_storage(confirm="cleanup"),
    )
    assert isinstance(outcome.get("error"), RuntimeError), outcome


def test_repair_of_the_deleted_book_cannot_race_the_unlocked_file_removal(controller, monkeypatch) -> None:
    coordinator = controller._conversion_service._job_coordinator
    outcome = _race_during_file_removal(
        controller, monkeypatch, lambda book, _source: coordinator.reserve_repair(book.id, book),
    )
    assert "Book cleanup is in progress" in str(outcome.get("error")), outcome


def test_conversion_of_the_deleted_source_cannot_race_the_unlocked_file_removal(controller, monkeypatch) -> None:
    coordinator = controller._conversion_service._job_coordinator
    outcome = _race_during_file_removal(
        controller, monkeypatch, lambda _book, source: coordinator.reserve_conversion(source, "Doomed", None),
    )
    assert "being deleted" in str(outcome.get("error")), outcome


def test_second_deletion_cannot_race_the_unlocked_file_removal(controller, monkeypatch) -> None:
    outcome = _race_during_file_removal(
        controller, monkeypatch, lambda book, _source: controller.delete_book_with_identity(book.id),
    )
    assert "already in progress" in str(outcome.get("error")), outcome


def test_storage_diagnostics_still_excludes_restore(controller, monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocking_audit(**_kwargs):
        entered.set()
        release.wait(5)
        return {}, []

    monkeypatch.setattr(controller_module, "audit_with_unowned_folders", blocking_audit)
    diagnostics = threading.Thread(target=controller.get_storage_diagnostics, daemon=True)
    diagnostics.start()
    assert entered.wait(5)
    try:
        assert not controller._library_restore_lock.acquire(timeout=0.2)
    finally:
        release.set()
        diagnostics.join(5)
