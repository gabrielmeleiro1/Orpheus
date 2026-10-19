import os
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from modules.app.controller import ConversionJob, JobStatus
from modules.storage.models import BookCreate, ConversionMode, SourceType
from modules.storage.sqlite_repo import LibraryReplacedError
from modules.web.routes.library import delete_library_book
from tests.test_backup_restore import controller as controller_fixture
from tests.test_web_api import _make_client_with_controller

controller = controller_fixture


class _TimedLock:
    def __init__(self, lock, before_acquire=None):
        self.lock = lock
        self.before_acquire = before_acquire

    def __enter__(self):
        if self.before_acquire:
            self.before_acquire()
        # Bound a blocked acquisition so failed tests can close their database.
        if not self.lock.acquire(timeout=4):
            raise TimeoutError("Lock acquisition did not finish")
        return self

    def __exit__(self, *args):
        self.lock.release()


def _book(controller, source_path="source.txt"):
    return controller.repository.create_book(
        BookCreate(title="Saved", source_path=str(source_path), source_type=SourceType.TXT)
    )


def _overlap(controller, monkeypatch, held_operation, contender_operation, pause_target):
    barrier = threading.Barrier(2)
    entered = threading.Event()
    service = controller._conversion_service
    coordinator = service._job_coordinator
    original = pause_target[0]
    contender_entered = False

    def pause(*args, **kwargs):
        entered.set()
        barrier.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(pause_target[1], pause_target[2], pause)

    def before_job_acquire():
        nonlocal contender_entered
        if threading.current_thread().name == "contender" and not contender_entered:
            contender_entered = True
            barrier.wait(timeout=5)

    monkeypatch.setattr(controller.repository, "_lock", _TimedLock(controller.repository._lock))
    monkeypatch.setattr(coordinator, "_job_lock", _TimedLock(coordinator._job_lock, before_job_acquire))
    results = {}

    def run(name, operation):
        try:
            results[name] = operation()
        except Exception as error:
            results[name] = error

    threads = [
        threading.Thread(target=run, args=("holder", held_operation), name="holder", daemon=True),
        threading.Thread(target=run, args=("contender", contender_operation), name="contender", daemon=True),
    ]
    threads[0].start()
    assert entered.wait(5)
    threads[1].start()
    try:
        for thread in threads:
            thread.join(timeout=1)
        assert all(not thread.is_alive() for thread in threads), "Both operations must finish without deadlocking"
    finally:
        for thread in threads:
            thread.join(timeout=6)
    return results


def test_repair_and_first_checkpoint_finish_without_deadlocking(controller, monkeypatch, tmp_path):
    service = controller._conversion_service
    source = tmp_path / "source.txt"
    source.write_text("Text")
    book = _book(controller)
    epoch = controller.repository.get_library_identity()["library_epoch"]
    job = ConversionJob(id="conversion", book_id=None, status=JobStatus.RUNNING)
    service._active_job = job
    command = SimpleNamespace(job=job, source_path=source, conversion_mode=ConversionMode.AUDIOBOOK, runtime_options=None)
    result = SimpleNamespace(title="Converted", author=None, chapters=[], output_path=None)
    outcomes = _overlap(
        controller, monkeypatch,
        lambda: service._checkpoint_conversion(command, source, result),
        lambda: controller.repair_conversion(book.id, expected_epoch=epoch),
        (service._persist_conversion_library_records, service, "_persist_conversion_library_records"),
    )
    assert outcomes["holder"] is None
    assert isinstance(outcomes["contender"], RuntimeError)
    assert "already in progress" in str(outcomes["contender"])
    assert controller.repository.get_book(job.book_id).title == "Converted"


