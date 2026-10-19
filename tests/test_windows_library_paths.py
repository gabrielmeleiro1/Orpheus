"""Library storage on a real Windows filesystem: names, long paths, locks, junctions."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from modules.app.config import AppConfig
from modules.app.controller import AppController
from modules.contracts.book_identity import sanitize_book_title
from modules.storage.models import BookCreate, ChapterCreate, SourceType

pytestmark = pytest.mark.skipif(os.name != "nt", reason="exercises Windows filesystem semantics")


def _controller(root: Path) -> AppController:
    return AppController(AppConfig(
        data_dir=root / "data", output_dir=root / "output", temp_dir=root / "temp",
        db_path=root / "data" / "library.db", worker_pool_reuse_enabled=False,
    ))


def _book_with_files(controller: AppController, title: str, chapters: int = 2):
    folder = controller.config.output_dir / sanitize_book_title(title)
    folder.mkdir(parents=True)
    paths = [folder / f"{number:02d}_Chapter.mp3" for number in range(1, chapters + 1)]
    for path in paths:
        path.write_bytes(b"chapter audio")
    (folder / "book.m4b").write_bytes(b"packaged")
    book, _ = controller.repository.create_book_with_chapters(
        BookCreate(title=title, source_path=str(folder / "missing-source.txt"), source_type=SourceType.TXT,
                   total_chapters=chapters, playback_path=str(folder / "book.m4b")),
        [ChapterCreate(book_id=0, chapter_number=index, title=str(index), mp3_path=str(path))
         for index, path in enumerate(paths, start=1)],
    )
    return book, folder


@pytest.fixture
def controller(tmp_path):
    service = _controller(tmp_path)
    yield service
    service.repository.close()


@pytest.mark.parametrize("title", [
    "Trailing dot.", "Trailing space ", "Dots...  ", "CON", "con.txt", "NUL", "COM1", "aux.epub",
    "COM¹", "LPT³", "CONIN$", "Café à Paris", "日本語の本", "x" * 400,
])
def test_awkward_titles_get_a_real_directory_that_deletes_cleanly(controller, title) -> None:
    book, folder = _book_with_files(controller, title)
    name = folder.name

    assert name == name.rstrip(" ."), "Windows would silently drop the trailing character"
    assert name in os.listdir(controller.config.output_dir)
    assert controller.get_book_playback_file(book.id) == folder / "book.m4b"

    result = controller.delete_book_with_identity(book.id)

    assert result["deleted"] is True and "files_pending" not in result
    assert not folder.exists()


@pytest.mark.parametrize("title", ["COM¹", "com²", "LPT³.epub"])
def test_superscript_device_names_are_escaped(title) -> None:
    # Windows 10 reserves COM¹-³ and LPT¹-³ like COM1; Windows 11 relaxed
    # device names inside full paths, so this machine cannot show the failure.
    assert sanitize_book_title(title).startswith("_")


def test_book_beyond_max_path_is_played_and_deleted(tmp_path) -> None:
    deep = tmp_path
    while len(str(deep)) < 200:
        deep = deep / ("nested-directory-" + "d" * 20)
    service = _controller(deep)
    try:
        book, folder = _book_with_files(service, "A long title " + "y" * 100, chapters=3)
        longest = max(len(str(path)) for path in folder.iterdir())
        assert longest > 260

        assert service.get_book_playback_file(book.id, chapter_number=3) is not None
        result = service.delete_book_with_identity(book.id)
        assert result["deleted"] is True and "files_pending" not in result
        assert not folder.exists()
    finally:
        service.repository.close()


def test_deleting_a_book_whose_chapter_is_open_reports_in_use_and_keeps_everything(controller) -> None:
    book, folder = _book_with_files(controller, "Playing")
    before = {path.name: path.read_bytes() for path in folder.iterdir()}

    with (folder / "01_Chapter.mp3").open("rb"):
        with pytest.raises(RuntimeError, match="in use"):
            controller.delete_book_with_identity(book.id)

    assert controller.repository.get_book(book.id) is not None
    assert {path.name: path.read_bytes() for path in folder.iterdir()} == before
    staging = controller.config.temp_dir / "delete_staging"
    assert not staging.exists() or not any(staging.iterdir())
    # Once playback stops the same deletion succeeds.
    assert controller.delete_book_with_identity(book.id)["deleted"] is True
    assert not folder.exists()


def _junction(link: Path, target: Path) -> None:
    import _winapi

    _winapi.CreateJunction(str(target), str(link))
    assert link.is_junction()


def test_book_deletion_does_not_follow_a_junction_out_of_the_library(controller, tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep me")
    book, folder = _book_with_files(controller, "Linked")
    _junction(folder / "extras", outside)

    assert controller.delete_book_with_identity(book.id)["deleted"] is True

    assert not folder.exists()
    assert (outside / "precious.txt").read_text() == "keep me"


def test_storage_cleanup_does_not_follow_an_orphan_junction(controller, tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep me")
    link = controller.config.output_dir / "Orphan"
    _junction(link, outside)
    old = time.time() - 3 * 24 * 60 * 60
    for path in (outside / "precious.txt", outside):
        os.utime(path, (old, old))

    controller.cleanup_storage(confirm="cleanup")

    # The orphan link itself is removed; its target is untouched.
    assert not os.path.lexists(link)
    assert (outside / "precious.txt").read_text() == "keep me"
