from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._factories_app import make_app_config

from modules.app import controller as app_controller_module
from modules.app.services.library_service import LibraryService
from modules.storage.models import Book, Chapter, ConversionMode, PlaybackStatus, SourceType
from modules.storage.sqlite_repo import SQLiteRepository


def _book(*, id, title, source_path, **overrides):
    values = {
        "id": id,
        "title": title,
        "author": "Tester",
        "source_path": source_path,
        "source_type": SourceType.EPUB,
        "conversion_mode": ConversionMode.AUDIOBOOK,
        "total_chapters": 1,
        "playback_path": None,
        "playback_status": PlaybackStatus.CHAPTER_AUDIO,
        "playback_position_sec": 0.0,
        "created_at": datetime(2026, 4, 1, 9, 0, 0),
        "updated_at": datetime(2026, 4, 1, 9, 5, 0),
    }
    values.update(overrides)
    return Book(**values)


def _chapter(*, id, book_id, mp3_path, **overrides):
    values = {
        "id": id,
        "book_id": book_id,
        "chapter_number": 1,
        "title": "Chapter 1",
        "duration_ms": 1000,
        "mp3_path": mp3_path,
    }
    values.update(overrides)
    return Chapter(**values)


def _make_service(tmp_path, repository, **overrides):
    values = {
        "output_dir": tmp_path / "output",
        "temp_dir": tmp_path / "temp",
    }
    values.update(overrides)
    return LibraryService(repository, **values)


class _FakeRuntimeCapabilityService:
    def get_system_runtime_snapshot(self):
        return {"tts": {"available": False}, "cleaner": {"available": False}}


class _FakeGlobalAPILimiter:
    def __init__(self, concurrency):
        self.concurrency = concurrency
        self.shutdown_called = False

    def snapshot(self):
        return {"wait_count": 0, "wait_ms_total": 0.0}

    def shutdown(self):
        self.shutdown_called = True


class _FakeSQLiteRepository:
    def __init__(self, db_path, *, output_dir=None, temp_dir=None):
        self.db_path = db_path
        self.output_dir = output_dir
        self.temp_dir = temp_dir
        self.closed = False

    def close(self):
        self.closed = True


class _RepositoryStub:
    def get_book_with_chapters(self, book_id: int):
        return (
            _book(
                id=book_id,
                title="Detail Book",
                source_path="/tmp/source.epub",
                conversion_mode=ConversionMode.DOCUMENT_AUDIO,
                playback_path="output/Detail Book/Detail Book.m4a",
                playback_status=PlaybackStatus.PACKAGED,
                playback_position_sec=32.5,
                created_at=datetime(2026, 3, 22, 10, 0, 0),
                updated_at=datetime(2026, 3, 22, 10, 5, 0),
            ),
            [
                _chapter(id=11, book_id=book_id, mp3_path="output/Detail Book/chapters/01.mp3")
            ],
        )


def test_library_service_get_book_reports_stored_detail(tmp_path):
    service = _make_service(tmp_path, _RepositoryStub())

    payload = service.get_book(7)

    assert payload is not None
    assert payload["playback_path"] == "output/Detail Book/Detail Book.m4a"
    assert payload["playback_status"] == PlaybackStatus.PACKAGED.value
    assert payload["conversion_mode"] == ConversionMode.DOCUMENT_AUDIO.value
    assert payload["tags"] == ""
    assert payload["favorite"] is False
    assert payload["source_available"] is False
    assert payload["chapters"][0]["number"] == 1


@pytest.mark.parametrize("start_ms", [None, 0, 350])
def test_library_service_detail_includes_packaged_chapter_start(tmp_path, start_ms):
    book, chapters = _RepositoryStub().get_book_with_chapters(7)
    chapters[0].packaged_start_ms = start_ms
    service = _make_service(tmp_path, _DeleteRepositoryStub((book, chapters)))

    assert service.get_book(7)["chapters"][0]["packaged_start_ms"] == start_ms


class _ListCleanupRepositoryStub:
    def __init__(self, books) -> None:
        self.books = list(books)
        self.deleted_ids: list[int] = []

    def list_books(self, filters):  # noqa: ANN001
        return list(self.books)

    def delete_book(self, book_id: int) -> bool:
        self.deleted_ids.append(book_id)
        self.books = [book for book in self.books if book.id != book_id]
        return True


def test_library_service_keeps_books_when_source_file_missing(tmp_path):
    existing_source = tmp_path / "keep.epub"
    existing_source.write_text("ok", encoding="utf-8")

    repository = _ListCleanupRepositoryStub(
        [
            SimpleNamespace(
                id=1,
                title="Keep",
                author="Tester",
                total_chapters=1,
                completed_chapters=1,
                created_at=datetime(2026, 4, 1, 9, 0, 0),
                source_path=str(existing_source),
            ),
            SimpleNamespace(
                id=2,
                title="Remove",
                author="Tester",
                total_chapters=1,
                completed_chapters=1,
                created_at=datetime(2026, 4, 1, 9, 1, 0),
                source_path=str(tmp_path / "missing.epub"),
            ),
        ]
    )

    service = _make_service(tmp_path, repository)

    books = service.get_library_books()

    assert [book.id for book in books] == [1, 2]
    assert repository.deleted_ids == []


class _DeleteRepositoryStub:
    def __init__(self, detail) -> None:
        self.detail = detail
        self.deleted_ids: list[int] = []

    def get_book_with_chapters(self, book_id: int):
        if self.detail is None:
            return None
        book, chapters = self.detail
        if book.id != book_id:
            return None
        return book, list(chapters)

    def delete_book(self, book_id: int) -> bool:
        if self.detail is None or self.detail[0].id != book_id:
            return False
        self.deleted_ids.append(book_id)
        self.detail = None
        return True


def test_library_service_delete_book_preserves_external_source_and_removes_output_artifacts(tmp_path):
    source_path = tmp_path / "library" / "source.epub"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text("source", encoding="utf-8")

    output_root = tmp_path / "output"
    book_dir = output_root / "Delete Me"
    chapters_dir = book_dir / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)
    (book_dir / "Delete Me.m4b").write_text("audio", encoding="utf-8")
    (chapters_dir / "01.mp3").write_text("chapter", encoding="utf-8")
    (book_dir / "source.md").write_text("notes", encoding="utf-8")

    repository = _DeleteRepositoryStub(
        (
            _book(
                id=7,
                title="Delete Me",
                source_path=str(source_path),
                playback_path="output/Delete Me/Delete Me.m4b",
                playback_status=PlaybackStatus.PACKAGED,
            ),
            [
                _chapter(id=11, book_id=7, mp3_path="output/Delete Me/chapters/01.mp3")
            ],
        )
    )

    service = _make_service(tmp_path, repository, output_dir=output_root)

    deleted = service.delete_book(7)

    assert deleted is True
    assert repository.deleted_ids == [7]
    assert source_path.exists()
    assert not book_dir.exists()


def test_library_service_reads_chapter_cleaning_preview(tmp_path):
    output_root = tmp_path / "output"
    chapters_dir = output_root / "Clean Book" / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)
    chapter_audio = chapters_dir / "01_chapter.mp3"
    chapter_audio.write_text("audio", encoding="utf-8")
    (chapters_dir / "chapter_01_before_cleaning.md").write_text("# Chapter 1\n\nraw text", encoding="utf-8")
    (chapters_dir / "chapter_01_llm_input.md").write_text("# Chapter 1\n\nrule text", encoding="utf-8")
    (chapters_dir / "chapter_01_cleaned.md").write_text("# Chapter 1\n\nclean text", encoding="utf-8")
    (chapters_dir / "chapter_01_cleaning_trace.jsonl").write_text(
        '{"chunk_index": 1, "original_text": "raw", "cleaned_text": "clean"}\n',
        encoding="utf-8",
    )

    repository = _DeleteRepositoryStub(
        (
            _book(
                id=12,
                title="Clean Book",
                source_path=str(tmp_path / "source.md"),
                source_type=SourceType.MD,
            ),
            [
                _chapter(id=21, book_id=12, mp3_path=str(chapter_audio))
            ],
        )
    )
    service = _make_service(tmp_path, repository, output_dir=output_root)

    preview = service.get_chapter_cleaning_preview(12, 1)

    assert preview is not None
    assert preview["available"] is True
    assert "raw text" in preview["before_text"]
    assert "rule text" in preview["llm_input_text"]
    assert "clean text" in preview["cleaned_text"]
    assert preview["trace_rows"][0]["chunk_index"] == 1


