"""Conversion inputs remain owned while their library book is being deleted."""

from pathlib import Path

import pytest

from modules.app.controller import ConversionJob, JobStatus
from modules.storage.models import BookCreate, SourceType
from tests import test_conversion_upload_lifecycle as _upload_lifecycle


controller = _upload_lifecycle.controller


def _book_with_managed_source(controller, name="book"):
    source = controller.config.data_dir / "sources" / f"{name}.txt"
    source.write_text("Words retained for another conversion.", encoding="utf-8")
    book = controller.repository.create_book(BookCreate(
        title=name, source_path=str(source), source_type=SourceType.TXT,
    ))
    return book, source


def _conversion_input(controller, book, source, input_kind):
    if input_kind == "managed_source":
        return source
    directory = controller.config.output_dir / "Book"
    chapters = directory / "chapters"
    chapters.mkdir(parents=True)
    playback = directory / "Book.m4b"
    playback.write_bytes(b"packaged audio")
    controller.repository.update_book(book.id, playback_path=str(playback))
    cleaned = chapters / "chapter_01_cleaned.md"
    cleaned.write_text("Cleaned text to reconvert.", encoding="utf-8")
    return cleaned


@pytest.mark.parametrize("identity_delete", [False, True])
@pytest.mark.parametrize("input_kind", ["managed_source", "output_markdown"])
def test_deleting_old_book_preserves_source_reserved_by_new_conversion(controller, identity_delete, input_kind):
    book, source = _book_with_managed_source(controller)
    source = _conversion_input(controller, book, source, input_kind)
    job = ConversionJob(id="new-job", book_id=None, status=JobStatus.IDLE)
    job.upload_source_path = source.resolve()
    controller._conversion_service._active_job = job

    delete = controller.delete_book_with_identity if identity_delete else controller.delete_book
    with pytest.raises(RuntimeError, match="source"):
        delete(book.id)

    assert source.is_file()
    assert controller.repository.get_book(book.id) is not None
    other, other_source = _book_with_managed_source(controller, "unrelated")
    assert controller.delete_book(other.id)
    assert not other_source.exists()


@pytest.mark.parametrize("input_kind", ["managed_source", "output_markdown"])
def test_conversion_cannot_read_source_reserved_for_book_deletion(controller, monkeypatch, input_kind):
    book, source = _book_with_managed_source(controller)
    source = _conversion_input(controller, book, source, input_kind)
    service = controller._conversion_service
    monkeypatch.setattr(service, "_validate_requested_tts_runtime", lambda **kwargs: None)
    service.begin_book_cleanup(book.id)
    try:
        with pytest.raises(RuntimeError, match="source"):
            service.start_conversion(source_path=Path(source))
        assert service.get_active_job() is None
    finally:
        service.end_book_cleanup(book.id)

    job = service._job_coordinator.reserve_conversion(source, "Book", None)
    assert job.upload_source_path == source.resolve()
    service._job_coordinator.release_reservation(job)
