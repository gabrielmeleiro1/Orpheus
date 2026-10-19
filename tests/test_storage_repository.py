"""Tests for SQLiteRepository storage operations."""

import os
import sys
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import modules.storage.sqlite_repo as sqlite_repo_module
from modules.storage.models import (
    BookCreate,
    ChapterCreate,
    ConversionMode,
    BookFilters,
    PlaybackStatus,
    SourceType,
)
from modules.storage.maintenance import workspace_root_for_db
from modules.storage.sqlite_repo import SQLiteRepository
from modules.app.services.library_service import LibraryService


def test_reference_queries_include_all_path_kinds_and_exclude_only_selected_book(tmp_path):
    repo = SQLiteRepository(tmp_path / "data" / "library.db")
    try:
        first, _ = repo.create_book_with_chapters(
            BookCreate(title="First", source_path="shared.md", source_type=SourceType.MD,
                       playback_path="first.m4b"),
            [ChapterCreate(book_id=0, chapter_number=1, title="First", mp3_path="first.mp3")],
        )
        second, _ = repo.create_book_with_chapters(
            BookCreate(title="Second", source_path="shared.md", source_type=SourceType.MD,
                       playback_path="output/second.m4b"),
            [ChapterCreate(book_id=0, chapter_number=1, title="Second", mp3_path="output/second.mp3"),
             ChapterCreate(book_id=0, chapter_number=2, title="Empty", mp3_path=""),
             ChapterCreate(book_id=0, chapter_number=3, title="Missing", mp3_path=None)],
        )
        assert repo.list_source_paths() == ["shared.md"]
        assert repo.list_source_paths(first.id) == ["shared.md"]
        assert set(repo.list_artifact_paths(first.id)) == {
            "shared.md", "output/second.m4b", "output/second.mp3",
        }
        repo.delete_book(second.id)
        assert repo.list_artifact_paths(first.id) == []
        assert repo.list_source_paths(first.id) == []
    finally:
        repo.close()


def test_count_books_returns_unfiltered_total_after_create_and_delete(tmp_path):
    repo = SQLiteRepository(tmp_path / "library.db")

    assert repo.count_books() == 0

    first = repo.create_book(
        BookCreate(
            title="First",
            source_path="/tmp/first.epub",
            source_type=SourceType.EPUB,
            total_chapters=1,
        )
    )
    repo.create_book(
        BookCreate(
            title="Second",
            source_path="/tmp/second.epub",
            source_type=SourceType.EPUB,
            total_chapters=1,
        )
    )
    assert repo.count_books() == 2

    assert repo.delete_book(first.id) is True
    assert repo.count_books() == 1


def test_create_books_bulk_inserts_in_bounded_batches(tmp_path):
    repo = SQLiteRepository(tmp_path / "library.db")
    books = [
        BookCreate(
            title=f"Bulk {index:04d}",
            source_path=f"/tmp/bulk-{index:04d}.pdf",
            source_type=SourceType.PDF,
            author="Bulk Author",
            total_chapters=1,
            tags="bulk",
        )
        for index in range(1200)
    ]
    inserted = repo.create_books(books, batch_size=500)
    assert inserted == 1200
    assert repo.count_books() == 1200
    summaries = repo.list_books(BookFilters(limit=1200))
    assert len(summaries) == 1200
    assert {item.title for item in summaries} == {
        f"Bulk {index:04d}" for index in range(1200)
    }


def test_create_books_rolls_back_only_the_failing_batch(tmp_path):
    repo = SQLiteRepository(tmp_path / "library.db")
    try:
        with repo._connection() as conn:
            conn.execute("""
                CREATE TRIGGER reject_book BEFORE INSERT ON books
                WHEN NEW.title = 'reject'
                BEGIN SELECT RAISE(ABORT, 'rejected book'); END
            """)
        books = [
            BookCreate(title=title, source_path=f"{title}.pdf", source_type=SourceType.PDF)
            for title in ("first", "second", "rolled back", "reject")
        ]
        with pytest.raises(sqlite3.IntegrityError, match="rejected book"):
            repo.create_books(books, batch_size=2)
        assert {book.title for book in repo.list_books()} == {"first", "second"}
    finally:
        repo.close()


