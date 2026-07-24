"""Path-safety tests for per-book output workspace allocation."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from modules.pipeline.book_workspace import (
    UNTITLED_BOOK_DIR,
    prepare_book_workspace,
    sanitize_book_title,
)


def test_prepare_book_workspace_allocates_unique_dir_for_duplicate_titles(tmp_path):
    output_root = tmp_path / "output"

    first = prepare_book_workspace(output_root, "Shared Title")
    (first.chapters_dir / "01.mp3").write_bytes(b"first")

    second = prepare_book_workspace(output_root, "Shared Title")
    (second.chapters_dir / "01.mp3").write_bytes(b"second")

    assert first.output_dir != second.output_dir
    assert first.output_dir == output_root / "Shared Title"
    assert second.output_dir == output_root / "Shared Title (2)"
    # The first book's artifacts are untouched by the second allocation.
    assert (first.chapters_dir / "01.mp3").read_bytes() == b"first"
    assert (second.chapters_dir / "01.mp3").read_bytes() == b"second"


def test_prepare_book_workspace_keeps_allocating_fresh_dirs_on_repeated_runs(tmp_path):
    output_root = tmp_path / "output"

    allocated: list[Path] = []
    for _ in range(5):
        workspace = prepare_book_workspace(output_root, "Repeated Book")
        marker = workspace.output_dir / "marker.txt"
        marker.write_text(str(workspace.output_dir), encoding="utf-8")
        allocated.append(workspace.output_dir)

    assert len(set(allocated)) == 5
    # Every run's marker still reflects its own directory (no overwrites).
    for output_dir in allocated:
        assert (output_dir / "marker.txt").read_text(encoding="utf-8") == str(output_dir)


def test_prepare_book_workspace_contains_titles_with_traversal_segments(tmp_path):
    output_root = tmp_path / "output"
    output_root.mkdir(parents=True, exist_ok=True)

    workspace = prepare_book_workspace(output_root, "../../etc/passwd")

    resolved = workspace.output_dir.resolve()
    assert resolved.parent == output_root.resolve()
    assert resolved.relative_to(output_root.resolve())


def test_sanitize_book_title_strips_separators_and_falls_back(tmp_path):
    assert "/" not in sanitize_book_title("a/b/c")
    assert "\\" not in sanitize_book_title("a\\b")
    assert ".." not in sanitize_book_title("..")
    assert sanitize_book_title("") == UNTITLED_BOOK_DIR
    assert sanitize_book_title("   ") == UNTITLED_BOOK_DIR


def test_sanitize_book_title_rejects_windows_reserved_and_trimmed_names():
    for title in ("CON", "nul", "COM1", "LPT9", "CON.txt"):
        sanitized = sanitize_book_title(title)
        assert sanitized.casefold() not in {"con", "nul", "com1", "lpt9"}
        assert sanitized.rstrip(" .") == sanitized

    assert sanitize_book_title("Book title... ") == "Book title"
    assert sanitize_book_title("...") == UNTITLED_BOOK_DIR


def test_prepare_book_workspace_keeps_reserved_title_artifacts_isolated(tmp_path):
    output_root = tmp_path / "output"

    reserved = prepare_book_workspace(output_root, "CON")
    normal = prepare_book_workspace(output_root, "CON_")

    assert reserved.output_dir != normal.output_dir
    assert reserved.output_dir.parent == output_root
    assert normal.output_dir.parent == output_root
