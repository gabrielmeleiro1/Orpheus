"""Managed files remain attached to their library across path changes."""

from contextlib import closing
import json
import os
import sqlite3
from pathlib import Path
import shutil

import pytest

from modules.storage.db_ownership import acquire_database
from modules.storage.managed_roots import STORAGE_OWNER_NAME, StorageOwnershipError
from modules.storage.models import BookCreate, SourceType
from modules.storage.sqlite_repo import SQLiteRepository


def _create_library(database):
    repo = SQLiteRepository(database)
    try:
        book = repo.create_book(BookCreate(
            title="Retained book", source_path="data/sources/book.txt", source_type=SourceType.TXT,
        ))
        identity = repo.get_library_identity()["library_id"]
        roots = (database.parent / "sources", repo._output_dir, repo._temp_dir)
        for root in roots:
            (root / "retained.txt").write_bytes(b"library content")
        return book.id, identity, roots
    finally:
        repo.close()


def _assert_library(database, book_id, identity):
    repo = SQLiteRepository(database)
    try:
        assert repo.get_book(book_id).title == "Retained book"
        assert repo.get_library_identity()["library_id"] == identity
        for root in (database.parent / "sources", repo._output_dir, repo._temp_dir):
            assert (root / "retained.txt").read_bytes() == b"library content"
            record = json.loads((root / STORAGE_OWNER_NAME).read_text())
            assert record["version"] == 2
            assert record["library_id"] == identity
            assert Path(record["database"]).samefile(database)
    finally:
        repo.close()


def test_moving_the_whole_library_folder_keeps_it_openable(tmp_path):
    original = tmp_path / "original"
    database = original / "data" / "library.db"
    book_id, identity, _ = _create_library(database)
    moved = tmp_path / "moved"
    original.rename(moved)
    _assert_library(moved / "data" / "library.db", book_id, identity)


def test_renaming_the_database_file_keeps_its_storage(tmp_path):
    database = tmp_path / "data" / "first.db"
    book_id, identity, _ = _create_library(database)
    renamed = database.with_name("second.db")
    database.rename(renamed)
    _assert_library(renamed, book_id, identity)


def _require_case_insensitive_filesystem(tmp_path):
    probe = tmp_path / "CaseProbe"
    probe.write_bytes(b"probe")
    if not (tmp_path / "caseprobe").exists():
        pytest.skip("Temporary filesystem is case-sensitive")


def test_case_alias_of_the_database_path_reopens_the_same_library(tmp_path):
    _require_case_insensitive_filesystem(tmp_path)
    database = tmp_path / "data" / "CaseLibrary.db"
    book_id, identity, _ = _create_library(database)
    alias = database.with_name("caselibrary.db")
    assert database.samefile(alias)
    _assert_library(alias, book_id, identity)


def test_copied_database_cannot_adopt_storage_while_the_original_exists(tmp_path):
    database = tmp_path / "data" / "original.db"
    _, _, roots = _create_library(database)
    markers = [(root / STORAGE_OWNER_NAME).read_bytes() for root in roots]
    copied = database.with_name("copied.db")
    shutil.copyfile(database, copied)
    with pytest.raises(StorageOwnershipError, match="belongs to"):
        SQLiteRepository(copied)
    assert [(root / STORAGE_OWNER_NAME).read_bytes() for root in roots] == markers


def test_different_library_cannot_adopt_storage_of_a_missing_database(tmp_path):
    database = tmp_path / "original" / "data" / "library.db"
    _, identity, roots = _create_library(database)
    other = tmp_path / "other" / "data" / "library.db"
    _, other_identity, _ = _create_library(other)
    assert other_identity != identity
    markers = [(root / STORAGE_OWNER_NAME).read_bytes() for root in roots]
    database.unlink()
    with pytest.raises(StorageOwnershipError, match="belongs to"):
        SQLiteRepository(other, output_dir=roots[1], temp_dir=roots[2])
    assert [(root / STORAGE_OWNER_NAME).read_bytes() for root in roots] == markers


def test_case_alias_shares_the_active_database_lease(tmp_path):
    _require_case_insensitive_filesystem(tmp_path)
    database = tmp_path / "CaseLibrary.db"
    database.touch()
    with acquire_database(database) as first:
        with acquire_database(database.with_name("caselibrary.db")) as second:
            assert second.lock is first.lock
            assert second.path == first.path


