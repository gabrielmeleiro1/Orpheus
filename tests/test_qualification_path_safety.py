"""Qualification mutations remain confined on every supported platform."""

import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from modules.storage.sqlite_repo import SQLiteRepository
from tools.release_qualification import fixtures


def _symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as error:
        pytest.skip(f"Symlink creation is unavailable: {error}")


def test_corrupt_scenario_refuses_database_link_even_within_evidence(tmp_path):
    root = fixtures.FixtureRoot.create(SimpleNamespace(path=tmp_path, release_id="paths"))
    pristine = root.create_runtime("pristine")
    SQLiteRepository(pristine.db_path).close()
    original = pristine.db_path.read_bytes()
    source = root.create_runtime("linked")
    _symlink(source.db_path, pristine.db_path)

    with pytest.raises(ValueError, match="refuse"):
        fixtures.create_scenario_copy(
            source, root.scenarios_root, "corrupt-db", expected_qualification_root=tmp_path,
            release_id="paths", backups_root=root.backups_root,
        )

    assert pristine.db_path.read_bytes() == original
    assert not (root.scenarios_root / "corrupt-db").exists()


def test_permission_helpers_work_without_nofollow_chmod_and_leave_links_untouched(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    regular = runtime / "regular.txt"
    regular.write_text("fixture")
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched")
    _symlink(runtime / "link.txt", outside)
    outside_mode = outside.stat().st_mode
    chmod = Path.chmod
    calls = []

    def windows_chmod(path, mode, **kwargs):
        assert kwargs.get("follow_symlinks", True), "Windows 3.12 cannot accept nofollow"
        calls.append(path)
        return chmod(path, mode)

    monkeypatch.setattr(os, "supports_follow_symlinks", set())
    monkeypatch.setattr(Path, "chmod", windows_chmod)
    try:
        fixtures._make_tree_read_only(runtime)
        assert not regular.stat().st_mode & stat.S_IWUSR
    finally:
        fixtures._make_tree_writable(runtime)
    assert regular.stat().st_mode & stat.S_IWUSR
    assert calls and outside not in calls and runtime / "link.txt" not in calls
    assert outside.stat().st_mode == outside_mode
    assert outside.read_text() == "untouched"
