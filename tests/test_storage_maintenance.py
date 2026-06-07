"""
Test storage maintenance audit/prune helpers.
"""

import sqlite3
import sys
import tempfile
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
        )

    assert orphan_output.exists()


if __name__ == "__main__":
    success = test_storage_maintenance()
    sys.exit(0 if success else 1)
