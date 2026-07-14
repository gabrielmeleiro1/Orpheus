"""
Test storage maintenance audit/prune helpers.
"""

import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import modules.storage.maintenance as maintenance_module
from modules.storage.models import BookCreate, ChapterCreate, SourceType
from modules.storage.models import PlaybackStatus
from modules.storage.sqlite_repo import SQLiteRepository
from modules.storage.maintenance import (
    audit_storage_state,
    backup_database,
    prune_stale_records_and_files,
)


def test_storage_maintenance() -> bool:
    print("\n" + "=" * 50)
    print("STORAGE MAINTENANCE TEST SUITE")
    print("=" * 50 + "\n")

    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            output_dir = root / "output"
            temp_dir = root / "temp"
            web_upload_dir = temp_dir / "web_uploads"

            data_dir.mkdir(parents=True, exist_ok=True)
            output_dir.mkdir(parents=True, exist_ok=True)
            web_upload_dir.mkdir(parents=True, exist_ok=True)

            db_path = data_dir / "audiobooks.db"
            repo = SQLiteRepository(db_path)

            # Keep-book artifacts.
            keep_chapter = output_dir / "Keep Book" / "chapters" / "01_keep.mp3"
            keep_chapter.parent.mkdir(parents=True, exist_ok=True)
            keep_chapter.write_bytes(b"ID3keep")

            keep_source = web_upload_dir / "keep_source.pdf"
            keep_source.write_bytes(b"%PDF-1.4 keep")

            keep_book, _ = repo.create_book_with_chapters(
                BookCreate(
                    title="Keep Book",
                    author="Author",
                    source_path=str(keep_source),
                    source_type=SourceType.PDF,
                    total_chapters=1,
                ),
                [
                    ChapterCreate(
                        book_id=0,
                        chapter_number=1,
                        title="Keep Chapter",
                        duration_ms=1000,
                        mp3_path=str(keep_chapter),
                    )
                ],
            )

            # Stale-book artifacts.
            stale_book, _ = repo.create_book_with_chapters(
                BookCreate(
                    title="Stale Book",
                    author="Author",
                    source_path=str(root / "missing_source.pdf"),
                    source_type=SourceType.PDF,
                    total_chapters=1,
                ),
                [
                    ChapterCreate(
                        book_id=0,
                        chapter_number=1,
                        title="Stale Chapter",
                        duration_ms=1000,
                        mp3_path=str(output_dir / "missing" / "01_missing.mp3"),
                    )
                ],
            )

            # Orphan output + temp artifacts.
            orphan_output = output_dir / "Orphan Book"
            orphan_output.mkdir(parents=True, exist_ok=True)
            (orphan_output / "orphan.txt").write_text("orphan", encoding="utf-8")

            stale_upload = web_upload_dir / "stale_upload.epub"
            stale_upload.write_bytes(b"EPUB stale")

            stale_stage_dir = temp_dir / "ch_999"
            stale_stage_dir.mkdir(parents=True, exist_ok=True)
            (stale_stage_dir / "chunk_0000.wav").write_bytes(b"RIFF")

            audit_before = audit_storage_state(
                db_path=db_path,
                output_dir=output_dir,
                temp_dir=temp_dir,
            )
            assert len(audit_before["stale_chapter_rows"]) >= 1
            assert any("Orphan Book" in p for p in audit_before["orphan_output_dirs"])

            report = prune_stale_records_and_files(
                db_path=db_path,
                output_dir=output_dir,
                temp_dir=temp_dir,
                aggressive=True,
            )

            assert Path(report["backup_path"]).exists()
            assert Path(report["report_path"]).exists()

            conn = sqlite3.connect(db_path)
            try:
                remaining_books = {
                    row[0]: row[1]
                    for row in conn.execute("SELECT id, title FROM books").fetchall()
                }
                assert keep_book.id in remaining_books
                assert stale_book.id not in remaining_books

                updated_source = conn.execute(
                    "SELECT source_path FROM books WHERE id = ?",
                    (keep_book.id,),
                ).fetchone()[0]
                assert str(data_dir / "sources") in updated_source
                assert Path(updated_source).exists()
            finally:
                conn.close()

            assert not orphan_output.exists()
            assert not stale_upload.exists()
            assert not stale_stage_dir.exists()
            assert keep_chapter.exists()

            print("✓ Audit detected stale rows and orphan artifacts")
            print("✓ Aggressive prune deleted stale rows/files")
            print("✓ Referenced upload source moved to data/sources")
    except Exception as exc:
        print(f"✗ Storage maintenance test failed: {exc}")
        return False

    print("\n" + "=" * 50)
    print("ALL TESTS PASSED ✓")
    print("=" * 50 + "\n")
    return True