def test_retargeted_database_filename_does_not_reuse_the_active_lease(tmp_path):
    database, target = tmp_path / "first.db", tmp_path / "second.db"
    database.touch()
    target.touch()
    with acquire_database(database) as first:
        database.unlink()
        try:
            database.symlink_to(target)
        except OSError as error:
            pytest.skip(f"Database symlinks are unavailable: {error}")
        with acquire_database(target) as second:
            assert second.lock is not first.lock
            assert second.path == target.resolve()


def test_renaming_a_database_with_legacy_markers_keeps_the_parent_layout(tmp_path):
    database = tmp_path / "data" / "first.db"
    book_id, identity, roots = _create_library(database)
    for root in roots:
        (root / STORAGE_OWNER_NAME).write_text(json.dumps({
            "version": 1, "database": str(database.resolve()),
        }))
    renamed = database.with_name("second.db")
    database.rename(renamed)
    _assert_library(renamed, book_id, identity)


def test_moving_a_library_with_legacy_markers_keeps_the_default_layout(tmp_path):
    original = tmp_path / "original"
    database = original / "data" / "library.db"
    book_id, identity, roots = _create_library(database)
    for root in roots:
        (root / STORAGE_OWNER_NAME).write_text(json.dumps({
            "version": 1, "database": str(database.resolve()),
        }))
    moved = tmp_path / "moved"
    original.rename(moved)
    _assert_library(moved / "data" / "library.db", book_id, identity)


def test_legacy_layout_cannot_be_adopted_while_the_original_storage_exists(tmp_path):
    original = tmp_path / "original"
    database = original / "data" / "library.db"
    _, _, roots = _create_library(database)
    for root in roots:
        (root / STORAGE_OWNER_NAME).write_text(json.dumps({
            "version": 1, "database": str(database.resolve()),
        }))
    copied = tmp_path / "copied"
    shutil.copytree(original, copied)
    database.unlink()
    with pytest.raises(StorageOwnershipError, match="belongs to"):
        SQLiteRepository(copied / "data" / "library.db")


def test_legacy_markers_outside_the_parent_layout_cannot_be_adopted(tmp_path):
    database = tmp_path / "data" / "first.db"
    _, _, roots = _create_library(database)
    outside = tmp_path / "external-output"
    outside.mkdir()
    marker = outside / STORAGE_OWNER_NAME
    marker.write_text(json.dumps({"version": 1, "database": str(database.resolve())}))
    before = marker.read_bytes()
    renamed = database.with_name("second.db")
    database.rename(renamed)
    with pytest.raises(StorageOwnershipError, match="belongs to"):
        SQLiteRepository(renamed, output_dir=outside, temp_dir=roots[2])
    assert marker.read_bytes() == before


@pytest.mark.skipif(os.name == "nt", reason="Windows cannot rename an open database")
def test_copy_cannot_adopt_roots_of_a_live_renamed_database(tmp_path):
    repo = SQLiteRepository(tmp_path / "original" / "data" / "library.db")
    copied = tmp_path / "copied" / "data" / "library.db"
    copied.parent.mkdir(parents=True)
    try:
        with closing(sqlite3.connect(copied)) as conn:
            repo._conn.backup(conn)
        roots = (repo.db_path.parent / "sources", repo._output_dir, repo._temp_dir)
        markers = [(root / STORAGE_OWNER_NAME).read_bytes() for root in roots]
        repo.db_path.rename(repo.db_path.with_name("renamed.db"))
        with pytest.raises(StorageOwnershipError, match="belongs to"):
            other = SQLiteRepository(copied, output_dir=roots[1], temp_dir=roots[2])
            other.close()
        assert [(root / STORAGE_OWNER_NAME).read_bytes() for root in roots] == markers
    finally:
        repo.close()


def _library_with_absolute_paths(root, *, configured=False):
    from modules.storage.models import ChapterCreate, PlaybackStatus

    options = dict(output_dir=root / "audio", temp_dir=root / "scratch") if configured else {}
    repo = SQLiteRepository(root / "data" / "library.db", **options)
    source = repo.db_path.parent / "sources" / "book.txt"
    audio = repo._output_dir / "book.m4b"
    chapter = repo._temp_dir / "chapter.mp3"
    for path in (source, audio, chapter):
        path.write_bytes(b"content")
    book = repo.create_book(BookCreate(
        title="Playable", source_path=str(source), source_type=SourceType.TXT,
        playback_path=str(audio), playback_status=PlaybackStatus.PACKAGED,
    ))
    repo.create_chapter(ChapterCreate(
        book_id=book.id, chapter_number=1, title="Chapter", mp3_path=str(chapter),
    ))
    return repo, book.id, (source, audio, chapter)