def test_library_service_cleaning_preview_rejects_chapter_path_outside_output(
    tmp_path,
):
    output_root = tmp_path / "output"
    outside_dir = tmp_path / "private"
    outside_dir.mkdir()
    outside_audio = outside_dir / "01_chapter.mp3"
    outside_audio.write_text("audio", encoding="utf-8")
    secret = outside_dir / "chapter_01_before_cleaning.md"
    secret.write_text("must not be exposed", encoding="utf-8")
    repository = _DeleteRepositoryStub(
        (
            _book(
                id=13,
                title="Corrupt Record",
                source_path=str(tmp_path / "source.md"),
                source_type=SourceType.MD,
            ),
            [
                _chapter(id=22, book_id=13, mp3_path=str(outside_audio))
            ],
        )
    )
    service = _make_service(tmp_path, repository, output_dir=output_root)

    preview = service.get_chapter_cleaning_preview(13, 1)

    assert preview is not None
    assert preview["available"] is False
    assert preview["before_text"] == ""
    assert secret.read_text(encoding="utf-8") == "must not be exposed"


@pytest.mark.parametrize("target_inside_output", [False, True])
@pytest.mark.parametrize("suffix, field, empty", [
    ("before_cleaning.md", "before_text", ""),
    ("llm_input.md", "llm_input_text", ""),
    ("cleaned.md", "cleaned_text", ""),
    ("cleaning_trace.jsonl", "trace_rows", []),
])
def test_cleaning_preview_ignores_symlinked_sidecars(
    tmp_path, target_inside_output, suffix, field, empty,
):
    output_root = tmp_path / "output"
    chapters_dir = output_root / "Book" / "chapters"
    chapters_dir.mkdir(parents=True)
    target = (output_root if target_inside_output else tmp_path) / "private.txt"
    target.write_text('{"private": "must not be exposed"}\n', encoding="utf-8")
    (chapters_dir / f"chapter_01_{suffix}").symlink_to(target)
    book, chapters = _RepositoryStub().get_book_with_chapters(7)
    chapters[0].mp3_path = str(chapters_dir / "01.mp3")
    service = _make_service(tmp_path, _DeleteRepositoryStub((book, chapters)), output_dir=output_root)

    preview = service.get_chapter_cleaning_preview(7, 1)

    assert preview[field] == empty
    assert preview["available"] is False


def test_library_service_delete_book_removes_uploaded_source_copy(tmp_path):
    temp_dir = tmp_path / "temp"
    source_path = temp_dir / "web_uploads" / "uploaded.epub"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text("source", encoding="utf-8")

    output_root = tmp_path / "output"
    book_dir = output_root / "Uploaded"
    chapters_dir = book_dir / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)
    (chapters_dir / "01.mp3").write_text("chapter", encoding="utf-8")

    repository = _DeleteRepositoryStub(
        (
            _book(id=8, title="Uploaded", source_path="temp/web_uploads/uploaded.epub"),
            [
                _chapter(id=12, book_id=8, mp3_path="output/Uploaded/chapters/01.mp3")
            ],
        )
    )

    service = _make_service(tmp_path, repository, output_dir=output_root, temp_dir=temp_dir)

    deleted = service.delete_book(8)

    assert deleted is True
    assert repository.deleted_ids == [8]
    assert not source_path.exists()
    assert not book_dir.exists()


def test_library_service_delete_book_preserves_lookalike_temp_web_uploads_outside_upload_root(
    tmp_path,
):
    """Name-only temp/web_uploads paths outside the real upload root must not be deleted."""
    lookalike_source = tmp_path / "Documents" / "temp" / "web_uploads" / "novel.pdf"
    lookalike_source.parent.mkdir(parents=True, exist_ok=True)
    lookalike_source.write_text("user owned novel", encoding="utf-8")

    temp_root = tmp_path / "app-support" / "temp"
    managed_source = temp_root / "web_uploads" / "managed.epub"
    managed_source.parent.mkdir(parents=True, exist_ok=True)
    managed_source.write_text("managed upload", encoding="utf-8")

    output_root = tmp_path / "app-support" / "output"
    lookalike_book_dir = output_root / "Lookalike"
    managed_book_dir = output_root / "Managed"
    for book_dir in (lookalike_book_dir, managed_book_dir):
        chapters_dir = book_dir / "chapters"
        chapters_dir.mkdir(parents=True, exist_ok=True)
        (chapters_dir / "01.mp3").write_text("chapter", encoding="utf-8")

    staging_root = temp_root / "delete_staging"

    lookalike_repository = _DeleteRepositoryStub(
        (
            _book(
                id=18,
                title="Lookalike",
                source_path=str(lookalike_source),
                source_type=SourceType.PDF,
            ),
            [
                _chapter(
                    id=18,
                    book_id=18,
                    mp3_path=str(lookalike_book_dir / "chapters" / "01.mp3"),
                )
            ],
        )
    )
    lookalike_service = _make_service(tmp_path, lookalike_repository, output_dir=output_root, temp_dir=temp_root)

    deleted_lookalike = lookalike_service.delete_book(18)

    assert deleted_lookalike is True
    assert lookalike_repository.deleted_ids == [18]
    assert lookalike_source.exists()
    assert lookalike_source.read_text(encoding="utf-8") == "user owned novel"
    assert not lookalike_book_dir.exists()
    if staging_root.exists():
        staged_names = {path.name for path in staging_root.rglob("*") if path.is_file()}
        assert "novel.pdf" not in staged_names

    managed_repository = _DeleteRepositoryStub(
        (
            _book(id=19, title="Managed", source_path=str(managed_source)),
            [
                _chapter(id=19, book_id=19, mp3_path=str(managed_book_dir / "chapters" / "01.mp3"))
            ],
        )
    )
    managed_service = _make_service(tmp_path, managed_repository, output_dir=output_root, temp_dir=temp_root)

    deleted_managed = managed_service.delete_book(19)

    assert deleted_managed is True
    assert managed_repository.deleted_ids == [19]
    assert not managed_source.exists()
    assert not managed_book_dir.exists()

def test_library_service_delete_book_removes_loose_artifacts_without_deleting_output_root(tmp_path):
    output_root = tmp_path / "output"
    output_root.mkdir()
    sibling_dir = output_root / "Other Book"
    sibling_dir.mkdir()
    sibling_file = sibling_dir / "keep.m4a"
    sibling_file.write_text("keep", encoding="utf-8")

    loose_playback = output_root / "loose-book.m4a"
    loose_chapter = output_root / "loose-chapter.mp3"
    loose_before = output_root / "chapter_01_before_cleaning.md"
    loose_input = output_root / "chapter_01_llm_input.md"
    loose_cleaned = output_root / "chapter_01_cleaned.md"
    loose_trace = output_root / "chapter_01_cleaning_trace.jsonl"
    for artifact in (loose_playback, loose_chapter, loose_before, loose_input, loose_cleaned, loose_trace):
        artifact.write_text("artifact", encoding="utf-8")

    source_path = tmp_path / "imports" / "source.md"
    source_path.parent.mkdir()
    source_path.write_text("external source", encoding="utf-8")

    repository = _DeleteRepositoryStub(
        (
            _book(
                id=19,
                title="Loose Book",
                source_path=str(source_path),
                source_type=SourceType.MD,
                conversion_mode=ConversionMode.DOCUMENT_AUDIO,
                playback_path=str(loose_playback),
                playback_status=PlaybackStatus.PACKAGED,
            ),
            [
                _chapter(id=19, book_id=19, mp3_path=str(loose_chapter))
            ],
        )
    )

    service = _make_service(tmp_path, repository, output_dir=output_root)

    deleted = service.delete_book(19)

    assert deleted is True
    assert repository.deleted_ids == [19]
    assert output_root.exists()
    assert sibling_file.exists()
    assert source_path.exists()
    for artifact in (loose_playback, loose_chapter, loose_before, loose_input, loose_cleaned, loose_trace):
        assert not artifact.exists()


