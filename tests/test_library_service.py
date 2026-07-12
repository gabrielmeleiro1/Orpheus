from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from modules.app.config import AppConfig
from modules.app import controller as app_controller_module
from modules.app.services.library_service import LibraryService
from modules.storage.models import Book, Chapter, ConversionMode, PlaybackStatus, SourceType


class _RepositoryStub:
    def __init__(self) -> None:
        self.detail_calls: list[int] = []

    def get_book(self, book_id: int):  # pragma: no cover - should never be called
        raise AssertionError(f"get_book() should not be called for {book_id}")

    def get_chapters(self, book_id: int):  # pragma: no cover - should never be called
        raise AssertionError(f"get_chapters() should not be called for {book_id}")

    def get_book_with_chapters(self, book_id: int):
        self.detail_calls.append(book_id)
        return (
            Book(
                id=book_id,
                title="Detail Book",
                author="Tester",
                source_path="/tmp/source.epub",
                source_type=SourceType.EPUB,
                conversion_mode=ConversionMode.DOCUMENT_AUDIO,
                total_chapters=1,
                playback_path="output/Detail Book/Detail Book.m4a",
                playback_status=PlaybackStatus.PACKAGED,
                playback_position_sec=32.5,
                created_at=datetime(2026, 3, 22, 10, 0, 0),
                updated_at=datetime(2026, 3, 22, 10, 5, 0),
            ),
            [
                Chapter(
                    id=11,
                    book_id=book_id,
                    chapter_number=1,
                    title="Chapter 1",
                    duration_ms=1000,
                    mp3_path="output/Detail Book/chapters/01.mp3",
                )
            ],
        )


def test_library_service_get_book_uses_combined_repository_detail_call():
    repository = _RepositoryStub()
    service = LibraryService(repository)

    payload = service.get_book(7)

    assert repository.detail_calls == [7]
    assert payload is not None
    assert payload["playback_path"] == "output/Detail Book/Detail Book.m4a"
    assert payload["playback_status"] == PlaybackStatus.PACKAGED.value
    assert payload["conversion_mode"] == ConversionMode.DOCUMENT_AUDIO.value
    assert payload["tags"] == ""
    assert payload["favorite"] is False
    assert payload["source_available"] is False
    assert payload["chapters"][0]["number"] == 1