def test_create_book_with_chapters_rolls_back_book_on_chapter_failure(tmp_path):
    repo = SQLiteRepository(tmp_path / "library.db")
    try:
        with repo._connection() as conn:
            conn.execute("""
                CREATE TRIGGER reject_chapter BEFORE INSERT ON chapters
                WHEN NEW.chapter_number = 2
                BEGIN SELECT RAISE(ABORT, 'rejected chapter'); END
            """)
        with pytest.raises(sqlite3.IntegrityError, match="rejected chapter"):
            repo.create_book_with_chapters(
                BookCreate(title="Rollback", source_path="book.pdf", source_type=SourceType.PDF),
                [ChapterCreate(book_id=0, chapter_number=i, title=str(i)) for i in (1, 2)],
            )
        assert repo.count_books() == 0
        with repo._connection() as conn:
            assert conn.execute("SELECT COUNT(*) FROM chapters").fetchone()[0] == 0
    finally:
        repo.close()


def test_interrupted_operation_never_leaks_writes_into_the_next_commit(tmp_path):
    class Interrupted(BaseException):
        pass

    repo = SQLiteRepository(tmp_path / "library.db")
    try:
        with pytest.raises(Interrupted):
            with repo._connection() as conn:
                conn.execute(
                    "INSERT INTO books (title, source_path, source_type) VALUES ('Partial', 'a.md', 'md')"
                )
                raise Interrupted()
        repo.create_book(BookCreate(title="Next", source_path="b.md", source_type=SourceType.MD))
        assert [book.title for book in repo.list_books()] == ["Next"]
    finally:
        repo.close()


def test_new_book_starts_unplayed_without_audio_and_counts_only_chapters_with_audio(tmp_path):
    repo = SQLiteRepository(tmp_path / "repository.db")
    try:
        book, _ = repo.create_book_with_chapters(
            BookCreate(
                title="Partial Book",
                source_path="/path/to/partial.pdf",
                source_type=SourceType.PDF,
                total_chapters=2,
            ),
            [
                ChapterCreate(book_id=0, chapter_number=1, title="Chapter 1", duration_ms=1000, mp3_path="/audio/ch1.mp3"),
                ChapterCreate(book_id=0, chapter_number=2, title="Chapter 2"),
            ],
        )
        fetched = repo.get_book(book.id)
        assert fetched.playback_position_sec == 0.0
        assert fetched.playback_path is None
        assert fetched.playback_status == PlaybackStatus.MISSING
        summary = next(item for item in repo.list_books() if item.id == book.id)
        assert (summary.total_chapters, summary.completed_chapters) == (2, 1)
    finally:
        repo.close()


