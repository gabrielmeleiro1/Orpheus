"""One undecodable row must not take the whole library down."""

from __future__ import annotations

import logging
from contextlib import closing
import sqlite3

import pytest
from fastapi.testclient import TestClient

from modules.storage.models import BookCreate, ChapterCreate, SourceType
from modules.storage import sqlite_repo
from modules.web.server import create_app
from tests.test_backup_restore import controller as controller_fixture

controller = controller_fixture


@pytest.fixture
def library(controller, monkeypatch):
    monkeypatch.setattr(sqlite_repo, "_reported_unreadable_values", set())
    ids = []
    for title in ("First", "Broken", "Last"):
        book, _ = controller.repository.create_book_with_chapters(
            BookCreate(title=title, source_path=f"{title}.txt", source_type=SourceType.TXT, total_chapters=1),
            [ChapterCreate(book_id=0, chapter_number=1, title="One", duration_ms=1000, packaged_start_ms=0)],
        )
        ids.append(book.id)
    broken = ids[1]
    with closing(sqlite3.connect(controller.config.db_path)) as conn, conn:
        conn.execute(
            "UPDATE books SET created_at = 'last tuesday', updated_at = 'soon', playback_position_sec = 'abc' "
            "WHERE id = ?", (broken,),
        )
        conn.execute(
            "UPDATE chapters SET duration_ms = 'long', packaged_start_ms = 'start' WHERE book_id = ?", (broken,),
        )
    return TestClient(create_app(controller=controller)), ids


def test_library_list_keeps_every_book_when_one_row_is_unreadable(library) -> None:
    client, ids = library

    response = client.get("/api/library")

    assert response.status_code == 200
    items = {item["id"]: item for item in response.json()["items"]}
    assert set(items) == set(ids)
    assert items[ids[1]]["created_at"] is None
    assert items[ids[0]]["created_at"] and items[ids[2]]["created_at"]


def test_book_detail_and_cover_survive_unreadable_values(library) -> None:
    client, ids = library

    detail = client.get(f"/api/library/{ids[1]}")
    cover = client.get(f"/api/library/{ids[1]}/cover")

    assert detail.status_code == 200
    payload = detail.json()
    assert payload["title"] == "Broken"
    assert payload["created_at"] is None
    assert payload["playback_position_sec"] == 0.0
    assert [(chapter["duration_ms"], chapter["packaged_start_ms"]) for chapter in payload["chapters"]] == [(None, None)]
    assert cover.status_code in {200, 204}


def test_each_unreadable_value_is_logged_once(library, caplog) -> None:
    client, ids = library

    with caplog.at_level(logging.WARNING, logger="modules.storage.sqlite_repo"):
        for _ in range(3):
            client.get("/api/library")
            client.get(f"/api/library/{ids[1]}")

    messages = [record.getMessage() for record in caplog.records if "unreadable" in record.getMessage()]
    assert len(messages) == len(set(messages)) == 5
    assert sum(f"books row {ids[1]} " in message for message in messages) == 3
    assert sum("chapters row" in message for message in messages) == 2


def test_restore_validation_still_rejects_unreadable_timestamps(controller) -> None:
    book = controller.repository.create_book(
        BookCreate(title="Broken", source_path="b.txt", source_type=SourceType.TXT)
    )
    with closing(sqlite3.connect(controller.config.db_path)) as conn, conn:
        conn.execute("UPDATE books SET created_at = 'last tuesday' WHERE id = ?", (book.id,))
    row = controller.repository._conn.execute("SELECT * FROM books WHERE id = ?", (book.id,)).fetchone()

    with pytest.raises(ValueError):
        controller.repository._row_to_book(row, strict=True)