def test_library_service_delete_book_preserves_external_source_inside_output_root(tmp_path):
    output_root = tmp_path / "output"
    source_path = output_root / "user-imports" / "source.epub"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text("external source", encoding="utf-8")

    book_dir = output_root / "Generated Book"
    chapters_dir = book_dir / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)
    (book_dir / "Generated Book.m4b").write_text("audio", encoding="utf-8")
    (chapters_dir / "01.mp3").write_text("chapter", encoding="utf-8")

    repository = _DeleteRepositoryStub(
        (
            _book(
                id=10,
                title="Generated Book",
                source_path=str(source_path),
                playback_path="output/Generated Book/Generated Book.m4b",
                playback_status=PlaybackStatus.PACKAGED,
            ),
            [
                _chapter(id=13, book_id=10, mp3_path="output/Generated Book/chapters/01.mp3")
            ],
        )
    )

    service = _make_service(tmp_path, repository, output_dir=output_root)

    deleted = service.delete_book(10)

    assert deleted is True
    assert source_path.exists()
    assert not book_dir.exists()


def test_library_service_delete_book_with_duplicate_title_preserves_sibling_output(tmp_path):
    output_root = tmp_path / "output"

    sibling_dir = output_root / "Shared Title"
    sibling_chapters = sibling_dir / "chapters"
    sibling_chapters.mkdir(parents=True, exist_ok=True)
    (sibling_dir / "Shared Title.m4b").write_text("sibling audio", encoding="utf-8")
    (sibling_chapters / "01.mp3").write_text("sibling chapter", encoding="utf-8")

    deleted_dir = output_root / "Shared Title (2)"
    deleted_chapters = deleted_dir / "chapters"
    deleted_chapters.mkdir(parents=True, exist_ok=True)
    (deleted_dir / "Shared Title.m4b").write_text("deleted audio", encoding="utf-8")
    (deleted_chapters / "01.mp3").write_text("deleted chapter", encoding="utf-8")

    repository = _DeleteRepositoryStub(
        (
            _book(
                id=42,
                title="Shared Title",
                source_path=str(tmp_path / "imports" / "shared.epub"),
                playback_path="output/Shared Title (2)/Shared Title.m4b",
                playback_status=PlaybackStatus.PACKAGED,
            ),
            [
                _chapter(id=42, book_id=42, mp3_path="output/Shared Title (2)/chapters/01.mp3")
            ],
        )
    )

    service = _make_service(tmp_path, repository, output_dir=output_root)

    deleted = service.delete_book(42)

    assert deleted is True
    assert not deleted_dir.exists()
    assert sibling_dir.exists()
    assert (sibling_dir / "Shared Title.m4b").exists()
    assert (sibling_chapters / "01.mp3").exists()


def test_library_service_delete_book_without_paths_does_not_guess_title_directory(tmp_path):
    output_root = tmp_path / "output"
    sibling_dir = output_root / "Shared Title"
    sibling_dir.mkdir(parents=True)
    sibling_audio = sibling_dir / "Shared Title.m4b"
    sibling_audio.write_text("sibling audio", encoding="utf-8")

    repository = _DeleteRepositoryStub(
        (
            _book(
                id=43,
                title="Shared Title",
                source_path=str(tmp_path / "imports" / "shared.epub"),
                playback_status=PlaybackStatus.MISSING,
            ),
            [
                _chapter(id=43, book_id=43, mp3_path=None)
            ],
        )
    )

    service = _make_service(tmp_path, repository, output_dir=output_root)

    assert service.delete_book(43) is True
    assert sibling_audio.exists()


def test_library_service_delete_book_refuses_path_traversal_in_stored_paths(tmp_path):
    output_root = tmp_path / "output"
    temp_root = tmp_path / "temp"
    output_root.mkdir(parents=True, exist_ok=True)
    (temp_root / "web_uploads").mkdir(parents=True, exist_ok=True)

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir(parents=True, exist_ok=True)
    protected_audio = outside_dir / "private.m4b"
    protected_chapter = outside_dir / "private.mp3"
    protected_source = outside_dir / "private_source.epub"
    for artifact in (protected_audio, protected_chapter, protected_source):
        artifact.write_text("user data", encoding="utf-8")

    repository = _DeleteRepositoryStub(
        (
            _book(
                id=77,
                title="Traversal Book",
                source_path="temp/web_uploads/../../outside/private_source.epub",
                playback_path="output/../outside/private.m4b",
                playback_status=PlaybackStatus.PACKAGED,
            ),
            [
                _chapter(id=77, book_id=77, mp3_path="output/../outside/private.mp3")
            ],
        )
    )

    service = _make_service(tmp_path, repository, output_dir=output_root, temp_dir=temp_root)

    deleted = service.delete_book(77)

    assert deleted is True
    assert protected_audio.exists()
    assert protected_chapter.exists()
    assert protected_source.exists()
    assert outside_dir.exists()


def test_library_service_delete_book_with_already_deleted_source_removes_output(tmp_path):
    output_root = tmp_path / "output"
    temp_root = tmp_path / "temp"
    (temp_root / "web_uploads").mkdir(parents=True, exist_ok=True)

    book_dir = output_root / "Gone Source"
    chapters_dir = book_dir / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)
    (book_dir / "Gone Source.m4b").write_text("audio", encoding="utf-8")
    (chapters_dir / "01.mp3").write_text("chapter", encoding="utf-8")

    # Source was a managed upload that the user already deleted from disk.
    missing_source = "temp/web_uploads/already_gone.epub"

    repository = _DeleteRepositoryStub(
        (
            _book(
                id=55,
                title="Gone Source",
                source_path=missing_source,
                playback_path="output/Gone Source/Gone Source.m4b",
                playback_status=PlaybackStatus.PACKAGED,
            ),
            [
                _chapter(id=55, book_id=55, mp3_path="output/Gone Source/chapters/01.mp3")
            ],
        )
    )

    service = _make_service(tmp_path, repository, output_dir=output_root, temp_dir=temp_root)

    deleted = service.delete_book(55)

    assert deleted is True
    assert repository.deleted_ids == [55]
    assert not book_dir.exists()


def test_library_service_open_book_folder_prefers_output_folder(tmp_path):
    source_path = tmp_path / "imports" / "folder-book.pdf"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text("source", encoding="utf-8")
    output_root = tmp_path / "output"
    output_book_dir = output_root / "Folder Book"
    output_book_dir.mkdir(parents=True, exist_ok=True)
    (output_book_dir / "Folder Book.m4b").write_text("audio", encoding="utf-8")

    opened_paths: list[Path] = []
    repository = _DeleteRepositoryStub(
        (
            _book(
                id=9,
                title="Folder Book",
                source_path=str(source_path),
                source_type=SourceType.PDF,
                playback_path="output/Folder Book/Folder Book.m4b",
                playback_status=PlaybackStatus.PACKAGED,
            ),
            [],
        )
    )

    service = _make_service(
        tmp_path,
        repository,
        output_dir=output_root,
        folder_opener=lambda folder: opened_paths.append(folder),
    )

    opened_folder = service.open_book_folder(9)

    assert opened_folder == output_book_dir
    assert opened_paths == [output_book_dir]


def test_library_service_open_book_folder_falls_back_to_source_parent_when_output_missing(tmp_path):
    source_path = tmp_path / "imports" / "folder-book.pdf"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text("source", encoding="utf-8")

    opened_paths: list[Path] = []
    repository = _DeleteRepositoryStub(
        (
            _book(
                id=9,
                title="Folder Book",
                source_path=str(source_path),
                source_type=SourceType.PDF,
                playback_status=PlaybackStatus.MISSING,
            ),
            [],
        )
    )

    service = _make_service(tmp_path, repository, folder_opener=lambda folder: opened_paths.append(folder))

    opened_folder = service.open_book_folder(9)

    assert opened_folder == source_path.parent
    assert opened_paths == [source_path.parent]