def _assert_paths(repo, book_id, paths):
    book = repo.get_book(book_id)
    actual = (book.source_path, book.playback_path, repo.get_chapters(book_id)[0].mp3_path)
    assert actual == tuple(str(path) for path in paths)
    for path in actual:
        assert repo._resolve_stored_path(path).read_bytes() == b"content"


@pytest.mark.parametrize("configured", [False, True])
def test_moved_library_keeps_audio_and_sources_resolvable(tmp_path, configured):
    original, moved = tmp_path / "original", tmp_path / "moved"
    repo, book_id, paths = _library_with_absolute_paths(original, configured=configured)
    repo.close()
    original.rename(moved)
    options = dict(output_dir=moved / "audio", temp_dir=moved / "scratch") if configured else {}
    for _ in range(2):
        with closing(SQLiteRepository(moved / "data" / "library.db", **options)) as repo:
            _assert_paths(repo, book_id, tuple(moved / path.relative_to(original) for path in paths))


def test_renamed_database_keeps_paths(tmp_path):
    repo, book_id, paths = _library_with_absolute_paths(tmp_path)
    database = repo.db_path
    repo.close()
    renamed = database.with_name("renamed.db")
    database.rename(renamed)
    with closing(SQLiteRepository(renamed)) as repo:
        _assert_paths(repo, book_id, paths)


def test_paths_outside_managed_roots_are_not_rewritten(tmp_path):
    original, moved = tmp_path / "original", tmp_path / "moved"
    repo, book_id, _ = _library_with_absolute_paths(original)
    external = original / "output-other" / "external.m4b"
    external.parent.mkdir()
    external.write_bytes(b"external")
    repo.update_book(book_id, source_path=str(external), playback_path=str(original / "output"))
    repo.close()
    original.rename(moved)
    with closing(SQLiteRepository(moved / "data" / "library.db")) as repo:
        assert repo.get_book(book_id).source_path == str(external)
        assert repo.get_book(book_id).playback_path == str(original / "output")


def test_opening_a_newer_schema_keeps_the_database_unchanged(tmp_path):
    from modules.storage.migrations import FutureSchemaError, LATEST_SCHEMA_VERSION, run_migrations

    database = tmp_path / "data" / "library.db"
    database.parent.mkdir()
    with closing(sqlite3.connect(database)) as conn, conn:
        run_migrations(conn)
        conn.execute("INSERT INTO schema_migrations(version) VALUES (?)", (LATEST_SCHEMA_VERSION + 1,))
    intact = database.read_bytes()
    with pytest.raises(FutureSchemaError, match="newer version of Orpheus"):
        SQLiteRepository(database).close()
    assert database.read_bytes() == intact, "opening a newer schema changed the database"


@pytest.mark.parametrize("recorded_layout", [False, True])
def test_restoring_a_pre_move_backup_remaps_paths_into_the_moved_library(tmp_path, recorded_layout):
    from modules.storage.maintenance import backup_database, restore_database_backup

    original, moved = tmp_path / "original", tmp_path / "moved"
    repo, book_id, paths = _library_with_absolute_paths(original)
    backup = backup_database(repo.db_path)
    if not recorded_layout:
        with closing(sqlite3.connect(backup)) as conn, conn:
            conn.execute("DELETE FROM library_metadata WHERE key='managed_roots'")
    repo.close()
    original.rename(moved)
    with closing(SQLiteRepository(moved / "data" / "library.db")) as repo:
        restore_database_backup(repo, backup.name)
        _assert_paths(repo, book_id, tuple(moved / path.relative_to(original) for path in paths))