def test_library_service_updates_book_organization_metadata(tmp_path):
    class _UpdateRepositoryStub(_RepositoryStub):
        def __init__(self) -> None:
            super().__init__()
            self.updates = []

        def update_book(self, book_id: int, **updates):  # noqa: ANN003
            self.updates.append((book_id, updates))
            return Book(
                id=book_id,
                title=updates.get("title", "Updated"),
                author=updates.get("author"),
                source_path=str(tmp_path / "source.md"),
                source_type=SourceType.MD,
                conversion_mode=ConversionMode.AUDIOBOOK,
                total_chapters=1,
                playback_path=None,
                playback_status=PlaybackStatus.MISSING,
                playback_position_sec=0.0,
                tags=updates.get("tags", ""),
                favorite=bool(updates.get("favorite", False)),
                created_at=datetime(2026, 4, 1, 9, 0, 0),
                updated_at=datetime(2026, 4, 1, 9, 5, 0),
            )

    repository = _UpdateRepositoryStub()
    service = LibraryService(repository)

    payload = service.update_book_metadata(
        7,
        title="Updated",
        author="New Author",
        tags="fiction, favorite",
        favorite=True,
    )

    assert repository.updates == [
        (7, {"title": "Updated", "author": "New Author", "tags": "fiction, favorite", "favorite": True})
    ]
    assert payload is not None
    assert payload["title"] == "Updated"
    assert payload["favorite"] is True


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

    service = LibraryService(repository, output_dir=tmp_path / "output")

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
            Book(
                id=7,
                title="Delete Me",
                author="Tester",
                source_path=str(source_path),
                source_type=SourceType.EPUB,
                conversion_mode=ConversionMode.AUDIOBOOK,
                total_chapters=1,
                playback_path="output/Delete Me/Delete Me.m4b",
                playback_status=PlaybackStatus.PACKAGED,
                playback_position_sec=0.0,
                created_at=datetime(2026, 4, 1, 9, 0, 0),
                updated_at=datetime(2026, 4, 1, 9, 5, 0),
            ),
            [
                Chapter(
                    id=11,
                    book_id=7,
                    chapter_number=1,
                    title="Chapter 1",
                    duration_ms=1000,
                    mp3_path="output/Delete Me/chapters/01.mp3",
                )
            ],
        )
    )

    service = LibraryService(repository, output_dir=output_root)

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
            Book(
                id=12,
                title="Clean Book",
                author="Tester",
                source_path=str(tmp_path / "source.md"),
                source_type=SourceType.MD,
                conversion_mode=ConversionMode.AUDIOBOOK,
                total_chapters=1,
                playback_path=None,
                playback_status=PlaybackStatus.CHAPTER_AUDIO,
                playback_position_sec=0.0,
                created_at=datetime(2026, 4, 1, 9, 0, 0),
                updated_at=datetime(2026, 4, 1, 9, 5, 0),
            ),
            [
                Chapter(
                    id=21,
                    book_id=12,
                    chapter_number=1,
                    title="Chapter 1",
                    duration_ms=1000,
                    mp3_path=str(chapter_audio),
                )
            ],
        )
    )
    service = LibraryService(repository, output_dir=output_root)

    preview = service.get_chapter_cleaning_preview(12, 1)

    assert preview is not None
    assert preview["available"] is True
    assert "raw text" in preview["before_text"]
    assert "rule text" in preview["llm_input_text"]
    assert "clean text" in preview["cleaned_text"]
    assert preview["trace_rows"][0]["chunk_index"] == 1


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
            Book(
                id=8,
                title="Uploaded",
                author="Tester",
                source_path="temp/web_uploads/uploaded.epub",
                source_type=SourceType.EPUB,
                conversion_mode=ConversionMode.AUDIOBOOK,
                total_chapters=1,
                playback_path=None,
                playback_status=PlaybackStatus.CHAPTER_AUDIO,
                playback_position_sec=0.0,
                created_at=datetime(2026, 4, 1, 9, 0, 0),
                updated_at=datetime(2026, 4, 1, 9, 5, 0),
            ),
            [
                Chapter(
                    id=12,
                    book_id=8,
                    chapter_number=1,
                    title="Chapter 1",
                    duration_ms=1000,
                    mp3_path="output/Uploaded/chapters/01.mp3",
                )
            ],
        )
    )

    service = LibraryService(repository, output_dir=output_root, temp_dir=temp_dir)

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
            Book(
                id=18,
                title="Lookalike",
                author="Tester",
                source_path=str(lookalike_source),
                source_type=SourceType.PDF,
                conversion_mode=ConversionMode.AUDIOBOOK,
                total_chapters=1,
                playback_path=None,
                playback_status=PlaybackStatus.CHAPTER_AUDIO,
                playback_position_sec=0.0,
                created_at=datetime(2026, 4, 1, 9, 0, 0),
                updated_at=datetime(2026, 4, 1, 9, 5, 0),
            ),
            [
                Chapter(
                    id=18,
                    book_id=18,
                    chapter_number=1,
                    title="Chapter 1",
                    duration_ms=1000,
                    mp3_path=str(lookalike_book_dir / "chapters" / "01.mp3"),
                )
            ],
        )
    )
    lookalike_service = LibraryService(
        lookalike_repository,
        output_dir=output_root,
        temp_dir=temp_root,
    )

    assert lookalike_service._is_managed_deletable_source_path(lookalike_source) is False  # noqa: SLF001
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
            Book(
                id=19,
                title="Managed",
                author="Tester",
                source_path=str(managed_source),
                source_type=SourceType.EPUB,
                conversion_mode=ConversionMode.AUDIOBOOK,
                total_chapters=1,
                playback_path=None,
                playback_status=PlaybackStatus.CHAPTER_AUDIO,
                playback_position_sec=0.0,
                created_at=datetime(2026, 4, 1, 9, 0, 0),
                updated_at=datetime(2026, 4, 1, 9, 5, 0),
            ),
            [
                Chapter(
                    id=19,
                    book_id=19,
                    chapter_number=1,
                    title="Chapter 1",
                    duration_ms=1000,
                    mp3_path=str(managed_book_dir / "chapters" / "01.mp3"),
                )
            ],
        )
    )
    managed_service = LibraryService(
        managed_repository,
        output_dir=output_root,
        temp_dir=temp_root,
    )

    assert managed_service._is_managed_deletable_source_path(managed_source) is True  # noqa: SLF001
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
            Book(
                id=19,
                title="Loose Book",
                author="Tester",
                source_path=str(source_path),
                source_type=SourceType.MD,
                conversion_mode=ConversionMode.DOCUMENT_AUDIO,
                total_chapters=1,
                playback_path=str(loose_playback),
                playback_status=PlaybackStatus.PACKAGED,
                playback_position_sec=0.0,
                created_at=datetime(2026, 4, 1, 9, 0, 0),
                updated_at=datetime(2026, 4, 1, 9, 5, 0),
            ),
            [
                Chapter(
                    id=19,
                    book_id=19,
                    chapter_number=1,
                    title="Chapter 1",
                    duration_ms=1000,
                    mp3_path=str(loose_chapter),
                )
            ],
        )
    )

    service = LibraryService(repository, output_dir=output_root)

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
            Book(
                id=10,
                title="Generated Book",
                author="Tester",
                source_path=str(source_path),
                source_type=SourceType.EPUB,
                conversion_mode=ConversionMode.AUDIOBOOK,
                total_chapters=1,
                playback_path="output/Generated Book/Generated Book.m4b",
                playback_status=PlaybackStatus.PACKAGED,
                playback_position_sec=0.0,
                created_at=datetime(2026, 4, 1, 9, 0, 0),
                updated_at=datetime(2026, 4, 1, 9, 5, 0),
            ),
            [
                Chapter(
                    id=13,
                    book_id=10,
                    chapter_number=1,
                    title="Chapter 1",
                    duration_ms=1000,
                    mp3_path="output/Generated Book/chapters/01.mp3",
                )
            ],
        )
    )

    service = LibraryService(repository, output_dir=output_root)

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
            Book(
                id=42,
                title="Shared Title",
                author="Tester",
                source_path=str(tmp_path / "imports" / "shared.epub"),
                source_type=SourceType.EPUB,
                conversion_mode=ConversionMode.AUDIOBOOK,
                total_chapters=1,
                playback_path="output/Shared Title (2)/Shared Title.m4b",
                playback_status=PlaybackStatus.PACKAGED,
                playback_position_sec=0.0,
                created_at=datetime(2026, 4, 1, 9, 0, 0),
                updated_at=datetime(2026, 4, 1, 9, 5, 0),
            ),
            [
                Chapter(
                    id=42,
                    book_id=42,
                    chapter_number=1,
                    title="Chapter 1",
                    duration_ms=1000,
                    mp3_path="output/Shared Title (2)/chapters/01.mp3",
                )
            ],
        )
    )

    service = LibraryService(repository, output_dir=output_root)

    deleted = service.delete_book(42)

    assert deleted is True
    assert not deleted_dir.exists()
    assert sibling_dir.exists()
    assert (sibling_dir / "Shared Title.m4b").exists()
    assert (sibling_chapters / "01.mp3").exists()


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
            Book(
                id=77,
                title="Traversal Book",
                author="Tester",
                source_path="temp/web_uploads/../../outside/private_source.epub",
                source_type=SourceType.EPUB,
                conversion_mode=ConversionMode.AUDIOBOOK,
                total_chapters=1,
                playback_path="output/../outside/private.m4b",
                playback_status=PlaybackStatus.PACKAGED,
                playback_position_sec=0.0,
                created_at=datetime(2026, 4, 1, 9, 0, 0),
                updated_at=datetime(2026, 4, 1, 9, 5, 0),
            ),
            [
                Chapter(
                    id=77,
                    book_id=77,
                    chapter_number=1,
                    title="Chapter 1",
                    duration_ms=1000,
                    mp3_path="output/../outside/private.mp3",
                )
            ],
        )
    )

    service = LibraryService(repository, output_dir=output_root, temp_dir=temp_root)

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
            Book(
                id=55,
                title="Gone Source",
                author="Tester",
                source_path=missing_source,
                source_type=SourceType.EPUB,
                conversion_mode=ConversionMode.AUDIOBOOK,
                total_chapters=1,
                playback_path="output/Gone Source/Gone Source.m4b",
                playback_status=PlaybackStatus.PACKAGED,
                playback_position_sec=0.0,
                created_at=datetime(2026, 4, 1, 9, 0, 0),
                updated_at=datetime(2026, 4, 1, 9, 5, 0),
            ),
            [
                Chapter(
                    id=55,
                    book_id=55,
                    chapter_number=1,
                    title="Chapter 1",
                    duration_ms=1000,
                    mp3_path="output/Gone Source/chapters/01.mp3",
                )
            ],
        )
    )

    service = LibraryService(repository, output_dir=output_root, temp_dir=temp_root)

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
            Book(
                id=9,
                title="Folder Book",
                author="Tester",
                source_path=str(source_path),
                source_type=SourceType.PDF,
                conversion_mode=ConversionMode.AUDIOBOOK,
                total_chapters=1,
                playback_path="output/Folder Book/Folder Book.m4b",
                playback_status=PlaybackStatus.PACKAGED,
                playback_position_sec=0.0,
                created_at=datetime(2026, 4, 1, 9, 0, 0),
                updated_at=datetime(2026, 4, 1, 9, 5, 0),
            ),
            [],
        )
    )

    service = LibraryService(
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
            Book(
                id=9,
                title="Folder Book",
                author="Tester",
                source_path=str(source_path),
                source_type=SourceType.PDF,
                conversion_mode=ConversionMode.AUDIOBOOK,
                total_chapters=1,
                playback_path=None,
                playback_status=PlaybackStatus.MISSING,
                playback_position_sec=0.0,
                created_at=datetime(2026, 4, 1, 9, 0, 0),
                updated_at=datetime(2026, 4, 1, 9, 5, 0),
            ),
            [],
        )
    )

    service = LibraryService(
        repository,
        output_dir=tmp_path / "output",
        folder_opener=lambda folder: opened_paths.append(folder),
    )

    opened_folder = service.open_book_folder(9)

    assert opened_folder == source_path.parent
    assert opened_paths == [source_path.parent]