def test_storage_maintenance_respects_custom_output_root(tmp_path):
    workspace = tmp_path / "workspace"
    db_path = workspace / "state" / "audiobooks.db"
    output_dir = workspace / "rendered" / "books"
    temp_dir = workspace / "runtime" / "temp"
    web_upload_dir = temp_dir / "web_uploads"

    output_dir.mkdir(parents=True, exist_ok=True)
    web_upload_dir.mkdir(parents=True, exist_ok=True)

    keep_chapter = output_dir / "Keep Book" / "chapters" / "01_keep.mp3"
    keep_chapter.parent.mkdir(parents=True, exist_ok=True)
    keep_chapter.write_bytes(b"ID3keep")

    keep_source = web_upload_dir / "keep_source.pdf"
    keep_source.write_bytes(b"%PDF-1.4 keep")

    repo = SQLiteRepository(db_path, output_dir=output_dir, temp_dir=temp_dir)
    try:
        repo.create_book_with_chapters(
            BookCreate(
                title="Keep Book",
                author="Author",
                source_path="temp/web_uploads/keep_source.pdf",
                source_type=SourceType.PDF,
                total_chapters=1,
            ),
            [
                ChapterCreate(
                    book_id=0,
                    chapter_number=1,
                    title="Keep Chapter",
                    duration_ms=1000,
                    mp3_path="output/Keep Book/chapters/01_keep.mp3",
                )
            ],
        )

        audit = audit_storage_state(db_path=db_path, output_dir=output_dir, temp_dir=temp_dir)
        assert str((output_dir / "Keep Book").resolve()) not in audit["orphan_output_dirs"]

        report = prune_stale_records_and_files(
            db_path=db_path,
            output_dir=output_dir,
            temp_dir=temp_dir,
            aggressive=True,
        )

        assert Path(report["report_path"]).parent == (output_dir / "reports").resolve()
        assert keep_chapter.exists()
    finally:
        repo.close()


def test_backup_database_includes_committed_wal_pages_and_uses_unique_names(tmp_path):
    db_path = tmp_path / "data" / "audiobooks.db"
    repo = SQLiteRepository(db_path)
    created = repo.create_book(
        BookCreate(
            title="WAL Backed",
            author="Author",
            source_path="/tmp/source.pdf",
            source_type=SourceType.PDF,
            total_chapters=1,
        )
    )

    first_backup = backup_database(db_path)
    second_backup = backup_database(db_path)
    repo.close()

    assert first_backup != second_backup
    for backup_path in (first_backup, second_backup):
        conn = sqlite3.connect(backup_path)
        try:
            title = conn.execute("SELECT title FROM books WHERE id = ?", (created.id,)).fetchone()[0]
        finally:
            conn.close()
        assert title == "WAL Backed"


def test_prune_preserves_packaged_only_books_and_output_dirs(tmp_path):
    root = tmp_path
    db_path = root / "data" / "audiobooks.db"
    output_dir = root / "output"
    temp_dir = root / "temp"
    source = root / "source.pdf"
    packaged = output_dir / "Packaged Only" / "Packaged Only.m4b"
    source.parent.mkdir(parents=True, exist_ok=True)
    packaged.parent.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"%PDF source")
    packaged.write_bytes(b"m4b")

    repo = SQLiteRepository(db_path, output_dir=output_dir, temp_dir=temp_dir)
    try:
        book, _chapters = repo.create_book_with_chapters(
            BookCreate(
                title="Packaged Only",
                author="Author",
                source_path=str(source),
                source_type=SourceType.PDF,
                total_chapters=2,
                playback_path=str(packaged),
                playback_status=PlaybackStatus.PACKAGED,
            ),
            [
                ChapterCreate(book_id=0, chapter_number=1, title="One"),
                ChapterCreate(book_id=0, chapter_number=2, title="Two"),
            ],
        )
    finally:
        repo.close()

    audit = audit_storage_state(db_path, output_dir=output_dir, temp_dir=temp_dir)
    assert audit["stale_chapter_rows"] == []
    assert str(packaged.parent.resolve()) not in audit["orphan_output_dirs"]

    report = prune_stale_records_and_files(
        db_path,
        output_dir=output_dir,
        temp_dir=temp_dir,
        aggressive=True,
    )

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT title FROM books WHERE id = ?", (book.id,)).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert packaged.exists()
    assert book.id not in report["actions"]["deleted_book_rows"]