def test_library_service_open_book_folder_creates_output_folder_when_artifacts_and_source_missing(tmp_path):
    output_root = tmp_path / "output"
    expected_output_dir = output_root / "Missing Source Book"

    opened_paths: list[Path] = []
    repository = _DeleteRepositoryStub(
        (
            _book(
                id=9,
                title="Missing Source Book",
                source_path=str(tmp_path / "imports" / "missing.epub"),
                playback_status=PlaybackStatus.MISSING,
            ),
            [],
        )
    )

    service = _make_service(
        tmp_path,
        repository,
        output_dir=output_root,
        folder_opener=lambda folder: opened_paths.append(folder),
    )

    opened_folder = service.open_book_folder(9)

    assert opened_folder == expected_output_dir
    assert expected_output_dir.is_dir()
    assert opened_paths == [expected_output_dir]


def test_library_service_open_book_folder_routes_managed_uploads_to_app_output(tmp_path):
    app_output_root = tmp_path / "app-support" / "output"
    app_temp_root = tmp_path / "app-support" / "temp"
    managed_source_path = app_temp_root / "web_uploads" / "folder-book.pdf"
    managed_source_path.parent.mkdir(parents=True, exist_ok=True)
    managed_source_path.write_text("source", encoding="utf-8")
    expected_output_dir = app_output_root / "Folder Book"

    opened_paths: list[Path] = []
    repository = _DeleteRepositoryStub(
        (
            _book(
                id=9,
                title="Folder Book",
                source_path=str(managed_source_path),
                source_type=SourceType.PDF,
                playback_status=PlaybackStatus.MISSING,
            ),
            [],
        )
    )

    service = _make_service(
        tmp_path,
        repository,
        output_dir=app_output_root,
        temp_dir=app_temp_root,
        folder_opener=lambda folder: opened_paths.append(folder),
    )

    opened_folder = service.open_book_folder(9)

    assert opened_folder == expected_output_dir
    assert expected_output_dir.is_dir()
    assert opened_paths == [expected_output_dir]
    assert opened_folder != managed_source_path.parent


def test_app_controller_cleans_up_owned_repository_connection(monkeypatch, tmp_path):
    class _FakeLibraryService:
        def __init__(
            self,
            repository,
            output_dir="__missing__",
            temp_dir="__missing__",
            storage_root="__missing__",
            folder_opener=None,
        ):
            self.repository = repository
            self.output_dir = output_dir
            self.temp_dir = temp_dir
            self.storage_root = storage_root

    class _FakeVoiceService:
        def __init__(self, config, tts_factory=None):
            self.config = config

        def validate_tts_runtime(self, *args, **kwargs):
            return None

        def cleanup(self):
            return None

    class _FakeConversionService:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def cancel_conversion(self, expected_job_id=None):
            return False


    class _FakeSharedWorkerPoolManager:
        def __init__(self, max_workers):
            self.max_workers = max_workers
            self.shutdown_called = False

        def snapshot(self):
            return {"reuse_hits": 0, "restarts": 0}

        def shutdown(self, *, timeout_seconds=None):  # noqa: ANN001
            self.shutdown_called = True
            return True


    monkeypatch.setattr(app_controller_module, "LibraryService", _FakeLibraryService)
    monkeypatch.setattr(app_controller_module, "VoiceService", _FakeVoiceService)
    monkeypatch.setattr(app_controller_module, "ConversionService", _FakeConversionService)
    monkeypatch.setattr(app_controller_module, "RuntimeCapabilityService", _FakeRuntimeCapabilityService)
    monkeypatch.setattr(app_controller_module, "SharedWorkerPoolManager", _FakeSharedWorkerPoolManager)
    monkeypatch.setattr(app_controller_module, "GlobalAPILimiter", _FakeGlobalAPILimiter)
    monkeypatch.setattr(app_controller_module, "SQLiteRepository", _FakeSQLiteRepository)

    config = make_app_config(tmp_path, db_path=tmp_path / "data" / "audiobooks.db")

    controller = app_controller_module.AppController(config=config)
    controller.cleanup()

    assert isinstance(controller.repository, _FakeSQLiteRepository)
    assert controller.repository.closed is True


