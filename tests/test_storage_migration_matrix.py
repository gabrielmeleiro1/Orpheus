"""Every historical schema version migrates to the same schema as a new library."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from modules.storage import migrations
from modules.storage.sqlite_repo import SQLiteRepository
from tests.test_storage_migrations import _create_legacy_schema

# 0 is the legacy schema without migration metadata; 1 is the same schema
# recorded as version 1. Later versions come from the migration list itself.
HISTORICAL_VERSIONS = [0, 1, *(version for version, _ in migrations.MIGRATIONS[:-1])]


def _database_at(path: Path, version: int) -> None:
    _create_legacy_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        if version >= 1:
            migrations._ensure_migrations_table(conn)
            conn.execute("INSERT INTO schema_migrations(version) VALUES (1)")
            conn.commit()
        for target, disable_foreign_keys in migrations.MIGRATIONS:
            if target > version:
                break
            migrations._apply_migration(
                conn, target, migrations.migration_step(target), disable_foreign_keys=disable_foreign_keys,
            )
        assert migrations.check_schema_version(conn) == version


def _checks(sql: str) -> list[str]:
    return sorted(re.sub(r"\s+", " ", match).strip().lower() for match in re.findall(r"CHECK\s*\((.*?\))\)", sql))


def _schema(path: Path) -> dict:
    with closing(sqlite3.connect(path)) as conn:
        tables = {
            name: sql for name, sql in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        schema = {}
        for table, sql in tables.items():
            columns = sorted(
                (row[1], row[2].upper(), row[3], row[4], row[5])
                for row in conn.execute(f"PRAGMA table_info({table})")
            )
            foreign_keys = sorted(
                (row[2], row[3], row[4], row[6]) for row in conn.execute(f"PRAGMA foreign_key_list({table})")
            )
            indexes = sorted(
                (
                    bool(row[2]),
                    tuple(info[2] for info in conn.execute(f"PRAGMA index_info('{row[1]}')")),
                    bool(row[4]),
                )
                for row in conn.execute(f"PRAGMA index_list({table})")
            )
            schema[table] = {"columns": columns, "foreign_keys": foreign_keys, "indexes": indexes, "checks": _checks(sql)}
        return schema


@pytest.fixture(scope="module")
def fresh_schema(tmp_path_factory) -> dict:
    path = tmp_path_factory.mktemp("fresh") / "library.db"
    SQLiteRepository(path).close()
    return _schema(path)


@pytest.mark.parametrize("version", HISTORICAL_VERSIONS)
def test_every_historical_version_migrates_to_the_fresh_schema(tmp_path, fresh_schema, version) -> None:
    path = tmp_path / "library.db"
    _database_at(path, version)

    repository = SQLiteRepository(path)
    try:
        book = repository.get_book_with_chapters(1)
        identity = repository.get_library_identity()
    finally:
        repository.close()

    assert _schema(path) == fresh_schema
    assert book is not None
    saved, chapters = book
    assert (saved.title, saved.author, saved.source_path) == ("legacy", "author", "/tmp/source.pdf")
    assert [(c.chapter_number, c.title, c.duration_ms, c.mp3_path) for c in chapters] == [
        (1, "chapter", 5000, "/tmp/chapter.mp3"),
    ]
    assert identity["library_id"] and identity["library_epoch"]
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert migrations.check_schema_version(conn) == migrations.LATEST_SCHEMA_VERSION


@pytest.mark.parametrize("failing_version", [version for version, _ in migrations.MIGRATIONS])
def test_interrupted_step_leaves_its_previous_version_and_reopening_completes(
    tmp_path, fresh_schema, monkeypatch, failing_version,
) -> None:
    path = tmp_path / "library.db"
    _database_at(path, 1)
    real_step = migrations.migration_step(failing_version)

    def crash_after_ddl(conn):
        real_step(conn)
        raise sqlite3.OperationalError("disk I/O error")

    with monkeypatch.context() as patch:
        patch.setattr(migrations, f"_migrate_to_v{failing_version}", crash_after_ddl)
        with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
            SQLiteRepository(path)

    previous = max([1, *(version for version, _ in migrations.MIGRATIONS if version < failing_version)])
    with closing(sqlite3.connect(path)) as conn:
        assert migrations.check_schema_version(conn) == previous

    SQLiteRepository(path).close()
    assert _schema(path) == fresh_schema