def test_library_service_open_book_folder_creates_output_folder_when_artifacts_and_source_missing(tmp_path):
    output_root = tmp_path / "output"
    expected_output_dir = output_root / "Missing Source Book"

    opened_paths: list[Path] = []
    repository = _DeleteRepositoryStub(
        (
            Book(
                id=9,
                title="Missing Source Book",
                author="Tester",
                source_path=str(tmp_path / "imports" / "missing.epub"),
                source_type=SourceType.EPUB,
                conversion_mode=ConversionMode.AUDIOBOOK,
                total_chapters=1,
                playback_path=None,
                playback_status=PlaybackStatus.MISSING,
                playback_position_sec=0.0,
                created_at=datetime(2026, 4, 1, 9, 0, 0),
                updated_at=datetime(2026, 4, 1, 9, 5, 0),
            ),
            [],
        )
    )

    service = LibraryService(
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
            Book(
                id=9,
                title="Folder Book",
                author="Tester",
                source_path=str(managed_source_path),
                source_type=SourceType.PDF,
                conversion_mode=ConversionMode.AUDIOBOOK,
                total_chapters=1,
                playback_path=None,
                playback_status=PlaybackStatus.MISSING,
                playback_position_sec=0.0,
                created_at=datetime(2026, 4, 1, 9, 0, 0),
                updated_at=datetime(2026, 4, 1, 9, 5, 0),
            ),
            [],
        )
    )

    service = LibraryService(
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


def test_app_controller_threads_configured_output_dir_into_library_service(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    class _FakeLibraryService:
        def __init__(
            self,
            repository,
            output_dir="__missing__",
            temp_dir="__missing__",
            storage_root="__missing__",
            folder_opener=None,
        ):
            captured["repository"] = repository
            captured["output_dir"] = output_dir
            captured["temp_dir"] = temp_dir
            captured["storage_root"] = storage_root
            captured["folder_opener"] = folder_opener

    class _FakeVoiceService:
        def __init__(self, config, tts_factory=None):
            self.config = config
            self.tts_factory = tts_factory

        def validate_tts_runtime(self, *args, **kwargs):
            return None

        def cleanup(self):
            return None

    class _FakeConversionService:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def cancel_conversion(self):
            return False

    class _FakeRuntimeCapabilityService:
        def get_system_runtime_snapshot(self):
            return {"tts": {"available": False}, "cleaner": {"available": False}}

    class _FakeSharedWorkerPoolManager:
        def __init__(self, max_workers):
            self.max_workers = max_workers
            self.shutdown_called = False

        def snapshot(self):
            return {"reuse_hits": 0, "restarts": 0}

        def shutdown(self):
            self.shutdown_called = True

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

    monkeypatch.setattr(app_controller_module, "LibraryService", _FakeLibraryService)
    monkeypatch.setattr(app_controller_module, "VoiceService", _FakeVoiceService)
    monkeypatch.setattr(app_controller_module, "ConversionService", _FakeConversionService)
    monkeypatch.setattr(app_controller_module, "RuntimeCapabilityService", _FakeRuntimeCapabilityService)
    monkeypatch.setattr(app_controller_module, "SharedWorkerPoolManager", _FakeSharedWorkerPoolManager)
    monkeypatch.setattr(app_controller_module, "GlobalAPILimiter", _FakeGlobalAPILimiter)
    monkeypatch.setattr(app_controller_module, "SQLiteRepository", _FakeSQLiteRepository)

    repository = SimpleNamespace()
    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "audiobooks.db",
    )

    app_controller_module.AppController(config=config, repository=repository)

    assert captured["repository"] is repository
    assert captured["output_dir"] == config.output_dir
    assert captured["temp_dir"] == config.temp_dir
    assert captured["storage_root"] == tmp_path


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

        def cancel_conversion(self):
            return False

    class _FakeRuntimeCapabilityService:
        def get_system_runtime_snapshot(self):
            return {"tts": {"available": False}, "cleaner": {"available": False}}

    class _FakeSharedWorkerPoolManager:
        def __init__(self, max_workers):
            self.max_workers = max_workers
            self.shutdown_called = False

        def snapshot(self):
            return {"reuse_hits": 0, "restarts": 0}

        def shutdown(self):
            self.shutdown_called = True

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

    monkeypatch.setattr(app_controller_module, "LibraryService", _FakeLibraryService)
    monkeypatch.setattr(app_controller_module, "VoiceService", _FakeVoiceService)
    monkeypatch.setattr(app_controller_module, "ConversionService", _FakeConversionService)
    monkeypatch.setattr(app_controller_module, "RuntimeCapabilityService", _FakeRuntimeCapabilityService)
    monkeypatch.setattr(app_controller_module, "SharedWorkerPoolManager", _FakeSharedWorkerPoolManager)
    monkeypatch.setattr(app_controller_module, "GlobalAPILimiter", _FakeGlobalAPILimiter)
    monkeypatch.setattr(app_controller_module, "SQLiteRepository", _FakeSQLiteRepository)

    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "audiobooks.db",
    )

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

    class _FakeRuntimeCapabilityService:
        def get_system_runtime_snapshot(self):
            return {"tts": {"available": False}, "cleaner": {"available": False}}

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

    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "audiobooks.db",
    )
    controller = app_controller_module.AppController(config=config)

    controller.cleanup()

    assert calls[:3] == [
        ("cancel", 2.0),
        ("worker_shutdown", 2.0),
        ("cancel", 0.5),
    ]
    assert ("limiter_shutdown", None) in calls
    assert ("repo_close", None) in calls