def test_prune_preserves_external_user_source_files(tmp_path):
    root = tmp_path
    db_path = root / "data" / "audiobooks.db"
    output_dir = root / "output"
    temp_dir = root / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    # User source kept in a personal library folder, not a managed upload.
    external_source = root / "my-library" / "novel.epub"
    external_source.parent.mkdir(parents=True, exist_ok=True)
    external_source.write_bytes(b"%EPUB user owned")

    packaged = output_dir / "Owned Book" / "Owned Book.m4b"
    packaged.parent.mkdir(parents=True, exist_ok=True)
    packaged.write_bytes(b"m4b")

    repo = SQLiteRepository(db_path, output_dir=output_dir, temp_dir=temp_dir)
    try:
        repo.create_book_with_chapters(
            BookCreate(
                title="Owned Book",
                author="Author",
                source_path=str(external_source),
                source_type=SourceType.EPUB,
                total_chapters=1,
                playback_path=str(packaged),
                playback_status=PlaybackStatus.PACKAGED,
            ),
            [ChapterCreate(book_id=0, chapter_number=1, title="One")],
        )
    finally:
        repo.close()

    report = prune_stale_records_and_files(
        db_path,
        output_dir=output_dir,
        temp_dir=temp_dir,
        aggressive=True,
    )

    assert external_source.exists()
    assert external_source.read_bytes() == b"%EPUB user owned"
    assert report["actions"]["moved_sources"] == []
    assert not (db_path.parent / "sources").exists() or not list(
        (db_path.parent / "sources").glob("novel*.epub")
    )


def test_prune_is_safe_against_path_traversal_in_stored_paths(tmp_path):
    root = tmp_path
    db_path = root / "data" / "audiobooks.db"
    output_dir = root / "output"
    temp_dir = root / "temp"
    (temp_dir / "web_uploads").mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    outside = root / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    protected_source = outside / "secret_source.epub"
    protected_artifact = outside / "secret.m4b"
    protected_source.write_bytes(b"user secret")
    protected_artifact.write_bytes(b"user audio")

    repo = SQLiteRepository(db_path, output_dir=output_dir, temp_dir=temp_dir)
    try:
        repo.create_book_with_chapters(
            BookCreate(
                title="Traversal Book",
                author="Author",
                source_path="temp/web_uploads/../../outside/secret_source.epub",
                source_type=SourceType.EPUB,
                total_chapters=1,
                playback_path="output/../outside/secret.m4b",
                playback_status=PlaybackStatus.PACKAGED,
            ),
            [
                ChapterCreate(
                    book_id=0,
                    chapter_number=1,
                    title="One",
                    mp3_path="output/../outside/secret.m4b",
                )
            ],
        )
    finally:
        repo.close()

    prune_stale_records_and_files(
        db_path,
        output_dir=output_dir,
        temp_dir=temp_dir,
        aggressive=True,
    )

    assert protected_source.exists()
    assert protected_source.read_bytes() == b"user secret"
    assert protected_artifact.exists()


def test_prune_does_not_move_absolute_path_with_web_uploads_name(tmp_path):
    """Only files inside the managed upload root may be promoted to durable storage."""
    workspace = tmp_path / "workspace"
    db_path = workspace / "data" / "audiobooks.db"
    output_dir = workspace / "output"
    temp_dir = workspace / "temp"
    (output_dir).mkdir(parents=True, exist_ok=True)
    (temp_dir / "web_uploads").mkdir(parents=True, exist_ok=True)

    lookalike = tmp_path / "user" / "Documents" / "web_uploads" / "novel.epub"
    lookalike.parent.mkdir(parents=True, exist_ok=True)
    lookalike.write_bytes(b"user-owned source")
    chapter_path = output_dir / "User Source" / "chapters" / "01_source.mp3"
    chapter_path.parent.mkdir(parents=True, exist_ok=True)
    chapter_path.write_bytes(b"audio")

    repo = SQLiteRepository(db_path, output_dir=output_dir, temp_dir=temp_dir)
    try:
        repo.create_book_with_chapters(
            BookCreate(
                title="User Source",
                author="Author",
                source_path=str(lookalike),
                source_type=SourceType.EPUB,
                total_chapters=1,
            ),
            [ChapterCreate(book_id=0, chapter_number=1, title="One", mp3_path=str(chapter_path))],
        )
    finally:
        repo.close()

    report = prune_stale_records_and_files(
        db_path,
        output_dir=output_dir,
        temp_dir=temp_dir,
        aggressive=True,
    )

    assert report["actions"]["moved_sources"] == []
    assert lookalike.exists()
    assert not (db_path.parent / "sources" / lookalike.name).exists()


