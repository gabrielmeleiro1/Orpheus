"""Regression coverage for native Windows database paths in maintenance."""

from contextlib import closing
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import sqlite3
from urllib.parse import urlsplit

import pytest

from modules.storage.sqlite_uri import readonly_sqlite_uri


@pytest.mark.parametrize("path", [
    PurePosixPath("/tmp/café #1%.db"),
    PureWindowsPath("C:/Books/café #1%.db"),
    PureWindowsPath("//?/C:/Books/café #1%.db"),
    PureWindowsPath("//server/share/café #1%.db"),
    PureWindowsPath("//?/UNC/server/share/café #1%.db"),
])
def test_sqlite_uri_keeps_filename_out_of_authority(path):
    uri = readonly_sqlite_uri(path)
    parsed = urlsplit(uri)
    assert parsed.scheme == "file"
    assert parsed.netloc == ""
    assert parsed.query == "mode=ro"
    assert parsed.fragment == ""
    assert "%23" in parsed.path and "%25" in parsed.path
    assert "%3F" not in parsed.path


def test_sqlite_uri_opens_exact_special_character_filename_read_only(tmp_path):
    path = tmp_path / "café #1%.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE sample (value TEXT)")
        conn.commit()
    with closing(sqlite3.connect(readonly_sqlite_uri(path), uri=True)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sample").fetchone() == (0,)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("INSERT INTO sample VALUES ('write')")


@pytest.mark.skipif(os.name != "nt", reason="Windows verbatim native paths")
def test_native_verbatim_path_supports_diagnostics_backup_restore_and_rollback(tmp_path):
    from modules.storage.maintenance import (
        audit_storage_state, backup_database, list_database_backups, restore_database_backup,
    )
    from modules.storage.models import BookCreate, SourceType
    from modules.storage.restore_recovery import record_restore_rollback
    from modules.storage.sqlite_repo import SQLiteRepository

    root = Path("\\\\?\\" + str(tmp_path.resolve()))
    database = root / "data" / "library #%.db"
    with closing(SQLiteRepository(database)) as repo:
        book = repo.create_book(BookCreate(title="Saved", source_path="source.txt", source_type=SourceType.TXT))
        audit_storage_state(database, output_dir=root / "output", temp_dir=root / "temp")
        backup = backup_database(database)
        listed = list_database_backups(database)
        assert next(item for item in listed if item["id"] == backup.name)["book_count"] == 1
        repo.update_book(book.id, title="Current")
        safety = restore_database_backup(repo, backup.name)
        assert repo.get_book(book.id).title == "Saved"
        record_restore_rollback(repo.db_path, safety)

    with closing(SQLiteRepository(database)) as recovered:
        assert recovered.get_book(book.id).title == "Current"