def test_restoring_a_post_move_backup_keeps_new_external_sources(tmp_path):
    from modules.storage.maintenance import backup_database, restore_database_backup

    original, moved = tmp_path / "original", tmp_path / "moved"
    repo, book_id, paths = _library_with_absolute_paths(original)
    repo.close()
    original.rename(moved)
    with closing(SQLiteRepository(moved / "data" / "library.db")) as repo:
        external = original / "data" / "sources" / "book.txt"
        external.parent.mkdir(parents=True)
        external.write_bytes(b"new external book")
        book = repo.create_book(BookCreate(
            title="External book", source_path=str(external), source_type=SourceType.TXT,
        ))
        backup = backup_database(repo.db_path)
        with closing(sqlite3.connect(backup)) as conn:
            layout = json.loads(conn.execute("SELECT value FROM library_metadata WHERE key='managed_roots'").fetchone()[0])
        assert {key: os.path.normcase(value) for key, value in layout.items()} == {
            "sources": os.path.normcase(str(repo.db_path.parent / "sources")),
            "output": os.path.normcase(str(repo._output_dir)), "temp": os.path.normcase(str(repo._temp_dir)),
        }
        restore_database_backup(repo, backup.name)
        assert repo.get_book(book.id).source_path == str(external)
        assert repo._resolve_stored_path(repo.get_book(book.id).source_path).read_bytes() == b"new external book"
        _assert_paths(repo, book_id, tuple(moved / path.relative_to(original) for path in paths))


def test_interrupted_marker_publication_repeats_path_remapping_safely(tmp_path, monkeypatch):
    from modules.storage import managed_roots
    from modules.storage.maintenance import backup_database, restore_database_backup

    original, moved = tmp_path / "original", tmp_path / "moved"
    repo, book_id, paths = _library_with_absolute_paths(original)
    backup = backup_database(repo.db_path)
    repo.close()
    original.rename(moved)
    database = moved / "data" / "library.db"
    publish = managed_roots.atomic_write_json
    published = []

    def interrupt_publication(marker, record):
        if published:
            raise OSError("interrupted marker publication")
        publish(marker, record)
        published.append(marker)

    with monkeypatch.context() as patch:
        patch.setattr(managed_roots, "atomic_write_json", interrupt_publication)
        with pytest.raises(OSError, match="interrupted marker publication"):
            SQLiteRepository(database)
    expected = tuple(moved / path.relative_to(original) for path in paths)
    with closing(SQLiteRepository(database)) as repo:
        _assert_paths(repo, book_id, expected)
        restore_database_backup(repo, backup.name)
        _assert_paths(repo, book_id, expected)


def test_path_remapping_failure_rolls_back_all_columns_before_marker_changes(tmp_path):
    original, moved = tmp_path / "original", tmp_path / "moved"
    repo, _, paths = _library_with_absolute_paths(original)
    with repo._connection() as conn:
        layout = conn.execute("SELECT value FROM library_metadata WHERE key='managed_roots'").fetchone()[0]
        conn.execute("CREATE TRIGGER keep_chapters BEFORE UPDATE OF mp3_path ON chapters "
                     "BEGIN SELECT RAISE(ABORT, 'cannot update chapter'); END")
    repo.close()
    original.rename(moved)
    database = moved / "data" / "library.db"
    roots = (moved / "data" / "sources", moved / "output", moved / "temp")
    markers = [(root / STORAGE_OWNER_NAME).read_bytes() for root in roots]
    with pytest.raises(sqlite3.IntegrityError, match="cannot update chapter"):
        SQLiteRepository(database)
    assert [(root / STORAGE_OWNER_NAME).read_bytes() for root in roots] == markers
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT value FROM library_metadata WHERE key='managed_roots'").fetchone()[0] == layout
        assert conn.execute("SELECT source_path, playback_path FROM books").fetchone() == tuple(map(str, paths[:2]))
        assert conn.execute("SELECT mp3_path FROM chapters").fetchone()[0] == str(paths[2])


def test_restoring_a_backup_before_two_moves_uses_the_latest_roots(tmp_path):
    from modules.storage.maintenance import backup_database, restore_database_backup

    original, first, last = (tmp_path / name for name in ("original", "first", "last"))
    repo, book_id, paths = _library_with_absolute_paths(original)
    backup = backup_database(repo.db_path)
    repo.close()
    original.rename(first)
    SQLiteRepository(first / "data" / "library.db").close()
    first.rename(last)
    with closing(SQLiteRepository(last / "data" / "library.db")) as repo:
        restore_database_backup(repo, backup.name)
        _assert_paths(repo, book_id, tuple(last / path.relative_to(original) for path in paths))