def test_prune_is_idempotent_and_preserves_live_artifacts_on_repeated_runs(tmp_path):
    root = tmp_path
    db_path = root / "data" / "audiobooks.db"
    output_dir = root / "output"
    temp_dir = root / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    source = root / "library" / "source.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"%PDF source")
    packaged = output_dir / "Live Book" / "Live Book.m4b"
    packaged.parent.mkdir(parents=True, exist_ok=True)
    packaged.write_bytes(b"m4b")

    repo = SQLiteRepository(db_path, output_dir=output_dir, temp_dir=temp_dir)
    try:
        book, _chapters = repo.create_book_with_chapters(
            BookCreate(
                title="Live Book",
                author="Author",
                source_path=str(source),
                source_type=SourceType.PDF,
                total_chapters=1,
                playback_path=str(packaged),
                playback_status=PlaybackStatus.PACKAGED,
            ),
            [ChapterCreate(book_id=0, chapter_number=1, title="One")],
        )
    finally:
        repo.close()

    first = prune_stale_records_and_files(
        db_path, output_dir=output_dir, temp_dir=temp_dir, aggressive=True
    )
    second = prune_stale_records_and_files(
        db_path, output_dir=output_dir, temp_dir=temp_dir, aggressive=True
    )

    assert book.id not in first["actions"]["deleted_book_rows"]
    assert second["actions"]["deleted_book_rows"] == []
    assert second["actions"]["deleted_output_dirs"] == []
    assert packaged.exists()
    assert source.exists()

    conn = sqlite3.connect(db_path)
    try:
        remaining = conn.execute(
            "SELECT title FROM books WHERE id = ?", (book.id,)
        ).fetchone()
    finally:
        conn.close()
    assert remaining is not None


def test_prune_does_not_move_upload_sources_until_db_prune_commits(tmp_path, monkeypatch):
    root = tmp_path
    db_path = root / "data" / "audiobooks.db"
    output_dir = root / "output"
    temp_dir = root / "temp"
    upload_dir = temp_dir / "web_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    upload_source = upload_dir / "rollback_source.pdf"
    upload_source.write_bytes(b"%PDF source")

    repo = SQLiteRepository(db_path, output_dir=output_dir, temp_dir=temp_dir)
    try:
        repo.create_book_with_chapters(
            BookCreate(
                title="Rollback Book",
                author="Author",
                source_path=str(upload_source),
                source_type=SourceType.PDF,
                total_chapters=1,
            ),
            [
                ChapterCreate(
                    book_id=0,
                    chapter_number=1,
                    title="Missing",
                    duration_ms=1000,
                    mp3_path=str(output_dir / "missing" / "chapter.mp3"),
                )
            ],
        )
    finally:
        repo.close()

    real_connect = maintenance_module.sqlite3.connect

    class FailingDeleteConnection:
        def __init__(self, connection):
            self._connection = connection

        @property
        def row_factory(self):
            return self._connection.row_factory

        @row_factory.setter
        def row_factory(self, value):
            self._connection.row_factory = value

        def execute(self, sql, parameters=()):
            if sql.lstrip().upper().startswith("DELETE FROM BOOKS"):
                raise sqlite3.OperationalError("forced delete failure")
            return self._connection.execute(sql, parameters)

        def backup(self, target):
            target_connection = getattr(target, "_connection", target)
            return self._connection.backup(target_connection)

        def commit(self):
            return self._connection.commit()

        def rollback(self):
            return self._connection.rollback()

        def close(self):
            return self._connection.close()

    def connect_with_delete_failure(path, *args, **kwargs):
        connection = real_connect(path, *args, **kwargs)
        if Path(path) == db_path:
            return FailingDeleteConnection(connection)
        return connection

    monkeypatch.setattr(maintenance_module.sqlite3, "connect", connect_with_delete_failure)

    with pytest.raises(sqlite3.OperationalError, match="forced delete failure"):
        prune_stale_records_and_files(
            db_path,
            output_dir=output_dir,
            temp_dir=temp_dir,
            aggressive=True,
        )

    assert upload_source.exists()
    assert not list((db_path.parent / "sources").glob("rollback_source*.pdf"))