def test_app_controller_desktop_shutdown_cleanup_budget_matches_cleanup_timeouts():
    controller = app_controller_module.AppController
    expected_budget = (
        controller._CLEANUP_CONVERSION_CANCEL_TIMEOUT_SECONDS
        + controller._CLEANUP_WORKER_SHUTDOWN_TIMEOUT_SECONDS
        + controller._CLEANUP_POST_WORKER_CANCEL_TIMEOUT_SECONDS
    )
    assert controller.DESKTOP_SHUTDOWN_CLEANUP_BUDGET_SECONDS >= expected_budget
    assert controller.DESKTOP_SHUTDOWN_ACK_EVENT == "shutdown"


def test_app_controller_cleanup_emits_desktop_shutdown_ack_when_desktop_ready_env_set(
    monkeypatch,
    tmp_path,
    capsys,
):
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
            return None

    class _FakeConversionService:
        def __init__(self, **kwargs):  # noqa: ANN003
            _ = kwargs

        def cancel_active_job(self, timeout_seconds=None):  # noqa: ANN001
            return False

    class _FakeRuntimeCapabilityService:
        def get_system_runtime_snapshot(self):
            return {"tts": {"available": False}, "cleaner": {"available": False}}

    class _FakeGlobalAPILimiter:
        def __init__(self, concurrency):  # noqa: ANN001
            _ = concurrency

        def snapshot(self):
            return {"wait_count": 0, "wait_ms_total": 0.0}

        def shutdown(self):
            return None

    class _FakeSQLiteRepository:
        def __init__(self, db_path, *, output_dir=None, temp_dir=None):  # noqa: ANN001
            self.db_path = db_path
            self.output_dir = output_dir
            self.temp_dir = temp_dir
            self.closed = False

        def close(self):
            self.closed = True

    monkeypatch.setattr(app_controller_module, "LibraryService", _FakeLibraryService)
    monkeypatch.setattr(app_controller_module, "VoiceService", _FakeVoiceService)
    monkeypatch.setattr(app_controller_module, "ConversionService", _FakeConversionService)
    monkeypatch.setattr(app_controller_module, "RuntimeCapabilityService", _FakeRuntimeCapabilityService)
    monkeypatch.setattr(app_controller_module, "GlobalAPILimiter", _FakeGlobalAPILimiter)
    monkeypatch.setattr(app_controller_module, "SQLiteRepository", _FakeSQLiteRepository)

    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "audiobooks.db",
    )
    controller = app_controller_module.AppController(config=config)
    controller.cleanup()

    captured = capsys.readouterr()
    assert captured.out.strip().endswith('{"event":"shutdown"}')
    assert isinstance(controller.repository, _FakeSQLiteRepository)
    assert controller.repository.closed is True


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

    class _FakeRuntimeCapabilityService:
        def get_system_runtime_snapshot(self):
            return {"tts": {"available": False}, "cleaner": {"available": False}}

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

    config = AppConfig(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
        db_path=tmp_path / "data" / "audiobooks.db",
    )
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
            Book(
                id=8,
                title="Uploaded",
                author="Tester",
                source_path=str(durable_source),
                source_type=SourceType.EPUB,
                conversion_mode=ConversionMode.AUDIOBOOK,
                total_chapters=1,
                playback_path=None,
                playback_status=PlaybackStatus.CHAPTER_AUDIO,
                playback_position_sec=0.0,
                created_at=datetime(2026, 4, 1, 9, 0, 0),
                updated_at=datetime(2026, 4, 1, 9, 5, 0),
            ),
            [
                Chapter(
                    id=12,
                    book_id=8,
                    chapter_number=1,
                    title="Chapter 1",
                    duration_ms=1000,
                    mp3_path="output/Uploaded/chapters/01.mp3",
                )
            ],
        )
    )

    service = LibraryService(
        repository,
        output_dir=output_root,
        temp_dir=tmp_path / "temp",
        storage_root=tmp_path,
    )

    deleted = service.delete_book(8)

    assert deleted is True
    assert repository.deleted_ids == [8]
    assert not durable_source.exists()
    assert not book_dir.exists()


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
                Book(
                    id=1,
                    title="First",
                    author="Tester",
                    source_path=str(shared_source),
                    source_type=SourceType.EPUB,
                    conversion_mode=ConversionMode.AUDIOBOOK,
                    total_chapters=1,
                    playback_path=None,
                    playback_status=PlaybackStatus.CHAPTER_AUDIO,
                    playback_position_sec=0.0,
                    created_at=datetime(2026, 4, 1, 9, 0, 0),
                    updated_at=datetime(2026, 4, 1, 9, 5, 0),
                ),
                [
                    Chapter(
                        id=1,
                        book_id=1,
                        chapter_number=1,
                        title="Chapter 1",
                        duration_ms=1000,
                        mp3_path="output/First/chapters/01.mp3",
                    )
                ],
            ),
            2: (
                Book(
                    id=2,
                    title="Second",
                    author="Tester",
                    source_path=str(shared_source),
                    source_type=SourceType.EPUB,
                    conversion_mode=ConversionMode.AUDIOBOOK,
                    total_chapters=1,
                    playback_path=None,
                    playback_status=PlaybackStatus.CHAPTER_AUDIO,
                    playback_position_sec=0.0,
                    created_at=datetime(2026, 4, 1, 9, 10, 0),
                    updated_at=datetime(2026, 4, 1, 9, 15, 0),
                ),
                [
                    Chapter(
                        id=2,
                        book_id=2,
                        chapter_number=1,
                        title="Chapter 1",
                        duration_ms=1000,
                        mp3_path="output/Second/chapters/01.mp3",
                    )
                ],
            ),
        }
    )

    service = LibraryService(
        repository,
        output_dir=output_root,
        temp_dir=tmp_path / "temp",
        storage_root=tmp_path,
    )

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
                Book(
                    id=2,
                    title="Second",
                    author="Tester",
                    source_path=str(shared_source),
                    source_type=SourceType.EPUB,
                    conversion_mode=ConversionMode.AUDIOBOOK,
                    total_chapters=1,
                    playback_path=None,
                    playback_status=PlaybackStatus.CHAPTER_AUDIO,
                    playback_position_sec=0.0,
                    created_at=datetime(2026, 4, 1, 9, 10, 0),
                    updated_at=datetime(2026, 4, 1, 9, 15, 0),
                ),
                [
                    Chapter(
                        id=2,
                        book_id=2,
                        chapter_number=1,
                        title="Chapter 1",
                        duration_ms=1000,
                        mp3_path="output/Second/chapters/01.mp3",
                    )
                ],
            ),
        }
    )

    service = LibraryService(
        repository,
        output_dir=output_root,
        temp_dir=tmp_path / "temp",
        storage_root=tmp_path,
    )

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
            Book(
                id=21,
                title="Owned",
                author="Tester",
                source_path=str(user_source),
                source_type=SourceType.EPUB,
                conversion_mode=ConversionMode.AUDIOBOOK,
                total_chapters=1,
                playback_path=None,
                playback_status=PlaybackStatus.CHAPTER_AUDIO,
                playback_position_sec=0.0,
                created_at=datetime(2026, 4, 1, 9, 0, 0),
                updated_at=datetime(2026, 4, 1, 9, 5, 0),
            ),
            [
                Chapter(
                    id=21,
                    book_id=21,
                    chapter_number=1,
                    title="Chapter 1",
                    duration_ms=1000,
                    mp3_path="output/Owned/chapters/01.mp3",
                )
            ],
        )
    )

    service = LibraryService(
        repository,
        output_dir=output_root,
        temp_dir=tmp_path / "temp",
        storage_root=tmp_path,
    )

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
            Book(
                id=88,
                title="Traversal",
                author="Tester",
                source_path="data/sources/../../outside/private.epub",
                source_type=SourceType.EPUB,
                conversion_mode=ConversionMode.AUDIOBOOK,
                total_chapters=1,
                playback_path=None,
                playback_status=PlaybackStatus.CHAPTER_AUDIO,
                playback_position_sec=0.0,
                created_at=datetime(2026, 4, 1, 9, 0, 0),
                updated_at=datetime(2026, 4, 1, 9, 5, 0),
            ),
            [
                Chapter(
                    id=88,
                    book_id=88,
                    chapter_number=1,
                    title="Chapter 1",
                    duration_ms=1000,
                    mp3_path="output/Traversal/chapters/01.mp3",
                )
            ],
        )
    )

    service = LibraryService(
        repository,
        output_dir=output_root,
        temp_dir=tmp_path / "temp",
        storage_root=tmp_path,
    )

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
        Book(
            id=99,
            title="Rollback",
            author="Tester",
            source_path=str(durable_source),
            source_type=SourceType.EPUB,
            conversion_mode=ConversionMode.AUDIOBOOK,
            total_chapters=1,
            playback_path=None,
            playback_status=PlaybackStatus.CHAPTER_AUDIO,
            playback_position_sec=0.0,
            created_at=datetime(2026, 4, 1, 9, 0, 0),
            updated_at=datetime(2026, 4, 1, 9, 5, 0),
        ),
        [
            Chapter(
                id=99,
                book_id=99,
                chapter_number=1,
                title="Chapter 1",
                duration_ms=1000,
                mp3_path="output/Rollback/chapters/01.mp3",
            )
        ],
    )
    repository = _FailingDeleteRepositoryStub(detail)

    service = LibraryService(
        repository,
        output_dir=output_root,
        temp_dir=tmp_path / "temp",
        storage_root=tmp_path,
    )

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
        Book(
            id=100,
            title="CommitFail",
            author="Tester",
            source_path=str(durable_source),
            source_type=SourceType.EPUB,
            conversion_mode=ConversionMode.AUDIOBOOK,
            total_chapters=1,
            playback_path=None,
            playback_status=PlaybackStatus.CHAPTER_AUDIO,
            playback_position_sec=0.0,
            created_at=datetime(2026, 4, 1, 9, 0, 0),
            updated_at=datetime(2026, 4, 1, 9, 5, 0),
        ),
        [
            Chapter(
                id=100,
                book_id=100,
                chapter_number=1,
                title="Chapter 1",
                duration_ms=1000,
                mp3_path="output/CommitFail/chapters/01.mp3",
            )
        ],
    )
    repository = _DeleteRepositoryStub(detail)
    service = LibraryService(
        repository,
        output_dir=output_root,
        temp_dir=tmp_path / "temp",
        storage_root=tmp_path,
    )

    def boom(_artifacts):  # noqa: ANN001
        raise OSError("forced commit failure")

    monkeypatch.setattr(service, "_commit_staged_artifacts", boom)

    # DB deletion already succeeded; surface success and leave staged trash for maintenance.
    assert service.delete_book(100) is True

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


def test_library_service_reports_source_unavailable_for_empty_source_path(tmp_path):
    service = LibraryService(
        _RepositoryStub(),
        output_dir=tmp_path / "output",
        temp_dir=tmp_path / "temp",
    )

    assert service._source_path_exists("") is False  # noqa: SLF001
    assert service._source_path_exists(None) is False  # noqa: SLF001