def test_moving_library_with_markers_without_root_paths_rebases_configured_paths(tmp_path):
    original, moved = tmp_path / "original", tmp_path / "moved"
    repo, book_id, paths = _library_with_absolute_paths(original, configured=True)
    roots = (repo.db_path.parent / "sources", repo._output_dir, repo._temp_dir)
    repo.close()
    for root in roots:
        marker = root / STORAGE_OWNER_NAME
        record = json.loads(marker.read_text())
        record.pop("root")
        record.pop("previous_roots")
        marker.write_text(json.dumps(record))
    original.rename(moved)
    with closing(SQLiteRepository(
        moved / "data" / "library.db", output_dir=moved / "audio", temp_dir=moved / "scratch",
    )) as repo:
        _assert_paths(repo, book_id, tuple(moved / path.relative_to(original) for path in paths))


def test_restoring_a_backup_after_many_moves_keeps_its_sources(tmp_path):
    from modules.storage.maintenance import backup_database, prune_stale_records_and_files, restore_database_backup

    original = current = tmp_path / "move0"
    repo, book_id, paths = _library_with_absolute_paths(current)
    backup = backup_database(repo.db_path)
    repo.close()
    for index in range(1, 11):
        destination = tmp_path / f"move{index}"
        current.rename(destination)
        current = destination
        SQLiteRepository(current / "data" / "library.db").close()
    expected = tuple(current / path.relative_to(original) for path in paths)
    with closing(SQLiteRepository(current / "data" / "library.db")) as repo:
        restore_database_backup(repo, backup.name)
        _assert_paths(repo, book_id, expected)
        prune_stale_records_and_files(
            repo.db_path, current / "output", current / "temp", aggressive=True, grace_seconds=0,
        )
        assert expected[0].read_bytes() == b"content"
        assert repo.get_book(book_id) is not None
        for root in (current / "data" / "sources", current / "output", current / "temp"):
            history = json.loads((root / STORAGE_OWNER_NAME).read_text())["previous_roots"]
            assert len(history) == len(set(history)) == 10


def test_reopening_a_moved_library_does_not_rewrite_new_external_paths(tmp_path):
    original, moved = tmp_path / "original", tmp_path / "moved"
    repo, _, _ = _library_with_absolute_paths(original)
    repo.close()
    original.rename(moved)
    with closing(SQLiteRepository(moved / "data" / "library.db")) as repo:
        external = original / "data" / "sources" / "book.txt"
        external.parent.mkdir(parents=True)
        external.write_bytes(b"new external book")
        book = repo.create_book(BookCreate(
            title="External book", source_path=str(external), source_type=SourceType.TXT,
        ))
    for _ in range(2):
        with closing(SQLiteRepository(moved / "data" / "library.db")) as repo:
            assert repo.get_book(book.id).source_path == str(external)
            assert repo._resolve_stored_path(repo.get_book(book.id).source_path).read_bytes() == b"new external book"


def test_moving_a_library_with_a_pending_deletion_restores_its_live_files(tmp_path):
    from modules.app.services.library_service import LibraryService
    from modules.storage.deletion import LOCAL_DELETE_STAGING, write_delete_transaction
    from tests.test_library_service import _create_deletion_book

    original, moved = tmp_path / "original", tmp_path / "moved"
    repo, service, book, directory, audio, source = _create_deletion_book(original)
    try:
        entry = service._delete_staging_root() / "pending-delete"
        entry.mkdir(parents=True)
        artifacts = [
            (path, path.parent / LOCAL_DELETE_STAGING / entry.name / path.name)
            for path in (directory, source)
        ]
        write_delete_transaction(entry, book.id, artifacts)
        for original_path, staged in artifacts:
            service._stage_artifact_for_deletion(original_path, staged)
    finally:
        repo.close()
    original.rename(moved)
    with closing(SQLiteRepository(moved / "data" / "library.db")) as repo:
        LibraryService(repo, output_dir=moved / "output", temp_dir=moved / "temp", storage_root=moved)
        assert repo.get_book(book.id) is not None
        assert (moved / audio.relative_to(original)).is_file(), "live chapter remains stranded in staging"
        assert (moved / audio.relative_to(original)).read_bytes() == b"chapter audio"
        assert (moved / source.relative_to(original)).read_bytes() == b"source bytes"
        assert not (moved / entry.relative_to(original)).exists()
        assert all(not (moved / staged.relative_to(original)).exists() for _, staged in artifacts)