def test_prune_verifies_directory_deletion_before_recording_action(tmp_path, monkeypatch):
    root = tmp_path
    db_path = root / "data" / "audiobooks.db"
    output_dir = root / "output"
    temp_dir = root / "temp"
    orphan_output = output_dir / "Orphan Book"

    orphan_output.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    (orphan_output / "artifact.txt").write_text("orphan", encoding="utf-8")

    repo = SQLiteRepository(db_path, output_dir=output_dir, temp_dir=temp_dir)
    repo.close()

    monkeypatch.setattr(maintenance_module, "rmtree", lambda _path, **_kwargs: None)

    with pytest.raises(OSError, match="Failed to delete directory"):
        prune_stale_records_and_files(
            db_path,
            output_dir=output_dir,
            temp_dir=temp_dir,
            aggressive=True,
            grace_seconds=0.0,
        )

    assert orphan_output.exists()


def test_audit_detects_orphan_durable_app_owned_sources(tmp_path):
    root = tmp_path
    db_path = root / "data" / "audiobooks.db"
    output_dir = root / "output"
    temp_dir = root / "temp"
    sources_dir = db_path.parent / "sources"
    sources_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    temp_dir.mkdir(parents=True)

    referenced_source = sources_dir / "referenced.epub"
    referenced_source.write_bytes(b"%EPUB referenced")
    orphan_source = sources_dir / "orphan.epub"
    orphan_source.write_bytes(b"%EPUB orphan")

    repo = SQLiteRepository(db_path, output_dir=output_dir, temp_dir=temp_dir)
    try:
        repo.create_book_with_chapters(
            BookCreate(
                title="Referenced Book",
                author="Author",
                source_path=str(referenced_source),
                source_type=SourceType.EPUB,
                total_chapters=1,
            ),
            [ChapterCreate(book_id=0, chapter_number=1, title="One")],
        )
    finally:
        repo.close()

    audit = audit_storage_state(
        db_path=db_path, output_dir=output_dir, temp_dir=temp_dir, grace_seconds=0.0
    )

    assert str(referenced_source.resolve()) in audit["referenced_durable_sources"]
    assert str(orphan_source.resolve()) in audit["orphan_durable_sources"]
    assert referenced_source.exists()
    assert orphan_source.exists()


def test_prune_deletes_orphan_durable_app_owned_sources(tmp_path):
    root = tmp_path
    db_path = root / "data" / "audiobooks.db"
    output_dir = root / "output"
    temp_dir = root / "temp"
    sources_dir = db_path.parent / "sources"
    sources_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    temp_dir.mkdir(parents=True)

    referenced_source = sources_dir / "referenced.epub"
    referenced_source.write_bytes(b"%EPUB referenced")
    orphan_source = sources_dir / "orphan.epub"
    orphan_source.write_bytes(b"%EPUB orphan")
    keep_chapter = output_dir / "Referenced Book" / "chapters" / "01_keep.mp3"
    keep_chapter.parent.mkdir(parents=True)
    keep_chapter.write_bytes(b"ID3keep")

    repo = SQLiteRepository(db_path, output_dir=output_dir, temp_dir=temp_dir)
    try:
        repo.create_book_with_chapters(
            BookCreate(
                title="Referenced Book",
                author="Author",
                source_path=str(referenced_source),
                source_type=SourceType.EPUB,
                total_chapters=1,
            ),
            [
                ChapterCreate(
                    book_id=0,
                    chapter_number=1,
                    title="One",
                    duration_ms=1000,
                    mp3_path=str(keep_chapter),
                )
            ],
        )
    finally:
        repo.close()

    report = prune_stale_records_and_files(
        db_path=db_path,
        output_dir=output_dir,
        temp_dir=temp_dir,
        aggressive=True,
        grace_seconds=0.0,
    )

    assert referenced_source.exists()
    assert not orphan_source.exists()
    assert str(orphan_source.resolve()) in report["actions"]["deleted_durable_sources"]


def test_audit_does_not_flag_user_owned_source_as_orphan_durable(tmp_path):
    root = tmp_path
    db_path = root / "data" / "audiobooks.db"
    output_dir = root / "output"
    temp_dir = root / "temp"
    output_dir.mkdir(parents=True)
    temp_dir.mkdir(parents=True)

    user_source = root / "my-library" / "owned.epub"
    user_source.parent.mkdir(parents=True)
    user_source.write_bytes(b"%EPUB user owned")

    repo = SQLiteRepository(db_path, output_dir=output_dir, temp_dir=temp_dir)
    try:
        repo.create_book_with_chapters(
            BookCreate(
                title="Owned Book",
                author="Author",
                source_path=str(user_source),
                source_type=SourceType.EPUB,
                total_chapters=1,
            ),
            [ChapterCreate(book_id=0, chapter_number=1, title="One")],
        )
    finally:
        repo.close()

    audit = audit_storage_state(db_path=db_path, output_dir=output_dir, temp_dir=temp_dir)

    assert audit["orphan_durable_sources"] == []
    assert str(user_source.resolve()) not in audit["referenced_durable_sources"]