def test_app_controller_cleanup_bounds_conversion_and_worker_shutdown(monkeypatch, tmp_path):
    calls: list[tuple[str, float | None]] = []

    class _FakeLibraryService:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            _ = args, kwargs

    class _FakeVoiceService:
        def __init__(self, config, tts_factory=None):  # noqa: ANN001, ARG002
            self.cleaned = False

        def validate_tts_runtime(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return None

        def cleanup(self):
            self.cleaned = True

    class _FakeConversionService:
        def __init__(self, **kwargs):  # noqa: ANN003
            _ = kwargs

        def cancel_active_job(self, timeout_seconds=None):  # noqa: ANN001
            calls.append(("cancel", timeout_seconds))
            return False


    class _FakeSharedWorkerPoolManager:
        def __init__(self, max_workers):  # noqa: ANN001
            self.max_workers = max_workers

        def snapshot(self):
            return {"reuse_hits": 0, "restarts": 0}

        def shutdown(self, *, timeout_seconds=None):  # noqa: ANN001
            calls.append(("worker_shutdown", timeout_seconds))
            return False

    class _FakeGlobalAPILimiter:
        def __init__(self, concurrency):  # noqa: ANN001
            self.concurrency = concurrency

        def snapshot(self):
            return {"wait_count": 0, "wait_ms_total": 0.0}

        def shutdown(self):
            calls.append(("limiter_shutdown", None))

    class _FakeSQLiteRepository:
        def __init__(self, db_path, *, output_dir=None, temp_dir=None):  # noqa: ANN001
            self.db_path = db_path
            self.output_dir = output_dir
            self.temp_dir = temp_dir

        def close(self):
            calls.append(("repo_close", None))

    monkeypatch.setattr(app_controller_module, "LibraryService", _FakeLibraryService)
    monkeypatch.setattr(app_controller_module, "VoiceService", _FakeVoiceService)
    monkeypatch.setattr(app_controller_module, "ConversionService", _FakeConversionService)
    monkeypatch.setattr(app_controller_module, "RuntimeCapabilityService", _FakeRuntimeCapabilityService)
    monkeypatch.setattr(app_controller_module, "SharedWorkerPoolManager", _FakeSharedWorkerPoolManager)
    monkeypatch.setattr(app_controller_module, "GlobalAPILimiter", _FakeGlobalAPILimiter)
    monkeypatch.setattr(app_controller_module, "SQLiteRepository", _FakeSQLiteRepository)

    config = make_app_config(tmp_path, db_path=tmp_path / "data" / "audiobooks.db")
    controller = app_controller_module.AppController(config=config)

    assert controller.cleanup() is False

    assert calls[:3] == [
        ("cancel", 2.0),
        ("worker_shutdown", 2.0),
        ("cancel", 0.5),
    ]
    assert ("limiter_shutdown", None) not in calls
    assert ("repo_close", None) not in calls


def test_app_controller_desktop_shutdown_cleanup_budget_matches_cleanup_timeouts():
    controller = app_controller_module.AppController
    expected_budget = (
        controller._CLEANUP_CONVERSION_CANCEL_TIMEOUT_SECONDS
        + controller._CLEANUP_WORKER_SHUTDOWN_TIMEOUT_SECONDS
        + controller._CLEANUP_POST_WORKER_CANCEL_TIMEOUT_SECONDS
    )
    assert controller.DESKTOP_SHUTDOWN_CLEANUP_BUDGET_SECONDS >= expected_budget
    assert controller.DESKTOP_SHUTDOWN_ACK_EVENT == "shutdown"


def test_app_controller_cleanup_closes_repo_before_shutdown_ack(monkeypatch, tmp_path, capsys):
    events: list[str] = []
    monkeypatch.setenv("AUDIOBOOK_DESKTOP_READY_FILE", str(tmp_path / "backend-ready.json"))

    class _FakeLibraryService:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            _ = args, kwargs

    class _FakeVoiceService:
        def __init__(self, config, tts_factory=None):  # noqa: ANN001, ARG002
            _ = config

        def validate_tts_runtime(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return None

        def cleanup(self):
            events.append("voice_cleanup")

    class _FakeConversionService:
        def __init__(self, **kwargs):  # noqa: ANN003
            _ = kwargs

        def cancel_active_job(self, timeout_seconds=None):  # noqa: ANN001
            events.append(f"cancel:{timeout_seconds}")
            return True


    class _FakeGlobalAPILimiter:
        def __init__(self, concurrency):  # noqa: ANN001
            _ = concurrency

        def snapshot(self):
            return {"wait_count": 0, "wait_ms_total": 0.0}

        def shutdown(self):
            events.append("limiter_shutdown")

    class _FakeSQLiteRepository:
        def __init__(self, db_path, *, output_dir=None, temp_dir=None):  # noqa: ANN001
            self.db_path = db_path
            self.output_dir = output_dir
            self.temp_dir = temp_dir

        def close(self):
            events.append("repo_close")

    monkeypatch.setattr(app_controller_module, "LibraryService", _FakeLibraryService)
    monkeypatch.setattr(app_controller_module, "VoiceService", _FakeVoiceService)
    monkeypatch.setattr(app_controller_module, "ConversionService", _FakeConversionService)
    monkeypatch.setattr(app_controller_module, "RuntimeCapabilityService", _FakeRuntimeCapabilityService)
    monkeypatch.setattr(app_controller_module, "GlobalAPILimiter", _FakeGlobalAPILimiter)
    monkeypatch.setattr(app_controller_module, "SQLiteRepository", _FakeSQLiteRepository)

    config = make_app_config(tmp_path, db_path=tmp_path / "data" / "audiobooks.db")
    controller = app_controller_module.AppController(config=config)
    controller.cleanup()

    assert events[-1] == "repo_close"
    captured = capsys.readouterr()
    assert captured.out.strip().endswith('{"event":"shutdown"}')


class _MultiBookDeleteRepositoryStub:
    def __init__(self, books: dict[int, tuple[Book, list[Chapter]]]) -> None:
        self._books = dict(books)
        self.deleted_ids: list[int] = []

    def get_book_with_chapters(self, book_id: int):
        return self._books.get(book_id)

    def list_books(self, filters):  # noqa: ANN001
        from modules.storage.models import BookSummary

        summaries = []
        for book_id, (book, chapters) in self._books.items():
            summaries.append(
                BookSummary(
                    id=book_id,
                    title=book.title,
                    author=book.author,
                    total_chapters=book.total_chapters,
                    created_at=book.created_at,
                    completed_chapters=len(chapters),
                    source_path=book.source_path,
                )
            )
        return summaries

    def delete_book(self, book_id: int) -> bool:
        if book_id not in self._books:
            return False
        self.deleted_ids.append(book_id)
        del self._books[book_id]
        return True


class _FailingDeleteRepositoryStub(_DeleteRepositoryStub):
    def __init__(self, detail, *, error: Exception | None = None) -> None:
        super().__init__(detail)
        self.error = error or sqlite3.OperationalError("forced delete failure")

    def delete_book(self, book_id: int) -> bool:
        if self.detail is None or self.detail[0].id != book_id:
            return False
        raise self.error


def test_library_service_delete_book_removes_durable_app_owned_source(tmp_path):
    data_dir = tmp_path / "data"
    sources_dir = data_dir / "sources"
    sources_dir.mkdir(parents=True)
    durable_source = sources_dir / "uploaded.epub"
    durable_source.write_text("durable source", encoding="utf-8")

    output_root = tmp_path / "output"
    book_dir = output_root / "Uploaded"
    chapters_dir = book_dir / "chapters"
    chapters_dir.mkdir(parents=True)
    (chapters_dir / "01.mp3").write_text("chapter", encoding="utf-8")

    repository = _DeleteRepositoryStub(
        (
            _book(id=8, title="Uploaded", source_path=str(durable_source)),
            [
                _chapter(id=12, book_id=8, mp3_path="output/Uploaded/chapters/01.mp3")
            ],
        )
    )

    service = _make_service(tmp_path, repository, output_dir=output_root, storage_root=tmp_path)

    deleted = service.delete_book(8)

    assert deleted is True
    assert repository.deleted_ids == [8]
    assert not durable_source.exists()
    assert not book_dir.exists()


def test_library_service_delete_book_removes_durable_source_beside_active_database(tmp_path):
    from modules.storage.models import BookCreate

    (tmp_path / "data" / "sources").mkdir(parents=True)
    state_dir = tmp_path / "state"
    durable_source = state_dir / "sources" / "book.epub"
    durable_source.parent.mkdir(parents=True)
    durable_source.write_text("durable source", encoding="utf-8")

    repository = SQLiteRepository(state_dir / "audiobooks.db")
    try:
        book = repository.create_book(
            BookCreate(title="Book", source_path=str(durable_source), source_type=SourceType.EPUB)
        )
        service = _make_service(tmp_path, repository, storage_root=tmp_path)

        assert service.delete_book(book.id) is True
        assert not durable_source.exists()
    finally:
        repository.close()


def test_library_service_delete_book_preserves_shared_durable_source(tmp_path):
    data_dir = tmp_path / "data"
    sources_dir = data_dir / "sources"
    sources_dir.mkdir(parents=True)
    shared_source = sources_dir / "shared.epub"
    shared_source.write_text("shared source", encoding="utf-8")

    output_root = tmp_path / "output"
    first_dir = output_root / "First"
    second_dir = output_root / "Second"
    for book_dir in (first_dir, second_dir):
        chapters_dir = book_dir / "chapters"
        chapters_dir.mkdir(parents=True)
        (chapters_dir / "01.mp3").write_text("chapter", encoding="utf-8")

    repository = _MultiBookDeleteRepositoryStub(
        {
            1: (
                _book(id=1, title="First", source_path=str(shared_source)),
                [
                    _chapter(id=1, book_id=1, mp3_path="output/First/chapters/01.mp3")
                ],
            ),
            2: (
                _book(
                    id=2,
                    title="Second",
                    source_path=str(shared_source),
                    created_at=datetime(2026, 4, 1, 9, 10, 0),
                    updated_at=datetime(2026, 4, 1, 9, 15, 0),
                ),
                [
                    _chapter(id=2, book_id=2, mp3_path="output/Second/chapters/01.mp3")
                ],
            ),
        }
    )

    service = _make_service(tmp_path, repository, output_dir=output_root, storage_root=tmp_path)

    deleted = service.delete_book(1)

    assert deleted is True
    assert shared_source.exists()
    assert not first_dir.exists()
    assert second_dir.exists()


def test_library_service_delete_book_removes_durable_source_after_last_reference(tmp_path):
    data_dir = tmp_path / "data"
    sources_dir = data_dir / "sources"
    sources_dir.mkdir(parents=True)
    shared_source = sources_dir / "shared.epub"
    shared_source.write_text("shared source", encoding="utf-8")

    output_root = tmp_path / "output"
    second_dir = output_root / "Second"
    chapters_dir = second_dir / "chapters"
    chapters_dir.mkdir(parents=True)
    (chapters_dir / "01.mp3").write_text("chapter", encoding="utf-8")

    repository = _MultiBookDeleteRepositoryStub(
        {
            2: (
                _book(
                    id=2,
                    title="Second",
                    source_path=str(shared_source),
                    created_at=datetime(2026, 4, 1, 9, 10, 0),
                    updated_at=datetime(2026, 4, 1, 9, 15, 0),
                ),
                [
                    _chapter(id=2, book_id=2, mp3_path="output/Second/chapters/01.mp3")
                ],
            ),
        }
    )

    service = _make_service(tmp_path, repository, output_dir=output_root, storage_root=tmp_path)

    deleted = service.delete_book(2)

    assert deleted is True
    assert not shared_source.exists()
    assert not second_dir.exists()


def test_library_service_delete_book_preserves_user_owned_source_outside_managed_roots(tmp_path):
    user_source = tmp_path / "my-library" / "owned.epub"
    user_source.parent.mkdir(parents=True)
    user_source.write_text("user owned", encoding="utf-8")

    output_root = tmp_path / "output"
    book_dir = output_root / "Owned"
    chapters_dir = book_dir / "chapters"
    chapters_dir.mkdir(parents=True)
    (chapters_dir / "01.mp3").write_text("chapter", encoding="utf-8")

    repository = _DeleteRepositoryStub(
        (
            _book(id=21, title="Owned", source_path=str(user_source)),
            [
                _chapter(id=21, book_id=21, mp3_path="output/Owned/chapters/01.mp3")
            ],
        )
    )

    service = _make_service(tmp_path, repository, output_dir=output_root, storage_root=tmp_path)

    deleted = service.delete_book(21)

    assert deleted is True
    assert user_source.exists()
    assert not book_dir.exists()


def test_library_service_delete_book_refuses_path_traversal_for_durable_source(tmp_path):
    data_dir = tmp_path / "data"
    sources_dir = data_dir / "sources"
    sources_dir.mkdir(parents=True)

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir(parents=True)
    protected_source = outside_dir / "private.epub"
    protected_source.write_text("user data", encoding="utf-8")

    output_root = tmp_path / "output"
    book_dir = output_root / "Traversal"
    chapters_dir = book_dir / "chapters"
    chapters_dir.mkdir(parents=True)
    (chapters_dir / "01.mp3").write_text("chapter", encoding="utf-8")

    repository = _DeleteRepositoryStub(
        (
            _book(id=88, title="Traversal", source_path="data/sources/../../outside/private.epub"),
            [
                _chapter(id=88, book_id=88, mp3_path="output/Traversal/chapters/01.mp3")
            ],
        )
    )

    service = _make_service(tmp_path, repository, output_dir=output_root, storage_root=tmp_path)

    deleted = service.delete_book(88)

    assert deleted is True
    assert protected_source.exists()
    assert not book_dir.exists()


def test_library_service_delete_book_restores_artifacts_when_repository_delete_fails(tmp_path):
    data_dir = tmp_path / "data"
    sources_dir = data_dir / "sources"
    sources_dir.mkdir(parents=True)
    durable_source = sources_dir / "rollback.epub"
    durable_source.write_text("durable source", encoding="utf-8")

    output_root = tmp_path / "output"
    book_dir = output_root / "Rollback"
    chapters_dir = book_dir / "chapters"
    chapters_dir.mkdir(parents=True)
    chapter_audio = chapters_dir / "01.mp3"
    chapter_audio.write_text("chapter", encoding="utf-8")

    detail = (
        _book(id=99, title="Rollback", source_path=str(durable_source)),
        [
            _chapter(id=99, book_id=99, mp3_path="output/Rollback/chapters/01.mp3")
        ],
    )
    repository = _FailingDeleteRepositoryStub(detail)

    service = _make_service(tmp_path, repository, output_dir=output_root, storage_root=tmp_path)

    with pytest.raises(sqlite3.OperationalError, match="forced delete failure"):
        service.delete_book(99)

    assert repository.deleted_ids == []
    assert durable_source.exists()
    assert book_dir.exists()
    assert chapter_audio.exists()


def test_library_service_delete_book_does_not_restore_after_successful_db_delete(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    sources_dir = data_dir / "sources"
    sources_dir.mkdir(parents=True)
    durable_source = sources_dir / "commit-fail.epub"
    durable_source.write_text("durable source", encoding="utf-8")

    output_root = tmp_path / "output"
    book_dir = output_root / "CommitFail"
    chapters_dir = book_dir / "chapters"
    chapters_dir.mkdir(parents=True)
    chapter_audio = chapters_dir / "01.mp3"
    chapter_audio.write_text("chapter", encoding="utf-8")

    detail = (
        _book(id=100, title="CommitFail", source_path=str(durable_source)),
        [
            _chapter(id=100, book_id=100, mp3_path="output/CommitFail/chapters/01.mp3")
        ],
    )
    repository = _DeleteRepositoryStub(detail)
    service = _make_service(tmp_path, repository, output_dir=output_root, storage_root=tmp_path)

    def boom(_artifacts):  # noqa: ANN001
        raise OSError("forced commit failure")

    monkeypatch.setattr(service, "_commit_staged_artifacts", boom)

    # DB deletion already succeeded; report the pending files explicitly.
    result = service.delete_book(100)
    assert result["deleted"] is True
    assert result["files_pending"] is True
    assert (tmp_path / "temp" / "delete_staging" / result["path"]).is_dir()
    assert not Path(result["path"]).is_absolute()

    assert repository.deleted_ids == [100]
    assert repository.get_book_with_chapters(100) is None
    # Original paths must stay vacated; do not resurrect after DB success.
    assert not durable_source.exists()
    assert not book_dir.exists()
    assert not chapter_audio.exists()
    staging_root = tmp_path / "temp" / "delete_staging"
    assert staging_root.exists()
    staged = list(staging_root.rglob("*"))
    assert any(path.is_file() or path.is_dir() for path in staged)


def test_default_folder_opener_uses_os_startfile_on_windows(tmp_path, monkeypatch):
    from modules.app.services import library_service as library_service_module

    opened: list[str] = []
    monkeypatch.setattr(library_service_module.os, "name", "nt")
    monkeypatch.setattr(
        library_service_module.os,
        "startfile",
        lambda path: opened.append(path),
        raising=False,
    )

    def _fail_run(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("subprocess must not be used to open folders on Windows")

    monkeypatch.setattr(library_service_module.subprocess, "run", _fail_run)

    library_service_module._default_folder_opener(tmp_path)

    assert opened == [str(tmp_path)]


def test_default_folder_opener_never_launches_a_file(tmp_path, monkeypatch):
    from modules.app.services import library_service as library_service_module

    launched: list[object] = []
    monkeypatch.setattr(library_service_module.os, "startfile", launched.append, raising=False)
    monkeypatch.setattr(
        library_service_module.subprocess, "run", lambda *args, **_kwargs: launched.append(args),
    )
    planted = tmp_path / "Book.exe"
    planted.write_bytes(b"MZ")

    for target in (planted, tmp_path / "missing"):
        with pytest.raises(RuntimeError, match="failed to open folder"):
            library_service_module._default_folder_opener(target)

    assert launched == []


def test_default_folder_opener_windows_failure_raises_runtime_error(tmp_path, monkeypatch):
    from modules.app.services import library_service as library_service_module

    def _fail_startfile(_path):  # noqa: ANN001, ANN202
        raise OSError("no folder association")

    monkeypatch.setattr(library_service_module.os, "name", "nt")
    monkeypatch.setattr(
        library_service_module.os, "startfile", _fail_startfile, raising=False
    )

    with pytest.raises(RuntimeError, match="failed to open folder"):
        library_service_module._default_folder_opener(tmp_path)


@pytest.mark.parametrize("source_path", ["", None])
def test_library_service_reports_source_unavailable_for_empty_source_path(tmp_path, source_path):
    book, chapters = _RepositoryStub().get_book_with_chapters(7)
    book.source_path = source_path
    service = _make_service(tmp_path, _DeleteRepositoryStub((book, chapters)))

    assert service.get_book(7)["source_available"] is False


@pytest.mark.parametrize("suffix,content,expected", [
    (".txt", b"A quiet lake.", "Original Book"),
    (".md", b"A quiet lake.", "Original Book"),
    (".md", b"# Embedded title\nA quiet lake.", "Embedded title"),
])
def test_uploaded_source_preserves_filename_fallback(tmp_path, suffix, content, expected):
    from io import BytesIO

    service = _make_service(tmp_path, _RepositoryStub(), temp_dir=tmp_path)
    result = service.upload_source(filename=f"Original Book{suffix}", file_obj=BytesIO(content))
    assert result["preflight"]["title"] == expected


@pytest.mark.parametrize("suffix", [".pdf", ".epub"])
@pytest.mark.parametrize("embedded_title", [None, "Embedded title"])
def test_uploaded_document_prefers_embedded_title_then_original_filename(tmp_path, suffix, embedded_title):
    from io import BytesIO

    path = tmp_path / f"fixture{suffix}"
    if suffix == ".pdf":
        import fitz
        with fitz.open() as document:
            page = document.new_page()
            page.insert_text((72, 72), "A quiet lake.")
            if embedded_title:
                document.set_metadata({"title": embedded_title})
            document.save(path)
    else:
        from ebooklib import epub
        book = epub.EpubBook()
        book.set_identifier("upload-title-test")
        book.set_title(embedded_title or "")
        book.set_language("en")
        chapter = epub.EpubHtml(title="Chapter", file_name="chapter.xhtml", lang="en")
        chapter.content = "<h1>Chapter</h1><p>A quiet lake.</p>"
        book.add_item(chapter)
        book.toc = (chapter,)
        book.spine = [chapter]
        book.add_item(epub.EpubNcx())
        epub.write_epub(str(path), book)
    service = _make_service(tmp_path, _RepositoryStub(), temp_dir=tmp_path / "uploads")
    result = service.upload_source(filename=f"Original Book{suffix}", file_obj=BytesIO(path.read_bytes()))
    assert result["preflight"]["title"] == (embedded_title or "Original Book")


def _create_deletion_book(tmp_path):
    from modules.storage.models import BookCreate, ChapterCreate

    output = tmp_path / "output"
    directory = output / "Book"
    audio = directory / "chapters" / "01.mp3"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"chapter audio")
    source = tmp_path / "data" / "sources" / "book.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source bytes")
    repo = SQLiteRepository(tmp_path / "data" / "library.db")
    book, _ = repo.create_book_with_chapters(
        BookCreate(title="Book", source_path=str(source), source_type=SourceType.TXT, playback_path=str(audio)),
        [ChapterCreate(book_id=0, chapter_number=1, title="First", mp3_path=str(audio))],
    )
    service = _make_service(tmp_path, repo, output_dir=output, storage_root=tmp_path)
    return repo, service, book, directory, audio, source


@pytest.mark.parametrize("crash_point", ["first_move", "before_database", "after_database"])
def test_new_service_reconciles_in_process_deletion_interruption(tmp_path, monkeypatch, crash_point):
    import json
    import os

    repo, service, book, directory, audio, source = _create_deletion_book(tmp_path)
    try:
        replace = os.replace
        delete = repo.delete_book
        manifests = []

        def interrupted_move(src, dst):
            if Path(src) == directory:
                manifests.extend((tmp_path / "temp" / "delete_staging").glob("*/transaction.json"))
                if manifests:
                    payload = json.loads(manifests[0].read_text(encoding="utf-8"))
                    assert payload["book_id"] == book.id
                    assert {item["original"] for item in payload["artifacts"]} == {str(directory), str(source)}
                    assert all(Path(item["staged"]).is_absolute() for item in payload["artifacts"])
            replace(src, dst)
            if Path(src) == directory and crash_point == "first_move":
                raise KeyboardInterrupt()

        def interrupted_delete(book_id):
            if crash_point == "after_database":
                delete(book_id)
            raise KeyboardInterrupt()

        with monkeypatch.context() as patch:
            patch.setattr(os, "replace", interrupted_move)
            if crash_point != "first_move":
                patch.setattr(repo, "delete_book", interrupted_delete)
            with pytest.raises(KeyboardInterrupt):
                service.delete_book(book.id)
        assert not audio.exists()
        restored = _make_service(tmp_path, repo, storage_root=tmp_path)
        if crash_point == "after_database":
            assert repo.get_book(book.id) is None
            assert not directory.exists()
            assert not source.exists()
        else:
            assert repo.get_book(book.id) is not None
            assert source.read_bytes() == b"source bytes"
            assert audio.read_bytes() == b"chapter audio"
            assert restored.get_book_playback_file(book.id) == audio
        assert manifests
        assert not any((tmp_path / "temp" / "delete_staging").iterdir())
    finally:
        repo.close()


def test_new_service_retries_failed_staged_artifact_restore(tmp_path, monkeypatch, caplog):
    import os

    repo, service, book, directory, audio, source = _create_deletion_book(tmp_path)
    try:
        replace = os.replace

        def fail_source_and_restore(src, dst):
            if Path(src) == source or Path(dst) == directory:
                raise PermissionError("File is in use")
            return replace(src, dst)

        with monkeypatch.context() as patch:
            patch.setattr(os, "replace", fail_source_and_restore)
            with pytest.raises(RuntimeError):
                service.delete_book(book.id)
            _make_service(tmp_path, repo, storage_root=tmp_path)
            assert "Failed to restore staged artifact" in caplog.text
            from modules.storage.maintenance import prune_stale_records_and_files

            prune_stale_records_and_files(
                repo.db_path, tmp_path / "output", tmp_path / "temp", aggressive=True, grace_seconds=0,
            )
            assert repo.get_book(book.id) is not None
        _make_service(tmp_path, repo, storage_root=tmp_path)
        assert repo.get_book(book.id) is not None
        assert audio.read_bytes() == b"chapter audio"
        assert source.read_bytes() == b"source bytes"
    finally:
        repo.close()


@pytest.mark.parametrize("fail_later", [False, True])
def test_delete_stages_artifacts_on_their_own_volume(tmp_path, monkeypatch, fail_later):
    import errno
    import os

    repo, service, book, directory, audio, source = _create_deletion_book(tmp_path)
    try:
        replace = os.replace
        volumes = [tmp_path / "output", source.parent, tmp_path / "temp"]

        def volume(path):
            return next((root for root in volumes if Path(path).is_relative_to(root)), tmp_path)

        def replace_on_volume(src, dst):
            if volume(src) != volume(dst):
                raise OSError(errno.EXDEV, "Cross-device link")
            if fail_later and Path(src) == source:
                raise PermissionError("File is in use")
            return replace(src, dst)

        monkeypatch.setattr(os, "replace", replace_on_volume)
        if fail_later:
            with pytest.raises(RuntimeError, match="files are in use"):
                service.delete_book(book.id)
            assert audio.read_bytes() == b"chapter audio"
            assert source.read_bytes() == b"source bytes"
            assert repo.get_book(book.id) is not None
        else:
            assert service.delete_book(book.id) is True
            assert not directory.exists()
            assert not source.exists()
            assert repo.get_book(book.id) is None
    finally:
        repo.close()


@pytest.mark.parametrize("error, message", [(18, "filesystem"), (13, "permission"), (1, "permission")])
def test_delete_reports_filesystem_and_permission_errors(tmp_path, monkeypatch, error, message):
    import os

    repo, service, book, directory, audio, source = _create_deletion_book(tmp_path)
    try:
        replace = os.replace

        def fail_artifact(src, dst):
            if Path(src) == directory:
                raise OSError(error, "Cannot rename")
            return replace(src, dst)

        monkeypatch.setattr(os, "replace", fail_artifact)
        with pytest.raises(RuntimeError, match=message):
            service.delete_book(book.id)
        assert audio.read_bytes() == b"chapter audio"
        assert source.read_bytes() == b"source bytes"
        assert repo.get_book(book.id) is not None
    finally:
        repo.close()


def test_delete_moved_book_preserves_reused_directory(tmp_path):
    from modules.storage.models import BookCreate, ChapterCreate

    repo, service, book, directory, audio, source = _create_deletion_book(tmp_path)
    try:
        moved = directory.with_name("Moved")
        directory.rename(moved)
        audio.parent.mkdir(parents=True)
        audio.write_bytes(b"newer audio")
        newer_source = directory / ".orpheus-source" / "new.txt"
        newer_source.parent.mkdir()
        newer_source.write_bytes(b"newer source")
        newer, newer_chapters = repo.create_book_with_chapters(
            BookCreate(title="Book", source_path=str(newer_source), source_type=SourceType.TXT, playback_path=str(audio)),
            [ChapterCreate(book_id=0, chapter_number=1, title="New chapter", mp3_path=str(audio))],
        )
        assert service.delete_book(book.id) is True
        assert audio.read_bytes() == b"newer audio"
        assert newer_source.read_bytes() == b"newer source"
        assert repo.get_book_with_chapters(newer.id)[1] == newer_chapters
        assert (moved / "chapters" / "01.mp3").read_bytes() == b"chapter audio"
    finally:
        repo.close()


def test_cleanup_finishes_committed_deletion_on_each_volume(tmp_path, monkeypatch):
    from modules.storage.maintenance import prune_stale_records_and_files

    repo, service, book, directory, audio, source = _create_deletion_book(tmp_path)
    try:
        def fail_commit(_artifacts):
            raise OSError("File is in use")

        monkeypatch.setattr(service, "_commit_staged_artifacts", fail_commit)
        result = service.delete_book(book.id)
        assert result["files_pending"] is True
        prune_stale_records_and_files(
            repo.db_path, tmp_path / "output", tmp_path / "temp", aggressive=True, grace_seconds=0,
        )
        assert not list(tmp_path.rglob(".orpheus-delete-staging"))
        assert not (tmp_path / "temp" / "delete_staging" / result["path"]).exists()
        assert repo.get_book(book.id) is None
        assert not directory.exists()
        assert not source.exists()
    finally:
        repo.close()


@pytest.mark.parametrize("reference", ["source", "playback", "chapter"])
def test_delete_removes_only_own_files_from_a_shared_directory(tmp_path, reference):
    from modules.storage.models import BookCreate, ChapterCreate

    repo, service, book, directory, audio, source = _create_deletion_book(tmp_path)
    try:
        other_file = directory / "other.mp3"
        other_file.write_bytes(b"other book")
        other = repo.create_book(BookCreate(
            title="Other", source_path=str(other_file) if reference == "source" else "other.txt",
            source_type=SourceType.TXT, playback_path=str(other_file) if reference == "playback" else None,
        ))
        if reference == "chapter":
            repo.create_chapter(ChapterCreate(book_id=other.id, chapter_number=1, title="Other", mp3_path=str(other_file)))
        assert service.delete_book(book.id) is True
        assert not audio.exists()
        assert not source.exists()
        assert other_file.read_bytes() == b"other book"
        assert repo.get_book(other.id) is not None
        assert not list(tmp_path.rglob(".orpheus-delete-staging"))
    finally:
        repo.close()


def test_delete_preserves_source_and_audio_referenced_through_aliases(tmp_path):
    from modules.storage.models import BookCreate, ChapterCreate

    repo, service, book, directory, audio, source = _create_deletion_book(tmp_path)
    try:
        alias = tmp_path / "shared-source.txt"
        try:
            alias.symlink_to(source)
        except OSError as error:
            pytest.skip(f"File symlinks are unavailable: {error}")
        other, _ = repo.create_book_with_chapters(
            BookCreate(title="Other", source_path=str(alias), source_type=SourceType.TXT),
            [ChapterCreate(book_id=0, chapter_number=1, title="Shared", mp3_path="output/Book/chapters/01.mp3")],
        )
        assert service.delete_book(book.id) is True
        assert audio.read_bytes() == b"chapter audio"
        assert source.read_bytes() == b"source bytes"
        assert repo.get_book(other.id) is not None
    finally:
        repo.close()


def test_sweep_removes_stale_staging_entry_whose_manifest_was_never_published(tmp_path):
    import os
    import time

    from modules.storage.maintenance import DEFAULT_CLEANUP_GRACE_SECONDS

    service = _make_service(tmp_path, _RepositoryStub())
    entry = tmp_path / "temp" / "delete_staging" / "interrupted"
    entry.mkdir(parents=True)
    pending = entry / "transaction.json.tmp"
    pending.write_text('{"book_id":', encoding="utf-8")
    stale = time.time() - DEFAULT_CLEANUP_GRACE_SECONDS - 60
    for path in (pending, entry):
        os.utime(path, (stale, stale))

    assert service.sweep_delete_staging() == 1
    assert not entry.exists()


def test_case_alias_of_output_root_keeps_audio_playable(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    alias = tmp_path / "Output"
    if not alias.exists():
        pytest.skip("Filesystem is case-sensitive")
    audio = output / "Book" / "01.mp3"
    audio.parent.mkdir()
    audio.write_bytes(b"audio")
    book, chapters = _RepositoryStub().get_book_with_chapters(7)
    book.playback_path = str(audio)
    chapters[0].mp3_path = str(audio)
    service = _make_service(tmp_path, _DeleteRepositoryStub((book, chapters)), output_dir=alias)

    assert service.get_book_playback_file(7) == audio
    assert service.get_book_playback_file(7, 1) == audio


def test_cleaning_preview_rejects_sidecar_replaced_with_link_before_open(tmp_path, monkeypatch):
    output = tmp_path / "output"
    directory = output / "Book" / "chapters"
    directory.mkdir(parents=True)
    sidecar = directory / "chapter_01_before_cleaning.md"
    sidecar.write_text("public text", encoding="utf-8")
    secret = tmp_path / "private.txt"
    secret.write_text("private text", encoding="utf-8")
    book, chapters = _RepositoryStub().get_book_with_chapters(7)
    chapters[0].mp3_path = str(directory / "01.mp3")
    service = _make_service(tmp_path, _DeleteRepositoryStub((book, chapters)), output_dir=output)
    is_file = Path.is_file

    def swap_after_check(path):
        valid = is_file(path)
        if path == sidecar and valid and not path.is_symlink():
            path.unlink()
            path.symlink_to(secret)
        return valid

    monkeypatch.setattr(Path, "is_file", swap_after_check)

    preview = service.get_chapter_cleaning_preview(7, 1)

    assert preview["before_text"] == ""
    assert preview["available"] is False
    assert secret.read_text(encoding="utf-8") == "private text"


@pytest.mark.parametrize("unexpected", ["other.txt", "directory", "link"])
def test_sweep_preserves_stale_manifestless_entry_with_unrecognized_contents(tmp_path, unexpected):
    import os
    import time

    from modules.storage.maintenance import DEFAULT_CLEANUP_GRACE_SECONDS

    service = _make_service(tmp_path, _RepositoryStub())
    entry = tmp_path / "temp" / "delete_staging" / "uncertain"
    entry.mkdir(parents=True)
    marker = entry / ".committed"
    marker.touch()
    child = entry / (".preserve" if unexpected == "link" else unexpected)
    if unexpected == "directory":
        child.mkdir()
    elif unexpected == "link":
        target = tmp_path / "external.txt"
        target.write_bytes(b"keep")
        child.symlink_to(target)
    else:
        child.write_bytes(b"keep")
    stale = time.time() - DEFAULT_CLEANUP_GRACE_SECONDS - 60
    for path in (child, marker, entry):
        os.utime(path, (stale, stale))

    assert service.sweep_delete_staging() == 0
    assert entry.exists()
    assert child.exists()


def test_delete_through_case_alias_preserves_output_root_and_unrelated_audio(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    alias = tmp_path / "Output"
    if not alias.exists():
        pytest.skip("Filesystem is case-sensitive")
    audio = output / "book.mp3"
    audio.write_bytes(b"audio")
    unrelated = output / "other.mp3"
    unrelated.write_bytes(b"other audio")
    book, chapters = _RepositoryStub().get_book_with_chapters(7)
    book.playback_path = str(audio)
    chapters[0].mp3_path = str(audio)
    service = _make_service(tmp_path, _DeleteRepositoryStub((book, chapters)), output_dir=alias)

    assert service.delete_book(7) is True

    assert output.is_dir()
    assert unrelated.read_bytes() == b"other audio"
    assert not audio.exists()