@pytest.mark.parametrize("operation", ["read_delete_transaction", "write_delete_transaction"])
def test_failed_deletion_inventory_rebase_keeps_adoption_retryable(tmp_path, monkeypatch, operation):
    from modules.app.services.library_service import LibraryService
    from modules.storage import library_relocation
    from modules.storage.deletion import LOCAL_DELETE_STAGING, write_delete_transaction

    original, moved = tmp_path / "original", tmp_path / "moved"
    repo, book_id, paths = _library_with_absolute_paths(original)
    try:
        entry = repo._temp_dir / "delete_staging" / "pending-delete"
        entry.mkdir(parents=True)
        source = paths[0]
        staged = source.parent / LOCAL_DELETE_STAGING / entry.name / source.name
        write_delete_transaction(entry, book_id, [(source, staged)])
        staged.parent.mkdir(parents=True)
        source.rename(staged)
    finally:
        repo.close()
    original.rename(moved)
    roots = (moved / "data" / "sources", moved / "output", moved / "temp")
    markers = [(root / STORAGE_OWNER_NAME).read_bytes() for root in roots]

    def fail_rebase(*args, **kwargs):
        raise OSError("cannot access deletion inventory")

    with monkeypatch.context() as patch:
        patch.setattr(library_relocation, operation, fail_rebase)
        with pytest.raises(OSError, match="cannot access deletion inventory"):
            SQLiteRepository(moved / "data" / "library.db").close()
    assert [(root / STORAGE_OWNER_NAME).read_bytes() for root in roots] == markers
    with closing(SQLiteRepository(moved / "data" / "library.db")) as repo:
        LibraryService(repo, output_dir=moved / "output", temp_dir=moved / "temp", storage_root=moved)
        _assert_paths(repo, book_id, tuple(moved / path.relative_to(original) for path in paths))
        assert not (moved / entry.relative_to(original)).exists()


def test_interrupted_inventory_rebase_never_leaves_a_truncated_manifest(tmp_path, monkeypatch):
    from modules.storage.deletion import DELETE_MANIFEST, LOCAL_DELETE_STAGING, read_delete_transaction, write_delete_transaction

    original, moved = tmp_path / "original", tmp_path / "moved"
    repo, book_id, paths = _library_with_absolute_paths(original)
    with closing(repo):
        entry = repo._temp_dir / "delete_staging" / "pending-delete"
        entry.mkdir(parents=True)
        source = paths[0]
        staged = source.parent / LOCAL_DELETE_STAGING / entry.name / source.name
        write_delete_transaction(entry, book_id, [(source, staged)])
        intact = (entry / DELETE_MANIFEST).read_bytes()
    original.rename(moved)
    entry = moved / entry.relative_to(original)
    manifest = entry / DELETE_MANIFEST
    dump, write_text = json.dump, Path.write_text
    interrupted = []

    def is_inventory(path):
        return path.parent == entry and path.name in (DELETE_MANIFEST, f"{DELETE_MANIFEST}.tmp")

    def interrupt_dump(payload, stream, *args, **kwargs):
        if is_inventory(Path(stream.name)):
            stream.write('{"book_id":')
            interrupted.append(Path(stream.name))
            raise OSError("interrupted inventory write")
        return dump(payload, stream, *args, **kwargs)

    def interrupt_text(path, payload, *args, **kwargs):
        if is_inventory(path):
            write_text(path, payload[:10], *args, **kwargs)
            interrupted.append(path)
            raise OSError("interrupted inventory write")
        return write_text(path, payload, *args, **kwargs)

    database = moved / "data" / "library.db"
    with monkeypatch.context() as patch:
        patch.setattr(json, "dump", interrupt_dump)
        patch.setattr(Path, "write_text", interrupt_text)
        with pytest.raises(OSError, match="interrupted inventory write"):
            SQLiteRepository(database).close()
    assert interrupted
    assert manifest.read_bytes() == intact, "interrupted inventory write truncated the original manifest"
    with closing(SQLiteRepository(database)) as repo:
        roots = (database.parent / "sources", repo._output_dir, repo._temp_dir)
        expected_source = moved / source.relative_to(original)
        expected_staged = moved / staged.relative_to(original)
        assert read_delete_transaction(entry, allowed_roots=roots) == (book_id, [(expected_source, expected_staged)])
        _assert_paths(repo, book_id, tuple(moved / path.relative_to(original) for path in paths))