def _backdate_tree(path: Path, mtime: float) -> None:
    os.utime(path, (mtime, mtime))
    if path.is_dir():
        for root, dirs, files in os.walk(path):
            for name in dirs + files:
                os.utime(Path(root) / name, (mtime, mtime))


def test_recent_unreferenced_artifacts_survive_default_grace(tmp_path):
    """Queued uploads and in-flight artifacts have no DB reference yet; the
    default grace window must keep audit/prune away from them until they age."""
    root = tmp_path
    db_path = root / "data" / "audiobooks.db"
    output_dir = root / "output"
    temp_dir = root / "temp"
    output_dir.mkdir(parents=True)
    temp_dir.mkdir(parents=True)

    queued_upload = temp_dir / "web_uploads" / "queued.epub"
    queued_upload.parent.mkdir(parents=True)
    queued_upload.write_bytes(b"%EPUB queued for conversion")

    inflight_output = output_dir / "In Flight Book" / "chapters" / "01_partial.wav"
    inflight_output.parent.mkdir(parents=True)
    inflight_output.write_bytes(b"RIFFpartial")

    stage_chunk = temp_dir / "ch_0001" / "chunk_000.wav"
    stage_chunk.parent.mkdir(parents=True)
    stage_chunk.write_bytes(b"RIFFchunk")

    copied_source = db_path.parent / "sources" / "queued_copy.epub"
    copied_source.parent.mkdir(parents=True)
    copied_source.write_bytes(b"%EPUB durable copy")

    repo = SQLiteRepository(db_path, output_dir=output_dir, temp_dir=temp_dir)
    repo.close()

    audit = audit_storage_state(db_path=db_path, output_dir=output_dir, temp_dir=temp_dir)
    assert audit["stale_temp_uploads"] == []
    assert audit["orphan_output_dirs"] == []
    assert audit["stale_temp_stage_dirs"] == []
    assert audit["orphan_durable_sources"] == []

    prune_stale_records_and_files(
        db_path=db_path, output_dir=output_dir, temp_dir=temp_dir, aggressive=True
    )
    assert queued_upload.exists()
    assert inflight_output.exists()
    assert stage_chunk.exists()
    assert copied_source.exists()

    expired = time.time() - (maintenance_module.DEFAULT_CLEANUP_GRACE_SECONDS + 60)
    _backdate_tree(queued_upload, expired)
    _backdate_tree(inflight_output.parent.parent, expired)
    _backdate_tree(stage_chunk.parent, expired)
    _backdate_tree(copied_source, expired)

    report = prune_stale_records_and_files(
        db_path=db_path, output_dir=output_dir, temp_dir=temp_dir, aggressive=True
    )
    assert not queued_upload.exists()
    assert not inflight_output.parent.parent.exists()
    assert not stage_chunk.parent.exists()
    assert not copied_source.exists()
    assert [Path(p).name for p in report["actions"]["deleted_temp_uploads"]] == ["queued.epub"]


def test_audit_and_prune_recover_orphan_delete_staging(tmp_path):
    root = tmp_path
    db_path = root / "data" / "audiobooks.db"
    output_dir = root / "output"
    temp_dir = root / "temp"
    output_dir.mkdir(parents=True)
    temp_dir.mkdir(parents=True)

    staging_dir = temp_dir / "delete_staging" / "book_42"
    staged_file = staging_dir / "chapters" / "01.mp3"
    staged_file.parent.mkdir(parents=True)
    staged_file.write_bytes(b"ID3trash")

    repo = SQLiteRepository(db_path, output_dir=output_dir, temp_dir=temp_dir)
    try:
        repo.create_book_with_chapters(
            BookCreate(
                title="Alive Book",
                author="Author",
                source_path=str(root / "alive.epub"),
                source_type=SourceType.EPUB,
                total_chapters=1,
            ),
            [ChapterCreate(book_id=0, chapter_number=1, title="One")],
        )
    finally:
        repo.close()

    audit = audit_storage_state(
        db_path=db_path, output_dir=output_dir, temp_dir=temp_dir, grace_seconds=0.0
    )
    assert str(staging_dir.resolve()) in audit["orphan_delete_staging_dirs"]

    report = prune_stale_records_and_files(
        db_path=db_path,
        output_dir=output_dir,
        temp_dir=temp_dir,
        aggressive=True,
        grace_seconds=0.0,
    )

    assert not staging_dir.exists()
    assert str(staging_dir.resolve()) in report["actions"]["deleted_delete_staging_dirs"]
    assert not (temp_dir / "delete_staging").exists() or not any(
        (temp_dir / "delete_staging").iterdir()
    )