def test_upload_and_book_deletion_finish_without_deadlocking(controller, monkeypatch):
    library = controller._library_service
    upload = controller.config.temp_dir / "web_uploads" / "uploaded.txt"
    upload.parent.mkdir(parents=True, exist_ok=True)
    upload.write_text("Text")
    book = _book(controller)
    identity = controller.repository.get_library_identity()
    outcomes = _overlap(
        controller, monkeypatch,
        lambda: controller.delete_upload("upload:uploaded.txt"),
        lambda: delete_library_book(book.id, controller, expected_epoch=identity["library_epoch"]),
        (library._is_source_referenced_by_books, library, "_is_source_referenced_by_books"),
    )
    assert outcomes["holder"] == {"deleted": True}
    assert outcomes["contender"] == {"book_id": book.id, "deleted": True, **identity}
    assert controller.repository.get_book(book.id) is None
    assert not upload.exists()


class _RecordingLock:
    def __init__(self, lock, name, held, acquisitions):
        self.lock = lock
        self.name = name
        self.held = held
        self.acquisitions = acquisitions

    def __enter__(self):
        thread_id = threading.get_ident()
        owned = self.held.setdefault(thread_id, {})
        self.acquisitions.append((thread_id, self.name, frozenset(owned)))
        self.lock.acquire()
        owned[self.name] = owned.get(self.name, 0) + 1
        return self

    def __exit__(self, *args):
        owned = self.held[threading.get_ident()]
        owned[self.name] -= 1
        if owned[self.name] == 0:
            del owned[self.name]
        self.lock.release()


def test_library_operations_acquire_job_lock_before_repository_lock(controller, monkeypatch):
    service = controller._conversion_service
    coordinator = service._job_coordinator
    repo = controller.repository
    held, acquisitions = {}, []
    monkeypatch.setattr(repo, "_lock", _RecordingLock(repo._lock, "repository", held, acquisitions))
    monkeypatch.setattr(coordinator, "_job_lock", _RecordingLock(coordinator._job_lock, "job", held, acquisitions))
    monkeypatch.setattr(service, "_validate_requested_tts_runtime", lambda **kwargs: None)
    monkeypatch.setattr(service._runtime_options_resolver, "with_setup_defaults", lambda options: options)
    errors = []

    def convert(*, job, source_path, **kwargs):
        try:
            command = SimpleNamespace(job=job, source_path=source_path, conversion_mode=ConversionMode.AUDIOBOOK, runtime_options=None)
            result = SimpleNamespace(title="Converted", author=None, chapters=[], output_path=None)
            service._checkpoint_conversion(command, source_path, result)
            service._handle_conversion_success(job=job, source_path=source_path, pipeline_result=result, callbacks=None)
        except Exception as error:
            errors.append(error)
            job.mark_cancelled(str(error))

    def repair(*, job, book_id, **kwargs):
        assert repo.get_book(book_id) is not None
        coordinator.publish_success(job=job, book_id=book_id, callbacks=None)

    monkeypatch.setattr(service, "_run_conversion", convert)
    monkeypatch.setattr(service, "_run_repair", repair)
    saved = _book(controller)
    upload_root = controller.config.temp_dir / "web_uploads"
    upload_root.mkdir(parents=True, exist_ok=True)
    upload = upload_root / "conversion.txt"
    upload.write_text("Text")
    job = controller.start_conversion(upload)
    assert job.wait(timeout=5)
    assert errors == []
    assert job.status is JobStatus.COMPLETED
    assert service.resolve_recovered_upload(upload) == Path(repo.get_book(job.book_id).source_path)
    epoch = repo.get_library_identity()["library_epoch"]
    repair_job = controller.repair_conversion(saved.id, expected_epoch=epoch)
    assert repair_job.wait(timeout=5)
    assert repair_job.status is JobStatus.COMPLETED

    loose = upload_root / "loose.txt"
    loose.write_text("Text")
    assert controller.delete_upload("upload:loose.txt") == {"deleted": True}
    stale = upload_root / "stale.txt"
    stale.write_text("Text")
    os.utime(stale, (1, 1))
    assert controller.sweep_stale_uploads() == 1

    pending = coordinator.reserve_conversion(upload, None, None)
    assert controller.cancel_conversion(expected_job_id=pending.id)
    pending.mark_cancelled("Cancelled")
    coordinator.release_reservation(pending)
    assert not controller.cancel_conversion()
    assert not service.has_active_job()
    assert service.get_active_job() is None

    backup = controller.backup_database()
    controller.restore_database_backup(backup.name)
    assert service.resolve_recovered_upload(upload) is None
    identity = repo.get_library_identity()
    client = _make_client_with_controller(controller)
    assert client.get("/api/library").status_code == 200
    assert client.get(f"/api/library/{saved.id}").status_code == 200
    assert client.patch(f"/api/library/{saved.id}", json={"title": "Renamed", "expected_epoch": identity["library_epoch"]}).status_code == 200
    assert client.put(f"/api/library/{saved.id}/playback-position", json={"position_sec": 3, "expected_epoch": identity["library_epoch"]}).status_code == 200
    assert client.get(f"/api/library/{saved.id}/audio").status_code == 404
    client.app.state.desktop_mode = True
    client.app.state.desktop_api_token = "secret"
    token = client.post("/api/desktop/query-token", headers={"Authorization": "Bearer secret"}, json={
        "scope": "audio", "book_id": saved.id, "expected_epoch": identity["library_epoch"],
    })
    assert token.status_code == 200
    assert client.get(f"/api/library/{saved.id}/audio", params={"token": token.json()["token"]}).status_code == 404
    assert delete_library_book(saved.id, controller, identity["library_epoch"]) == {
        "book_id": saved.id, "deleted": True, **identity,
    }
    assert controller.delete_book(job.book_id, expected_epoch=identity["library_epoch"]) is True
    controller.get_library_books()
    controller.get_storage_diagnostics()
    controller.cleanup_storage(confirm="cleanup")

    assert any(name == "repository" and "job" in owned for _, name, owned in acquisitions)
    assert {thread_id for thread_id, _, _ in acquisitions} != {threading.get_ident()}
    assert not [entry for entry in acquisitions if entry[1] == "job" and "repository" in entry[2]]
    assert all(not owned for owned in held.values())


