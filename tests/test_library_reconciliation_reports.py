"""Restore and cleanup tell the user how the library and the disk disagree."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from modules.storage.models import BookCreate, ChapterCreate, PlaybackStatus, SourceType
from tests.test_backup_restore import controller as controller_fixture
from tests.test_web_api import _make_client_with_controller

controller = controller_fixture


def _converted_book(controller, title: str):
    folder = controller.config.output_dir / title
    (folder / "chapters").mkdir(parents=True)
    (folder / "book.m4b").write_bytes(b"packaged " + title.encode())
    (folder / "chapters" / "01_One.mp3").write_bytes(b"chapter")
    source = controller.config.data_dir / "sources" / f"{title}.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(f"{title} text", encoding="utf-8")
    book, _ = controller.repository.create_book_with_chapters(
        BookCreate(title=title, source_path=str(source), source_type=SourceType.TXT, total_chapters=1,
                   playback_path=str(folder / "book.m4b"), playback_status=PlaybackStatus.PACKAGED),
        [ChapterCreate(book_id=0, chapter_number=1, title="One", mp3_path=str(folder / "chapters" / "01_One.mp3"))],
    )
    return book, folder


@pytest.fixture
def restored(controller):
    """Back up; delete book A; convert book B; restore the backup."""
    client = _make_client_with_controller(controller)
    keep, _keep_folder = _converted_book(controller, "Ninety")
    short, short_folder = _converted_book(controller, "Short Note")
    backup = Path(client.post("/api/maintenance/backup").json()["backup_path"])
    assert client.delete(f"/api/library/{short.id}").json()["deleted"] is True
    after, after_folder = _converted_book(controller, "After Backup")
    response = client.post(f"/api/maintenance/backups/{backup.name}/restore")
    assert response.status_code == 200, response.text
    return client, response.json(), {"keep": keep, "short": short, "after": after,
                                     "short_folder": short_folder, "after_folder": after_folder}


def test_restore_reports_missing_files_and_unowned_folders_and_deletes_nothing(restored) -> None:
    client, payload, books = restored
    summary = payload["summary"]

    assert summary["books_restored"] == 2
    assert summary["missing_audio"] == [{"id": books["short"].id, "title": "Short Note"}]
    assert summary["missing_source"] == [{"id": books["short"].id, "title": "Short Note"}]
    assert [(folder["name"], folder["has_audio"]) for folder in summary["unowned_output_folders"]] == [
        ("After Backup", True),
    ]
    # Restore removes nothing: the newer book's files stay for the user to decide.
    assert (books["after_folder"] / "book.m4b").is_file()
    # The safety-copy path keeps the real on-disk case.
    assert payload["safety_copy_path"] == str(Path(payload["safety_copy_path"]).resolve())

    items = {item["id"]: item for item in client.get("/api/library").json()["items"]}
    assert items[books["short"].id]["audio_available"] is False
    assert items[books["keep"].id]["audio_available"] is True
    detail = client.get(f"/api/library/{books['short'].id}").json()
    assert detail["audio_available"] is False and detail["source_available"] is False


def _kept(summary, category):
    return [entry for entry in summary["kept"] if entry["category"] == category]


def test_cleanup_reports_what_it_kept_and_why_without_a_needless_backup(restored, controller) -> None:
    client, _payload, books = restored
    backups_before = client.get("/api/maintenance/backups").json()["items"]
    preview = client.get("/api/maintenance/diagnostics").json()

    report = client.post("/api/maintenance/cleanup", json={"confirm": "cleanup"}).json()
    summary = report["summary"]

    assert summary["changed"] is False
    assert report["backup_path"] is None
    assert len(client.get("/api/maintenance/backups").json()["items"]) == len(backups_before)
    folders = _kept(summary, "unowned_output_folder")
    assert [(entry["reason"], [item["name"] for item in entry["items"]]) for entry in folders] == [
        ("contains_finished_audio", ["After Backup"]),
    ]
    [without_audio] = _kept(summary, "book_without_audio")
    # The folder on disk is another book's: it blocks the removal, nothing more.
    assert without_audio["reason"] == "other_audio_present"
    assert without_audio["items"] == [{"id": books["short"].id, "title": "Short Note"}]
    assert _kept(summary, "missing_source")[0]["items"] == [{"id": books["short"].id, "title": "Short Note"}]
    # Diagnostics classify the same findings, so the Settings counts agree.
    assert _kept(preview["cleanup_preview"], "unowned_output_folder") == folders
    assert preview["checked_at"]
    # Reports are app data, not part of the audiobook folder.
    assert Path(report["report_path"]).parent == controller.config.db_path.parent / "reports"
    assert not (controller.config.output_dir / "reports").exists()


def test_cleanup_removes_an_unowned_folder_only_when_asked(restored) -> None:
    client, _payload, books = restored

    report = client.post("/api/maintenance/cleanup", json={
        "confirm": "cleanup", "remove_unowned_folders": ["After Backup", "Not There"],
    }).json()

    assert report["removed_unowned_folders"] == ["After Backup"]
    assert report["unknown_unowned_folders"] == ["Not There"]
    assert report["summary"]["removed"]["unowned_folders"] == 1
    assert report["summary"]["changed"] is True
    assert not books["after_folder"].exists()
    assert not list(books["after_folder"].parent.glob(".orpheus-delete-staging/*/*"))

    # With no unowned audio left, the next cleanup removes the fileless row.
    second = client.post("/api/maintenance/cleanup", json={"confirm": "cleanup"}).json()
    assert books["short"].id in second["actions"]["deleted_book_rows"]
    assert second["backup_path"] is not None


def test_cleanup_says_audio_is_on_disk_only_for_a_folder_named_for_the_book(restored, controller) -> None:
    client, _payload, books = restored
    renamed = controller.repository.create_book_with_chapters(
        BookCreate(title="Ninety: Part 2", source_path=str(controller.config.data_dir / "gone.txt"),
                   source_type=SourceType.TXT, total_chapters=1),
        [ChapterCreate(book_id=0, chapter_number=1, title="One")],
    )[0]
    # Conversion would have named this book's folder "Ninety_ Part 2", then "... (2)".
    folder = controller.config.output_dir / "Ninety_ Part 2 (2)"
    folder.mkdir()
    (folder / "book.m4b").write_bytes(b"audio")

    summary = client.post("/api/maintenance/cleanup", json={"confirm": "cleanup"}).json()["summary"]

    assert {entry["reason"]: [item["title"] for item in entry["items"]]
            for entry in _kept(summary, "book_without_audio")} == {
        "unowned_audio_present": ["Ninety: Part 2"],
        "other_audio_present": ["Short Note"],
    }
    assert client.get(f"/api/library/{renamed.id}").status_code == 200
    assert client.get(f"/api/library/{books['short'].id}").status_code == 200


def test_removing_the_unowned_folders_drops_the_claims_that_depended_on_them(restored) -> None:
    client, _payload, books = restored

    report = client.post("/api/maintenance/cleanup", json={
        "confirm": "cleanup", "remove_unowned_folders": ["After Backup"],
    }).json()
    summary = report["summary"]

    assert not books["after_folder"].exists()
    assert _kept(summary, "book_without_audio") == []
    assert _kept(summary, "unowned_output_folder") == []
    assert summary["changed"] is True and summary["removed"]["unowned_folders"] == 1
    # Nothing holds the fileless entry now, so it is no longer reported as kept:
    # the next cleanup removes it, and the summary says so.
    assert _kept(summary, "missing_source") == []
    assert summary["removable_books"] == [{"id": books["short"].id, "title": "Short Note"}]


def _kept_book_ids(summary) -> set[int]:
    return {item["id"] for entry in summary["kept"] if entry["category"] != "unowned_output_folder"
            for item in entry["items"]}


def test_check_names_the_book_whose_entry_the_cleanup_then_removes(restored) -> None:
    client, _payload, books = restored
    short = {"id": books["short"].id, "title": "Short Note"}

    # While a folder with unclaimed audio is on disk the entry is kept, and said to be.
    held = client.get("/api/maintenance/diagnostics").json()["cleanup_preview"]
    assert held["removable_books"] == []
    assert short["id"] in _kept_book_ids(held)

    removal = client.post("/api/maintenance/cleanup", json={
        "confirm": "cleanup", "remove_unowned_folders": ["After Backup"],
    }).json()["summary"]
    preview = client.get("/api/maintenance/diagnostics").json()["cleanup_preview"]

    # Nothing holds the entry any more: both say the next cleanup removes it.
    assert removal["removable_books"] == preview["removable_books"] == [short]
    assert removal["removed_books"] == []
    assert short["id"] not in _kept_book_ids(preview)

    report = client.post("/api/maintenance/cleanup", json={"confirm": "cleanup"}).json()

    assert report["actions"]["deleted_book_rows"] == [item["id"] for item in preview["removable_books"]]
    assert report["summary"]["removed_books"] == [short]
    assert report["summary"]["removable_books"] == []
    assert client.get(f"/api/library/{short['id']}").status_code == 404
    assert client.get("/api/maintenance/diagnostics").json()["cleanup_preview"]["removable_books"] == []


def test_backups_have_labels_and_no_sidecars(controller) -> None:
    client = _make_client_with_controller(controller)
    _converted_book(controller, "Ninety")

    backup = Path(client.post("/api/maintenance/backup").json()["backup_path"])
    [item] = client.get("/api/maintenance/backups").json()["items"]

    assert item["kind"] == "backup"
    assert item["label"].startswith("Backup from ") and item["label"].endswith("1 book")
    # No WAL sidecars. The .lock file stays: retention and restore take it to
    # prove the backup is not open (the hidden gate directory is shared too).
    assert sorted(path.name for path in backup.parent.iterdir() if not path.name.startswith(".orpheus-")) == [
        backup.name, f"{backup.name}.lock",
    ]


@pytest.mark.parametrize("field,length", [("title", 201), ("author", 201), ("tags", 501)])
def test_book_metadata_edits_are_bounded(controller, field, length) -> None:
    client = _make_client_with_controller(controller)
    book, _folder = _converted_book(controller, "Ninety")

    response = client.patch(f"/api/library/{book.id}", json={field: "x" * length})
    accepted = client.patch(f"/api/library/{book.id}", json={field: "x" * (length - 1)})

    assert response.status_code == 422
    assert accepted.status_code == 200


def test_library_counts_chapters_an_interrupted_conversion_already_published(controller) -> None:
    folder = controller.config.output_dir / "Orchard"
    source = folder / ".orpheus-source" / "orchard.txt"
    source.parent.mkdir(parents=True)
    source.write_text("text", encoding="utf-8")
    titles = [f"Chapter {number}" for number in range(1, 6)]
    names = [f"{number:02d}_Chapter {number}.mp3" for number in range(1, 6)]
    (folder / ".orpheus-recovery.json").write_text(json.dumps({
        "version": 1, "source": source.name,
        "chapters": [{"number": n, "title": t, "audio": a} for n, t, a in zip(range(1, 6), titles, names)],
    }), encoding="utf-8")
    (folder / "chapters").mkdir()
    for name in names[:3]:
        (folder / "chapters" / name).write_bytes(b"published")
    book, _ = controller.repository.create_book_with_chapters(
        BookCreate(title="Orchard", source_path=str(source), source_type=SourceType.TXT, total_chapters=5),
        [ChapterCreate(book_id=0, chapter_number=n, title=t) for n, t in zip(range(1, 6), titles)],
    )
    client = _make_client_with_controller(controller)

    [item] = client.get("/api/library").json()["items"]
    detail = client.get(f"/api/library/{book.id}").json()

    assert item["completed_chapters"] == 3
    assert detail["completed_chapters"] == 3


def test_model_list_says_why_delete_is_unavailable_during_a_conversion(controller) -> None:
    from modules.runtime_setup.catalog import MODEL_GROUPS

    client = _make_client_with_controller(controller)
    group = next(iter(MODEL_GROUPS))
    idle = client.get("/api/setup/models").json()["models"]
    assert all(model["delete_blocked"] is None for model in idle)

    coordinator = controller._conversion_service._job_coordinator
    job = coordinator.reserve_conversion(controller.config.temp_dir / "book.txt", "Book", None)
    try:
        busy = client.get("/api/setup/models").json()["models"]
        refused = client.delete(f"/api/setup/models/{group}")
    finally:
        coordinator.release_reservation(job)

    assert {model["delete_blocked"]["code"] for model in busy} == {"conversion_in_progress"}
    assert not any(model["deletable"] for model in busy)
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "conversion_in_progress"


@pytest.mark.parametrize("path", ["/api/setup/models/cleaner-cuda", "/api/setup/runtime"])
def test_model_and_runtime_delete_name_maintenance_rather_than_a_conversion(controller, path) -> None:
    client = _make_client_with_controller(controller)
    coordinator = controller._conversion_service._job_coordinator
    coordinator.begin_book_cleanup(1)
    try:
        refused = client.delete(path)
    finally:
        coordinator.end_book_cleanup(1)

    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "maintenance_in_progress"
    if path.startswith("/api/setup/models/"):
        assert refused.json()["detail"]["message"] == (
            "Wait for library maintenance to finish before deleting a model."
        )


def test_storage_folders_report_the_log_only_when_it_exists(controller) -> None:
    client = _make_client_with_controller(controller)
    assert client.get("/api/system/storage").json()["log_path"] is None

    log = controller.config.data_dir / "logs" / "backend.log"
    log.parent.mkdir(parents=True)
    log.write_text("started\n", encoding="utf-8")

    assert client.get("/api/system/storage").json()["log_path"] == str(log.resolve())


def test_unsupported_upload_and_missing_source_have_codes_and_plain_messages(controller, tmp_path) -> None:
    client = _make_client_with_controller(controller)
    missing = tmp_path / "Nobody" / "missing book.epub"

    docx = client.post("/api/uploads/source", files={"file": ("notes.docx", b"PK", "application/octet-stream")})
    absent = client.post("/api/sources/preflight", json={"source_path": str(missing)})

    assert docx.status_code == 400
    assert docx.json()["detail"]["code"] == "unsupported_source_type"
    assert absent.status_code == 400
    assert absent.json()["detail"] == {
        "code": "source_not_found",
        "message": f"Orpheus could not find {missing}. It may have been moved or deleted.",
    }


def test_permission_failure_names_the_output_folder_in_plain_words(controller) -> None:
    from modules.app.services.conversion_service import describe_conversion_failure

    output = controller.config.output_dir
    raw = str(PermissionError(13, "Access is denied", str(output / "Added While Running")))

    message = describe_conversion_failure(raw, output_dir=output, temp_dir=controller.config.temp_dir)

    assert message.startswith(f"Orpheus cannot write to the output folder {output}.")
    assert "WinError" not in message and "\\\\" not in message
    assert describe_conversion_failure("Kokoro failed", output_dir=output, temp_dir=output) == "Kokoro failed"


def test_preflight_reports_books_already_made_from_the_same_file(controller, tmp_path) -> None:
    client = _make_client_with_controller(controller)
    book, _folder = _converted_book(controller, "Ninety")
    copy = tmp_path / "elsewhere" / "ninety-copy.txt"
    copy.parent.mkdir()
    copy.write_text("Ninety text", encoding="utf-8")
    other = tmp_path / "elsewhere" / "fresh.txt"
    other.write_text("Something else entirely", encoding="utf-8")

    same = client.post("/api/sources/preflight", json={"source_path": str(copy)}).json()
    fresh = client.post("/api/sources/preflight", json={"source_path": str(other)}).json()

    assert same["existing_books"] == [{"id": book.id, "title": "Ninety", "match": "same_file"}]
    assert fresh["existing_books"] == []