def test_prune_preserves_migrated_packaged_book_with_only_ondisk_audio(tmp_path):
    """A v3/legacy->v7 migrated packaged book has playback_path=NULL and chapters
    with NULL mp3_path, yet a real .m4b still lives in its output directory. The
    migration deliberately never probes disk to backfill, so aggressive prune must
    not delete the row nor rmtree the output dir, and should reconcile the playback
    metadata back to the on-disk artifact."""
    root = tmp_path
    db_path = root / "data" / "audiobooks.db"
    output_dir = root / "output"
    temp_dir = root / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    # External source kept by the user (mirrors a migrated legacy library).
    source = root / "library" / "migrated.epub"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"%EPUB migrated")

    packaged = output_dir / "Migrated Packaged" / "Migrated Packaged.m4b"
    packaged.parent.mkdir(parents=True, exist_ok=True)
    packaged.write_bytes(b"m4b durable audio")

    repo = SQLiteRepository(db_path, output_dir=output_dir, temp_dir=temp_dir)
    try:
        book, _chapters = repo.create_book_with_chapters(
            BookCreate(
                title="Migrated Packaged",
                author="Author",
                source_path=str(source),
                source_type=SourceType.EPUB,
                total_chapters=2,
                # Post-migration state: no playback pointer, status missing.
                playback_path=None,
                playback_status=PlaybackStatus.MISSING,
            ),
            [
                ChapterCreate(book_id=0, chapter_number=1, title="One"),
                ChapterCreate(book_id=0, chapter_number=2, title="Two"),
            ],
        )
    finally:
        repo.close()

    report = prune_stale_records_and_files(
        db_path,
        output_dir=output_dir,
        temp_dir=temp_dir,
        aggressive=True,
        grace_seconds=0.0,
    )

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT playback_path, playback_status FROM books WHERE id = ?",
            (book.id,),
        ).fetchone()
    finally:
        conn.close()

    # Row and durable audio survive.
    assert row is not None
    assert packaged.exists()
    assert packaged.parent.exists()
    assert book.id not in report["actions"]["deleted_book_rows"]

    # Reconciled back to the on-disk packaged artifact so a later prune keeps it
    # without rescanning the directory.
    reconciled_path, reconciled_status = row
    assert reconciled_path is not None
    assert Path(reconciled_path).resolve() == packaged.resolve()
    assert reconciled_status == PlaybackStatus.PACKAGED.value


def test_prune_preserves_migrated_book_with_only_ondisk_chapter_m4a(tmp_path):
    """Per-chapter .m4a artifacts on disk keep a migrated book alive even when the
    DB has no playback pointer and chapter mp3_path values are NULL, reconciling to
    chapter-audio playback."""
    root = tmp_path
    db_path = root / "data" / "audiobooks.db"
    output_dir = root / "output"
    temp_dir = root / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    source = root / "library" / "chaptered.epub"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"%EPUB chaptered")

    chapters_dir = output_dir / "Chaptered Book" / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)
    first_chapter_audio = chapters_dir / "chapter_01.m4a"
    first_chapter_audio.write_bytes(b"m4a one")
    (chapters_dir / "chapter_02.m4a").write_bytes(b"m4a two")

    repo = SQLiteRepository(db_path, output_dir=output_dir, temp_dir=temp_dir)
    try:
        book, _chapters = repo.create_book_with_chapters(
            BookCreate(
                title="Chaptered Book",
                author="Author",
                source_path=str(source),
                source_type=SourceType.EPUB,
                total_chapters=2,
                playback_path=None,
                playback_status=PlaybackStatus.MISSING,
            ),
            [
                ChapterCreate(book_id=0, chapter_number=1, title="One"),
                ChapterCreate(book_id=0, chapter_number=2, title="Two"),
            ],
        )
    finally:
        repo.close()

    report = prune_stale_records_and_files(
        db_path,
        output_dir=output_dir,
        temp_dir=temp_dir,
        aggressive=True,
        grace_seconds=0.0,
    )

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT playback_path, playback_status FROM books WHERE id = ?",
            (book.id,),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert first_chapter_audio.exists()
    assert chapters_dir.exists()
    assert book.id not in report["actions"]["deleted_book_rows"]

    reconciled_path, reconciled_status = row
    assert reconciled_path is not None
    assert Path(reconciled_path).resolve().parent == chapters_dir.resolve()
    assert reconciled_status == PlaybackStatus.CHAPTER_AUDIO.value