def test_stale_repair_releases_reservation_without_starting_work(controller):
    service = controller._conversion_service
    service.start_repair = Mock()
    with pytest.raises(LibraryReplacedError):
        controller.repair_conversion(1, expected_epoch="old")
    service.start_repair.assert_not_called()
    assert service.get_active_job() is None
    service.begin_storage_cleanup()
    service.end_storage_cleanup()


def test_repair_reservation_blocks_restore_until_epoch_is_validated(controller, monkeypatch):
    repo = controller.repository
    service = controller._conversion_service
    book = _book(controller)
    backup = controller.backup_database()
    epoch = repo.get_library_identity()["library_epoch"]
    service.start_repair = Mock(side_effect=ValueError("Cannot start"))
    snapshot = controller.library_snapshot
    entered = threading.Event()
    resume = threading.Event()

    @contextmanager
    def wait_for_repository(*args, **kwargs):
        entered.set()
        assert resume.wait(5)
        with snapshot(*args, **kwargs) as identity:
            yield identity

    monkeypatch.setattr(controller, "library_snapshot", wait_for_repository)
    errors = []

    def repair():
        try:
            controller.repair_conversion(book.id, expected_epoch=epoch)
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=repair, daemon=True)
    try:
        thread.start()
        assert entered.wait(5)
        with pytest.raises(RuntimeError, match="queued or running"):
            controller.restore_database_backup(backup.name)
    finally:
        resume.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(errors) == 1 and str(errors[0]) == "Cannot start"
    assert service.get_active_job() is None


