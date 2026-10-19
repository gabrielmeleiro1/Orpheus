import os
import time

import pytest

from modules.storage.maintenance import audit_storage_state, prune_stale_records_and_files
from modules.storage.models import BookCreate, SourceType
from modules.storage.sqlite_repo import SQLiteRepository


def _write_aged_source(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"source bytes")
    stale = time.time() - 3 * 86400
    for entry in (path, path.parent):
        os.utime(entry, (stale, stale))


def _create_book(db_path, output_dir, temp_dir, source_path):
    repo = SQLiteRepository(db_path, output_dir=output_dir, temp_dir=temp_dir)
    try:
        return repo.create_book(
            BookCreate(
                title="Live Book",
                source_path=str(source_path),
                source_type=SourceType.TXT,
            )
        ).id
    finally:
        repo.close()


@pytest.mark.parametrize("case_variant", ["filename", "root"])
def test_cleanup_keeps_a_durable_source_referenced_through_a_case_variant_path(
    tmp_path, case_variant
):
    db_path = tmp_path / "data" / "audiobooks.db"
    output_dir = tmp_path / "output"
    temp_dir = tmp_path / "temp"
    source = db_path.parent / "sources" / "Live.txt"
    alias = (
        source.with_name("LIVE.TXT")
        if case_variant == "filename"
        else source.parent.with_name("SOURCES") / source.name
    )
    _write_aged_source(source)
    # Separate case-sensitive entries exercise the conservative retention rule.
    if not alias.exists():
        _write_aged_source(alias)
    orphan = source.with_name("unused.txt")
    _write_aged_source(orphan)
    book_id = _create_book(db_path, output_dir, temp_dir, alias)

    report = prune_stale_records_and_files(
        db_path, output_dir=output_dir, temp_dir=temp_dir,
        aggressive=True, grace_seconds=86400,
    )

    assert source.exists(), "Cleanup must keep a case-variant referenced durable source"
    assert source.read_bytes() == b"source bytes"
    assert report["audit_before"]["orphan_durable_sources"] == [str(orphan)]
    assert report["actions"]["deleted_durable_sources"] == [str(orphan)]
    assert book_id not in report["actions"]["deleted_book_rows"]
    assert not orphan.exists()


@pytest.mark.parametrize("case_variant", ["directory", "root"])
def test_cleanup_keeps_output_referenced_through_a_case_variant_path(
    tmp_path, case_variant
):
    db_path = tmp_path / "data" / "audiobooks.db"
    output_dir = tmp_path / "output"
    temp_dir = tmp_path / "temp"
    source = output_dir / "Live Book" / "source.txt"
    alias = (
        output_dir / "LIVE BOOK" / source.name
        if case_variant == "directory"
        else output_dir.with_name("OUTPUT") / source.parent.name / source.name
    )
    _write_aged_source(source)
    if not alias.exists():
        _write_aged_source(alias)
    orphan = output_dir / "Unused Book" / "source.txt"
    _write_aged_source(orphan)
    book_id = _create_book(db_path, output_dir, temp_dir, alias)

    report = prune_stale_records_and_files(
        db_path, output_dir=output_dir, temp_dir=temp_dir,
        aggressive=True, grace_seconds=86400,
    )

    assert source.exists(), "Cleanup must keep a case-variant referenced output directory"
    assert source.read_bytes() == b"source bytes"
    assert report["audit_before"]["orphan_output_dirs"] == [str(orphan.parent)]
    assert report["actions"]["deleted_output_dirs"] == [str(orphan.parent)]
    assert book_id not in report["actions"]["deleted_book_rows"]
    assert not orphan.exists()


@pytest.mark.parametrize("case_variant", ["filename", "root", "upload_directory"])
def test_cleanup_keeps_temp_upload_referenced_through_a_case_variant_path(
    tmp_path, case_variant
):
    db_path = tmp_path / "data" / "audiobooks.db"
    output_dir = tmp_path / "output"
    temp_dir = tmp_path / "temp"
    source = temp_dir / "web_uploads" / "Live.txt"
    aliases = {
        "filename": source.with_name("LIVE.TXT"),
        "root": temp_dir.with_name("TEMP") / "web_uploads" / source.name,
        "upload_directory": temp_dir / "WEB_UPLOADS" / source.name,
    }
    alias = aliases[case_variant]
    _write_aged_source(source)
    if not alias.exists():
        _write_aged_source(alias)
    orphan = source.with_name("unused.txt")
    _write_aged_source(orphan)
    _create_book(db_path, output_dir, temp_dir, alias)

    state = audit_storage_state(
        db_path, output_dir=output_dir, temp_dir=temp_dir, grace_seconds=86400,
    )

    assert str(source) not in state["stale_temp_uploads"]
    assert state["stale_temp_uploads"] == [str(orphan)]