def test_prune_still_deletes_migrated_book_without_ondisk_audio(tmp_path):
    """A migrated book with NULL playback metadata and no audio anywhere on disk is
    genuinely orphaned and must still be pruned; the on-disk survival path must not
    weaken deletion of true orphans."""
    root = tmp_path
    db_path = root / "data" / "audiobooks.db"
    output_dir = root / "output"
    temp_dir = root / "temp"
    output_dir.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)

    repo = SQLiteRepository(db_path, output_dir=output_dir, temp_dir=temp_dir)
    try:
        book, _chapters = repo.create_book_with_chapters(
            BookCreate(
                title="Migrated Orphan",
                author="Author",
                source_path=str(root / "gone.epub"),
                source_type=SourceType.EPUB,
                total_chapters=1,
                playback_path=None,
                playback_status=PlaybackStatus.MISSING,
            ),
            [ChapterCreate(book_id=0, chapter_number=1, title="One")],
        )
    finally:
        repo.close()

    report = prune_stale_records_and_files(
        db_path,
        output_dir=output_dir,
        temp_dir=temp_dir,
        aggressive=True,
        grace_seconds=0.0,
    )

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT id FROM books WHERE id = ?", (book.id,)).fetchone()
    finally:
        conn.close()

    assert row is None
    assert book.id in report["actions"]["deleted_book_rows"]


if __name__ == "__main__":
    success = test_storage_maintenance()
    sys.exit(0 if success else 1)


def test_prune_retains_upload_source_file_until_source_move_commits(tmp_path, monkeypatch):
    root = tmp_path
    db_path = root / "data" / "audiobooks.db"
    output_dir = root / "output"
    temp_dir = root / "temp"
    upload_dir = temp_dir / "web_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    book_dir = output_dir / "Durable Book"
    book_dir.mkdir(parents=True, exist_ok=True)
    playback = book_dir / "Durable Book.m4b"
    playback.write_bytes(b"m4b-audio")

    upload_source = upload_dir / "durable_source.pdf"
    upload_source.write_bytes(b"%PDF source")

    repo = SQLiteRepository(db_path, output_dir=output_dir, temp_dir=temp_dir)
    try:
        book = repo.create_book(
            BookCreate(
                title="Durable Book",
                author="Author",
                source_path=str(upload_source),
                source_type=SourceType.PDF,
                total_chapters=0,
                playback_path=str(playback),
                playback_status=PlaybackStatus.PACKAGED,
            )
        )
    finally:
        repo.close()

    observed = {}
    real_connect = maintenance_module.sqlite3.connect

    class ProbeConnection:
        def __init__(self, connection):
            self._connection = connection

        @property
        def row_factory(self):
            return self._connection.row_factory

        @row_factory.setter
        def row_factory(self, value):
            self._connection.row_factory = value

        def execute(self, sql, parameters=()):
            normalized = " ".join(sql.split()).upper()
            if normalized.startswith("UPDATE BOOKS SET SOURCE_PATH"):
                # A crash between the file transfer and this commit must leave
                # the DB row pointing at a file that still exists.
                observed["source_exists_at_update"] = upload_source.exists()
            return self._connection.execute(sql, parameters)

        def backup(self, target):
            target_connection = getattr(target, "_connection", target)
            return self._connection.backup(target_connection)

        def commit(self):
            return self._connection.commit()

        def rollback(self):
            return self._connection.rollback()

        def close(self):
            return self._connection.close()

    def probe_connect(path, *args, **kwargs):
        connection = real_connect(path, *args, **kwargs)
        if Path(path) == db_path:
            return ProbeConnection(connection)
        return connection

    monkeypatch.setattr(maintenance_module.sqlite3, "connect", probe_connect)

    prune_stale_records_and_files(
        db_path,
        output_dir=output_dir,
        temp_dir=temp_dir,
        aggressive=True,
    )

    assert observed["source_exists_at_update"] is True

    moved = list((db_path.parent / "sources").glob("durable_source*.pdf"))
    assert len(moved) == 1
    assert moved[0].read_bytes() == b"%PDF source"
    # The consumed upload is only removed after the DB commit.
    assert not upload_source.exists()

    conn = sqlite3.connect(db_path)
    try:
        stored = conn.execute(
            "SELECT source_path FROM books WHERE id = ?", (book.id,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert Path(stored) == moved[0]