def test_update_book_playback_position_round_trips(tmp_path):
    repo = SQLiteRepository(tmp_path / "repository.db")
    created = repo.create_book(
        BookCreate(
            title="Resume Test",
            source_path="/tmp/resume.pdf",
            source_type=SourceType.PDF,
            author="Tester",
            total_chapters=1,
        )
    )

    updated = repo.update_book_playback_position(created.id, 42.5)
    fetched = repo.get_book(created.id)

    assert updated is not None
    assert fetched is not None
    assert updated.playback_position_sec == 42.5
    assert fetched.playback_position_sec == 42.5

    conn = sqlite3.connect(tmp_path / "repository.db")
    try:
        raw_value = conn.execute(
            "SELECT playback_position_sec FROM books WHERE id = ?",
            (created.id,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert raw_value == 42.5


@pytest.mark.parametrize("position", [float("inf"), float("-inf"), float("nan")])
def test_update_book_playback_position_rejects_non_finite_values(tmp_path, position):
    repo = SQLiteRepository(tmp_path / "repository.db")
    created = repo.create_book(
        BookCreate(
            title="Finite Position",
            source_path="/tmp/finite.pdf",
            source_type=SourceType.PDF,
        )
    )

    with pytest.raises(ValueError, match="finite"):
        repo.update_book_playback_position(created.id, position)


def test_get_book_with_chapters_returns_playback_metadata_and_sorted_chapters(tmp_path):
    repo = SQLiteRepository(tmp_path / "repository.db")
    created, _ = repo.create_book_with_chapters(
        BookCreate(
            title="Bulk Book",
            source_path="/tmp/bulk.epub",
            source_type=SourceType.EPUB,
            author="Bulk Author",
            total_chapters=2,
            playback_path="output/Bulk Book/Bulk Book.m4b",
            playback_status=PlaybackStatus.PACKAGED,
        ),
        [
            ChapterCreate(
                book_id=0,
                chapter_number=2,
                title="Chapter 2",
                duration_ms=2000,
                mp3_path="output/Bulk Book/chapters/02.mp3",
            ),
            ChapterCreate(
                book_id=0,
                chapter_number=1,
                title="Chapter 1",
                duration_ms=1000,
                mp3_path="output/Bulk Book/chapters/01.mp3",
            ),
        ],
    )

    fetched = repo.get_book_with_chapters(created.id)

    assert fetched is not None
    book, chapters = fetched
    assert book.playback_path == "output/Bulk Book/Bulk Book.m4b"
    assert book.playback_status == PlaybackStatus.PACKAGED
    assert [chapter.chapter_number for chapter in chapters] == [1, 2]


def test_repository_init_does_not_backfill_playback_metadata(tmp_path):
    db_path = tmp_path / "data" / "repository.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    output_path = tmp_path / "output" / "Notes" / "Notes.m4a"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"m4a")

    repo = SQLiteRepository(db_path)
    created = repo.create_book(
        BookCreate(
            title="Notes",
            source_path="/tmp/notes.txt",
            source_type=SourceType.TXT,
            conversion_mode=ConversionMode.DOCUMENT_AUDIO,
            author="Tester",
            total_chapters=1,
            playback_path="output/Notes/Notes.m4a",
            playback_status=PlaybackStatus.MISSING,
        )
    )
    repo.close()

    reopened = SQLiteRepository(db_path)
    fetched = reopened.get_book(created.id)

    assert fetched is not None
    assert fetched.playback_path == "output/Notes/Notes.m4a"
    assert fetched.playback_status == PlaybackStatus.MISSING


def test_list_books_reports_packaged_only_books_as_complete(tmp_path):
    repo = SQLiteRepository(tmp_path / "repository.db")
    packaged_book, _chapters = repo.create_book_with_chapters(
        BookCreate(
            title="Packaged Only",
            source_path="/tmp/source.pdf",
            source_type=SourceType.PDF,
            author="Author",
            total_chapters=3,
            playback_path="output/Packaged Only/Packaged Only.m4b",
            playback_status=PlaybackStatus.PACKAGED,
        ),
        [
            ChapterCreate(book_id=0, chapter_number=1, title="One"),
            ChapterCreate(book_id=0, chapter_number=2, title="Two"),
            ChapterCreate(book_id=0, chapter_number=3, title="Three"),
        ],
    )

    summary = next(item for item in repo.list_books() if item.id == packaged_book.id)

    assert summary.total_chapters == 3
    assert summary.completed_chapters == 3


def test_create_book_persists_document_audio_mode_for_txt_source(tmp_path):
    repo = SQLiteRepository(tmp_path / "repository.db")

    created = repo.create_book(
        BookCreate(
            title="Notes",
            source_path="/tmp/notes.txt",
            source_type=SourceType.TXT,
            conversion_mode=ConversionMode.DOCUMENT_AUDIO,
            author="Tester",
            total_chapters=1,
        )
    )
    fetched = repo.get_book(created.id)

    assert fetched is not None
    assert fetched.source_type == SourceType.TXT
    assert fetched.conversion_mode == ConversionMode.DOCUMENT_AUDIO


def test_library_organization_fields_can_be_updated_and_filtered(tmp_path):
    repo = SQLiteRepository(tmp_path / "repository.db")
    favorite_book = repo.create_book(
        BookCreate(
            title="Original",
            source_path="/tmp/original.epub",
            source_type=SourceType.EPUB,
            conversion_mode=ConversionMode.AUDIOBOOK,
            author="Author",
            total_chapters=1,
            tags="classic",
            favorite=True,
        )
    )
    repo.create_book(
        BookCreate(
            title="Notes",
            source_path="/tmp/notes.txt",
            source_type=SourceType.TXT,
            conversion_mode=ConversionMode.DOCUMENT_AUDIO,
            author="Worker",
            total_chapters=1,
            tags="work",
            favorite=False,
        )
    )

    updated = repo.update_book(
        favorite_book.id,
        title="Updated",
        author="New Author",
        tags="classic,featured",
        favorite=False,
    )
    fetched = repo.get_book(favorite_book.id)
    document_audio = repo.list_books(BookFilters(conversion_mode=ConversionMode.DOCUMENT_AUDIO))
    not_favorites = repo.list_books(BookFilters(favorite=False))

    assert updated is not None
    assert fetched is not None
    assert fetched.title == "Updated"
    assert fetched.author == "New Author"
    assert fetched.tags == "classic,featured"
    assert fetched.favorite is False
    assert [book.title for book in document_audio] == ["Notes"]
    assert {book.title for book in not_favorites} == {"Updated", "Notes"}


def test_list_books_filters_treat_sql_fragments_as_values(tmp_path):
    repo = SQLiteRepository(tmp_path / "repository.db")
    safe_book = repo.create_book(
        BookCreate(
            title="Safe Title",
            source_path="/tmp/safe.pdf",
            source_type=SourceType.PDF,
            conversion_mode=ConversionMode.AUDIOBOOK,
            author="Safe Author",
            total_chapters=1,
            tags="safe",
            favorite=False,
        )
    )
    injected_title = "Needle%' OR 1=1 --"
    injected_author = "Author'); DROP TABLE books; --"
    injected_book = repo.create_book(
        BookCreate(
            title=injected_title,
            source_path="/tmp/injected.pdf",
            source_type=SourceType.EPUB,
            conversion_mode=ConversionMode.DOCUMENT_AUDIO,
            author=injected_author,
            total_chapters=1,
            tags="injected",
            favorite=True,
        )
    )

    title_matches = repo.list_books(BookFilters(search_query=injected_title))
    author_matches = repo.list_books(
        BookFilters(
            search_query=injected_author,
            source_type=SourceType.EPUB,
            conversion_mode=ConversionMode.DOCUMENT_AUDIO,
            favorite=True,
        )
    )
    no_matches = repo.list_books(BookFilters(search_query="x' OR 1=1 --"))

    assert [book.id for book in title_matches] == [injected_book.id]
    assert [book.id for book in author_matches] == [injected_book.id]
    assert no_matches == []
    assert repo.get_book(safe_book.id) is not None
    assert repo.get_book(injected_book.id) is not None


def test_update_book_ignores_sql_like_field_names_and_binds_values(tmp_path):
    repo = SQLiteRepository(tmp_path / "repository.db")
    original = repo.create_book(
        BookCreate(
            title="Original",
            source_path="/tmp/original.pdf",
            source_type=SourceType.PDF,
            conversion_mode=ConversionMode.AUDIOBOOK,
            author="Author",
            total_chapters=1,
            tags="safe",
            favorite=False,
        )
    )
    sentinel = repo.create_book(
        BookCreate(
            title="Sentinel",
            source_path="/tmp/sentinel.pdf",
            source_type=SourceType.PDF,
            conversion_mode=ConversionMode.AUDIOBOOK,
            author="Other",
            total_chapters=1,
            tags="untouched",
            favorite=True,
        )
    )
    injected_value = "Updated', favorite = 1 WHERE id > 0 --"

    updated = repo.update_book(
        original.id,
        title=injected_value,
        **{
            "favorite = 1 WHERE id > 0 --": True,
            "tags, favorite": "compromised",
        },
    )
    fetched_original = repo.get_book(original.id)
    fetched_sentinel = repo.get_book(sentinel.id)

    assert updated is not None
    assert fetched_original is not None
    assert fetched_sentinel is not None
    assert fetched_original.title == injected_value
    assert fetched_original.favorite is False
    assert fetched_original.tags == "safe"
    assert fetched_sentinel.title == "Sentinel"
    assert fetched_sentinel.favorite is True
    assert fetched_sentinel.tags == "untouched"


def test_apply_repair_result_rolls_back_chapters_when_later_update_fails(tmp_path):
    repo = SQLiteRepository(tmp_path / "repository.db")
    book, chapters = repo.create_book_with_chapters(
        BookCreate(
            title="Repairable",
            source_path="/tmp/source.md",
            source_type=SourceType.MD,
            conversion_mode=ConversionMode.AUDIOBOOK,
            author="Author",
            total_chapters=2,
            playback_path="/tmp/old.m4b",
            playback_status=PlaybackStatus.CHAPTER_AUDIO,
        ),
        [
            ChapterCreate(
                book_id=0,
                chapter_number=1,
                title="One",
                duration_ms=1000,
                mp3_path="/tmp/old-one.mp3",
            ),
            ChapterCreate(
                book_id=0,
                chapter_number=2,
                title="Two",
                duration_ms=2000,
                mp3_path="/tmp/old-two.mp3",
            ),
        ],
    )

    with pytest.raises(LookupError, match="Chapter not found"):
        repo.apply_repair_result(
            book.id,
            repaired_chapter_updates=[
                SimpleNamespace(
                    chapter_id=chapters[0].id,
                    duration_ms=1100,
                    mp3_path="/tmp/new-one.mp3",
                ),
                SimpleNamespace(
                    chapter_id=chapters[1].id + 999,
                    duration_ms=2200,
                    mp3_path="/tmp/new-two.mp3",
                ),
            ],
            playback_path="/tmp/new.m4b",
            playback_status=PlaybackStatus.PACKAGED,
            total_chapters=2,
        )

    fetched_book, fetched_chapters = repo.get_book_with_chapters(book.id)
    assert fetched_book.playback_path == "/tmp/old.m4b"
    assert fetched_book.playback_status == PlaybackStatus.CHAPTER_AUDIO
    assert [(chapter.duration_ms, chapter.mp3_path) for chapter in fetched_chapters] == [
        (1000, "/tmp/old-one.mp3"),
        (2000, "/tmp/old-two.mp3"),
    ]


def test_apply_repair_result_updates_chapters_and_book_in_one_call(tmp_path):
    repo = SQLiteRepository(tmp_path / "repository.db")
    book, chapters = repo.create_book_with_chapters(
        BookCreate(
            title="Repairable",
            source_path="/tmp/source.md",
            source_type=SourceType.MD,
            conversion_mode=ConversionMode.AUDIOBOOK,
            author="Author",
            total_chapters=2,
            playback_path=None,
            playback_status=PlaybackStatus.CHAPTER_AUDIO,
        ),
        [
            ChapterCreate(book_id=0, chapter_number=1, title="One"),
            ChapterCreate(book_id=0, chapter_number=2, title="Two"),
        ],
    )

    updated = repo.apply_repair_result(
        book.id,
        repaired_chapter_updates=[
            SimpleNamespace(
                chapter_id=chapters[0].id,
                duration_ms=1100,
                mp3_path="/tmp/new-one.mp3",
            ),
            SimpleNamespace(
                chapter_id=chapters[1].id,
                duration_ms=2200,
                mp3_path="/tmp/new-two.mp3",
            ),
        ],
        playback_path="/tmp/new.m4b",
        playback_status=PlaybackStatus.PACKAGED,
        total_chapters=2,
    )

    fetched_book, fetched_chapters = repo.get_book_with_chapters(book.id)
    assert updated.playback_path == "/tmp/new.m4b"
    assert updated.playback_status == PlaybackStatus.PACKAGED
    assert fetched_book.playback_path == "/tmp/new.m4b"
    assert fetched_book.playback_status == PlaybackStatus.PACKAGED
    assert [(chapter.duration_ms, chapter.mp3_path) for chapter in fetched_chapters] == [
        (1100, "/tmp/new-one.mp3"),
        (2200, "/tmp/new-two.mp3"),
    ]


def test_relative_stored_paths_use_the_same_workspace_root_across_layers(tmp_path):
    db_path = tmp_path / "state" / "audiobooks.db"
    output_dir = tmp_path / "rendered" / "books"
    temp_dir = tmp_path / "runtime" / "temp"
    repo = SQLiteRepository(db_path, output_dir=output_dir, temp_dir=temp_dir)
    service = LibraryService(
        object(),
        output_dir=output_dir,
        temp_dir=temp_dir,
        storage_root=workspace_root_for_db(db_path),
    )
    raw_path = "output/Book Title/Book Title.m4b"
    raw_upload = "temp/web_uploads/uploaded.pdf"
    expected = (output_dir / "Book Title" / "Book Title.m4b").resolve()
    expected_upload = (temp_dir / "web_uploads" / "uploaded.pdf").resolve()

    try:
        assert repo._resolve_stored_path(raw_path) == expected
        assert service._resolve_stored_path(raw_path) == expected
        assert repo._resolve_stored_path(raw_upload) == expected_upload
        assert service._resolve_stored_path(raw_upload) == expected_upload
    finally:
        repo.close()


def test_list_books_many_synthetic_rows_returns_correct_state_within_budget(tmp_path):
    row_count = 750
    runtime_budget_seconds = 2.5
    repo = SQLiteRepository(tmp_path / "repository.db")

    for index in range(row_count):
        repo.create_book(
            BookCreate(
                title=f"Synthetic Book {index:04d}",
                source_path=f"/tmp/synthetic-{index:04d}.pdf",
                source_type=SourceType.PDF,
                author="Stress Proxy",
                total_chapters=1,
                tags="synthetic",
                favorite=index % 10 == 0,
            )
        )

    started = time.perf_counter()
    summaries = repo.list_books(BookFilters(limit=row_count))
    elapsed = time.perf_counter() - started

    assert elapsed < runtime_budget_seconds
    assert len(summaries) == row_count
    titles = {item.title for item in summaries}
    assert titles == {f"Synthetic Book {index:04d}" for index in range(row_count)}
    assert sum(1 for item in summaries if item.favorite) == row_count // 10


def test_list_books_search_escapes_like_wildcards(tmp_path):
    repo = SQLiteRepository(tmp_path / "repository.db")
    percent_book = repo.create_book(
        BookCreate(
            title="100% Complete Guide",
            source_path="/tmp/percent.pdf",
            source_type=SourceType.PDF,
        )
    )
    literal_underscore = repo.create_book(
        BookCreate(
            title="AB_CD",
            source_path="/tmp/underscore.pdf",
            source_type=SourceType.PDF,
        )
    )
    repo.create_book(
        BookCreate(
            title="ABXCD",
            source_path="/tmp/lookalike.pdf",
            source_type=SourceType.PDF,
        )
    )

    # "%" must match only titles containing a literal percent sign.
    percent_matches = repo.list_books(BookFilters(search_query="%"))
    assert [book.id for book in percent_matches] == [percent_book.id]

    # An embedded "%" must not act as a multi-character wildcard.
    assert repo.list_books(BookFilters(search_query="100%Guide")) == []

    # "_" must not act as a single-character wildcard.
    underscore_matches = repo.list_books(BookFilters(search_query="AB_CD"))
    assert [book.id for book in underscore_matches] == [literal_underscore.id]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not enforced on Windows")
def test_sqlite_database_parent_and_file_are_owner_only(tmp_path):
    import stat

    db_path = tmp_path / "data" / "app.db"
    repo = SQLiteRepository(db_path)
    repo.close()
    assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


def test_repository_quarantines_corrupt_database_and_recreates_fresh(tmp_path):
    """A corrupt/non-SQLite database file must not turn every request into an
    opaque 500: the repository quarantines the unreadable file aside and recreates
    an empty, usable library (defect: corrupt DB availability)."""
    db_path = tmp_path / "data" / "audiobooks.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Seed a valid DB with content, then clobber it into garbage plus a leftover
    # WAL sidecar to exercise sidecar quarantining.
    SQLiteRepository(db_path).close()
    db_path.write_bytes(b"SQLite format 3\x00" + b"\xde\xad" * 500)
    wal_sidecar = db_path.with_name(db_path.name + "-wal")
    wal_sidecar.write_bytes(b"stale wal pages")

    repo = SQLiteRepository(db_path)
    try:
        assert repo.count_books() == 0  # fresh, usable library
        created = repo.create_book(
            BookCreate(
                title="After Recovery",
                source_path="/tmp/x.epub",
                source_type=SourceType.EPUB,
                total_chapters=1,
            )
        )
        assert created.id is not None
    finally:
        repo.close()

    quarantined = list(db_path.parent.glob("audiobooks.db.corrupt-*"))
    assert quarantined, "corrupt database was not quarantined aside"
    assert not wal_sidecar.exists()  # sidecar moved with the main file


def test_repository_quarantines_corrupt_database_when_file_sync_requires_write_access(
    tmp_path, monkeypatch
):
    import errno
    import stat

    fcntl = pytest.importorskip("fcntl")
    original_fsync = os.fsync
    synced_files = []

    def sync_writable_file(descriptor):
        file_stat = os.fstat(descriptor)
        if stat.S_ISREG(file_stat.st_mode):
            access_mode = fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE
            if access_mode == os.O_RDONLY:
                raise OSError(errno.EBADF, "File sync requires write access")
            synced_files.append(file_stat.st_ino)
        original_fsync(descriptor)

    db_path = tmp_path / "audiobooks.db"
    corrupt_contents = b"not a sqlite database"
    db_path.write_bytes(corrupt_contents)
    original_inode = db_path.stat().st_ino
    monkeypatch.setattr(sqlite_repo_module.os, "fsync", sync_writable_file)

    repo = SQLiteRepository(db_path)
    try:
        assert repo.count_books() == 0
        assert repo.quarantine is not None
    finally:
        repo.close()

    quarantined = list(tmp_path.glob("audiobooks.db.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == corrupt_contents
    assert original_inode in synced_files


def test_repository_does_not_quarantine_healthy_database_on_transient_lock(
    tmp_path, monkeypatch
):
    """A transient SQLite operational failure is not proof of corruption.

    The open must fail with the original error while leaving the healthy database
    and its user data in place.
    """
    db_path = tmp_path / "data" / "audiobooks.db"
    repo = SQLiteRepository(db_path)
    book = repo.create_book(
        BookCreate(
            title="Keep Me",
            source_path="/tmp/keep-me.epub",
            source_type=SourceType.EPUB,
            total_chapters=1,
        )
    )
    repo.close()

    real_run_migrations = sqlite_repo_module.run_migrations
    calls = 0

    def fail_once_with_lock(conn):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_run_migrations(conn)

    monkeypatch.setattr(
        sqlite_repo_module, "run_migrations", fail_once_with_lock
    )

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        SQLiteRepository(db_path)

    assert not list(db_path.parent.glob("audiobooks.db.corrupt-*"))
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT title FROM books WHERE id = ?", (book.id,)
        ).fetchone()
    finally:
        conn.close()
    assert row == ("Keep Me",)


def test_repository_never_overwrites_existing_quarantine(tmp_path, monkeypatch):
    """Two recovery attempts in one timestamp bucket must preserve both files."""
    db_path = tmp_path / "data" / "audiobooks.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(b"not a sqlite database")

    class FrozenDateTime:
        @classmethod
        def now(cls):
            return cls()

        def strftime(self, _format):
            return "20260723_120000"

    monkeypatch.setattr(sqlite_repo_module, "datetime", FrozenDateTime)
    existing = db_path.with_name(
        "audiobooks.db.corrupt-20260723_120000"
    )
    existing.write_bytes(b"older quarantine")

    repo = SQLiteRepository(db_path)
    repo.close()

    assert existing.read_bytes() == b"older quarantine"
    quarantines = sorted(db_path.parent.glob("audiobooks.db.corrupt-*"))
    assert len(quarantines) == 2
    assert any(path.read_bytes() == b"not a sqlite database" for path in quarantines)


def test_repository_never_deletes_sidecar_when_quarantine_move_fails(
    tmp_path, monkeypatch
):
    """Recovery must fail safely and restore the main DB if a WAL move fails."""
    db_path = tmp_path / "data" / "audiobooks.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    original_database = b"not a sqlite database"
    db_path.write_bytes(original_database)
    wal_path = db_path.with_name(db_path.name + "-wal")

    real_replace = Path.replace

    def fail_wal_replace(path, target):
        if path == db_path:
            result = real_replace(path, target)
            # SQLite may discard an invalid pre-existing WAL while opening the
            # corrupt main file. Materialize it at the recovery boundary so this
            # test exercises our sidecar move failure deterministically.
            wal_path.write_bytes(b"user recovery data")
            return result
        if path == wal_path:
            raise OSError("simulated sidecar move failure")
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_wal_replace)

    with pytest.raises(OSError, match="simulated sidecar move failure"):
        SQLiteRepository(db_path)

    assert db_path.read_bytes() == original_database
    assert wal_path.read_bytes() == b"user recovery data"
    assert not list(db_path.parent.glob("audiobooks.db.corrupt-*"))


def test_repository_rolls_back_already_moved_wal_when_shm_quarantine_fails(
    tmp_path, monkeypatch
):
    """A later sidecar failure must not leave earlier moves split across paths."""
    db_path = tmp_path / "data" / "audiobooks.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    original_database = b"not a sqlite database"
    original_wal = b"user wal recovery data"
    original_shm = b"user shm recovery data"
    db_path.write_bytes(original_database)
    wal_path = db_path.with_name(db_path.name + "-wal")
    shm_path = db_path.with_name(db_path.name + "-shm")

    real_replace = Path.replace

    def fail_shm_replace(path, target):
        if path == db_path:
            result = real_replace(path, target)
            # SQLite may discard invalid sidecars while opening the corrupt main
            # file, so materialize both at the recovery boundary.
            wal_path.write_bytes(original_wal)
            shm_path.write_bytes(original_shm)
            return result
        if path == shm_path:
            raise OSError("simulated shm move failure")
        return real_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_shm_replace)

    with pytest.raises(OSError, match="simulated shm move failure"):
        SQLiteRepository(db_path)

    assert db_path.read_bytes() == original_database
    assert wal_path.read_bytes() == original_wal
    assert shm_path.read_bytes() == original_shm
    assert not list(db_path.parent.glob("audiobooks.db.corrupt-*"))


@pytest.mark.parametrize("query", ["Café", "Cafe\u0301", "CAFÉ", "été", "ÉTÉ", "STRASSE", "100%_\\"])
def test_search_matches_unicode_equivalents_and_literal_patterns(tmp_path, query):
    repo = SQLiteRepository(tmp_path / "data" / "library.db")
    try:
        book = repo.create_book(BookCreate(
            title="Cafe\u0301 ÉTÉ 100%_\\", author="Straße",
            source_path="source.txt", source_type=SourceType.TXT,
        ))
        repo.create_book(BookCreate(
            title="100 percent", source_path="other.txt", source_type=SourceType.TXT,
        ))
        matches = repo.list_books(BookFilters(search_query=query))
        assert [item.id for item in matches] == [book.id]
        assert matches[0].title == "Cafe\u0301 ÉTÉ 100%_\\"
        assert matches[0].author == "Straße"
    finally:
        repo.close()

