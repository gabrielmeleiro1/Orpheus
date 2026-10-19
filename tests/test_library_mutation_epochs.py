from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from modules.storage.maintenance import backup_database, restore_database_backup
from modules.storage.models import BookCreate, SourceType
from modules.storage.sqlite_repo import LibraryReplacedError
from tests.test_backup_restore import controller as controller_fixture
from tests.test_web_api import _make_client_with_controller

controller = controller_fixture


@pytest.mark.parametrize("method, suffix, payload", [
    ("patch", "", {"title": "Wrong"}),
    ("put", "/playback-position", {"position_sec": 99}),
    ("delete", "", None),
])
def test_mutation_waiting_across_restore_rejects_selected_epoch(controller, method, suffix, payload):
    repo = controller.repository
    book = repo.create_book(BookCreate(title="Saved", source_path="source.txt", source_type=SourceType.TXT))
    epoch = repo.get_library_identity()["library_epoch"]
    backup = backup_database(repo.db_path)
    client = _make_client_with_controller(controller)
    entered = threading.Event()

    def request():
        entered.set()
        kwargs = {"params": {"expected_epoch": epoch}} if payload is None else {"json": {**payload, "expected_epoch": epoch}}
        return getattr(client, method)(f"/api/library/{book.id}{suffix}", **kwargs)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with repo._lock:
            pending = pool.submit(request)
            assert entered.wait(5)
            restore_database_backup(repo, backup.name)
        response = pending.result(timeout=5)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "library_replaced"
    assert repo.get_book(book.id).title == "Saved"
    assert repo.get_book(book.id).playback_position_sec == 0


def test_current_epoch_and_legacy_mutations_remain_writable(controller, caplog):
    repo = controller.repository
    book = repo.create_book(BookCreate(title="Saved", source_path="source.txt", source_type=SourceType.TXT))
    epoch = repo.get_library_identity()["library_epoch"]
    client = _make_client_with_controller(controller)
    assert client.patch(f"/api/library/{book.id}", json={"title": "Current", "expected_epoch": epoch}).status_code == 200
    assert client.put(f"/api/library/{book.id}/playback-position", json={"position_sec": 3}).status_code == 200
    assert "without expected library epoch" in caplog.text


def test_repair_rejects_stale_epoch_before_starting_work(controller):
    controller._conversion_service.start_repair = Mock(return_value=SimpleNamespace(id="repair", status="idle", book_id=1, error=None))
    client = _make_client_with_controller(controller)
    response = client.post("/api/conversions/repair/1", json={"expected_epoch": "old"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "library_replaced"
    controller._conversion_service.start_repair.assert_not_called()


def test_repair_waiting_across_restore_rejects_epoch_before_reserving_job(controller):
    repo = controller.repository
    epoch = repo.get_library_identity()["library_epoch"]
    backup = backup_database(repo.db_path)
    controller._conversion_service.start_repair = Mock()
    entered = threading.Event()

    def repair():
        entered.set()
        return controller.repair_conversion(1, expected_epoch=epoch)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with repo._lock:
            pending = pool.submit(repair)
            assert entered.wait(5)
            restore_database_backup(repo, backup.name)
        with pytest.raises(LibraryReplacedError):
            pending.result(timeout=5)
    controller._conversion_service.start_repair.assert_not_called()