def test_book_deletion_reservation_blocks_restore_before_snapshot(controller, monkeypatch):
    service = controller._conversion_service
    book = _book(controller)
    backup = controller.backup_database()
    identity = controller.repository.get_library_identity()
    snapshot = controller.library_snapshot
    entered, resume = threading.Event(), threading.Event()

    @contextmanager
    def wait_for_repository(*args, **kwargs):
        entered.set()
        assert resume.wait(5)
        with snapshot(*args, **kwargs) as current:
            yield current

    monkeypatch.setattr(controller, "library_snapshot", wait_for_repository)
    results = []

    def delete():
        try:
            results.append(delete_library_book(book.id, controller, identity["library_epoch"]))
        except Exception as error:
            results.append(error)

    thread = threading.Thread(target=delete, daemon=True)
    try:
        thread.start()
        assert entered.wait(5)
        with pytest.raises(RuntimeError, match="book cleanup"):
            controller.restore_database_backup(backup.name)
    finally:
        resume.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert results == [{"book_id": book.id, "deleted": True, **identity}]
    service.begin_storage_cleanup()
    service.end_storage_cleanup()


def test_stale_book_deletion_releases_cleanup_without_deleting(controller):
    book = _book(controller)
    with pytest.raises(LibraryReplacedError):
        controller.delete_book_with_identity(book.id, expected_epoch="old")
    assert controller.repository.get_book(book.id) is not None
    service = controller._conversion_service
    service.begin_storage_cleanup()
    service.end_storage_cleanup()


def test_book_cleanup_and_storage_cleanup_exclude_each_other(controller):
    service = controller._conversion_service
    service.begin_book_cleanup(1)
    try:
        with pytest.raises(RuntimeError, match="book cleanup"):
            service.begin_storage_cleanup()
    finally:
        service.end_book_cleanup(1)
    service.begin_storage_cleanup()
    try:
        with pytest.raises(RuntimeError, match="storage cleanup"):
            service.begin_book_cleanup(1)
    finally:
        service.end_storage_cleanup()


@pytest.mark.parametrize("restore_point", ["before_start", "before_launch"])
def test_restore_from_another_controller_after_epoch_validation_rejects_repair(
    controller, monkeypatch, restore_point,
):
    from modules.app.controller import AppController

    second = AppController(controller.config, repository=controller.repository)
    service = controller._conversion_service
    book = _book(controller)
    backup = controller.backup_database()
    controller.repository.update_book(book.id, title="Current version")
    epoch = controller.repository.get_library_identity()["library_epoch"]
    launched = []
    monkeypatch.setattr(service._runtime_options_resolver, "with_setup_defaults", lambda options: options)
    monkeypatch.setattr(service, "_start_pipeline_job", lambda **kwargs: launched.append(kwargs["job"]))

    def restore_from_second():
        errors = []

        def restore():
            try:
                second.restore_database_backup(backup.name)
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=restore, daemon=True)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive(), "Restore was blocked after epoch validation"
        assert not errors, errors
        assert controller.repository.get_library_identity()["library_epoch"] != epoch

    if restore_point == "before_start":
        start = service.start_repair

        def restore_then_start(*args, **kwargs):
            restore_from_second()
            return start(*args, **kwargs)

        monkeypatch.setattr(service, "start_repair", restore_then_start)
        monkeypatch.setattr(service, "_validate_requested_tts_runtime", lambda **kwargs: None)
    else:
        monkeypatch.setattr(service, "_validate_requested_tts_runtime", lambda **kwargs: restore_from_second())

    try:
        with pytest.raises(LibraryReplacedError):
            controller.repair_conversion(book.id, expected_epoch=epoch)
        assert launched == []
        assert service.get_active_job() is None
        service.begin_storage_cleanup()
        service.end_storage_cleanup()
    finally:
        job = service.get_active_job()
        if job is not None:
            service._job_coordinator.release_reservation(job)


def test_repair_reservation_fence_holds_after_epoch_validation(controller, monkeypatch):
    service = controller._conversion_service
    book = _book(controller)
    backup = controller.backup_database()
    epoch = controller.repository.get_library_identity()["library_epoch"]

    def check_reservation(*args, **kwargs):
        with pytest.raises(RuntimeError, match="queued or running"):
            controller.restore_database_backup(backup.name)
        raise ValueError("Stop before launching repair")

    monkeypatch.setattr(service, "start_repair", check_reservation)
    with pytest.raises(ValueError, match="Stop before launching repair"):
        controller.repair_conversion(book.id, expected_epoch=epoch)
    assert service.get_active_job() is None
