"""Tests for installing the packaged macOS app by replacement."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.install_desktop_app import install_app_bundle


def _create_app_bundle(path: Path, marker: str) -> None:
    contents_dir = path / "Contents"
    contents_dir.mkdir(parents=True)
    (contents_dir / "Info.plist").write_text(marker, encoding="utf-8")


def test_install_app_bundle_replaces_existing_app_in_applications_dir(tmp_path: Path) -> None:
    source_app = tmp_path / "build" / "Orpheus.app"
    applications_dir = tmp_path / "Applications"
    existing_app = applications_dir / "Orpheus.app"
    stale_file = existing_app / "Contents" / "stale.txt"

    _create_app_bundle(source_app, "new")
    _create_app_bundle(existing_app, "old")
    stale_file.write_text("remove me", encoding="utf-8")

    installed_app = install_app_bundle(source_app, applications_dir=applications_dir)

    assert installed_app == existing_app
    assert (installed_app / "Contents" / "Info.plist").read_text(encoding="utf-8") == "new"
    assert not stale_file.exists()
    assert [path.name for path in applications_dir.glob("Orpheus*.app")] == [
        "Orpheus.app"
    ]


def test_install_app_bundle_can_remove_source_after_install_to_avoid_spotlight_duplicates(
    tmp_path: Path,
) -> None:
    source_app = tmp_path / "build" / "Orpheus.app"
    applications_dir = tmp_path / "Applications"

    _create_app_bundle(source_app, "new")

    installed_app = install_app_bundle(
        source_app,
        applications_dir=applications_dir,
        remove_source_after_install=True,
    )

    assert installed_app == applications_dir / "Orpheus.app"
    assert installed_app.is_dir()
    assert not source_app.exists()


def test_install_app_bundle_preserves_existing_app_when_final_swap_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_app = tmp_path / "build" / "Orpheus.app"
    applications_dir = tmp_path / "Applications"
    existing_app = applications_dir / "Orpheus.app"

    _create_app_bundle(source_app, "new")
    _create_app_bundle(existing_app, "old")

    original_replace = Path.replace

    def fail_installing_swap(path: Path, target: Path) -> Path:
        if path.name == ".Orpheus.installing.app":
            raise OSError("simulated final install swap failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_installing_swap)

    with pytest.raises(OSError, match="simulated final install swap failure"):
        install_app_bundle(source_app, applications_dir=applications_dir)

    assert existing_app.is_dir()
    assert (existing_app / "Contents" / "Info.plist").read_text(
        encoding="utf-8"
    ) == "old"


def test_install_app_bundle_removes_partial_staging_bundle_when_copy_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_app = tmp_path / "build" / "Orpheus.app"
    applications_dir = tmp_path / "Applications"
    existing_app = applications_dir / "Orpheus.app"
    staging_app = applications_dir / ".Orpheus.installing.app"

    _create_app_bundle(source_app, "new")
    _create_app_bundle(existing_app, "old")

    def fail_partial_copy(_source: Path, target: Path, **_kwargs: object) -> None:
        _create_app_bundle(target, "partial")
        raise OSError("simulated copy failure")

    monkeypatch.setattr("scripts.install_desktop_app.shutil.copytree", fail_partial_copy)

    with pytest.raises(OSError, match="simulated copy failure"):
        install_app_bundle(source_app, applications_dir=applications_dir)

    assert not staging_app.exists()
    assert (existing_app / "Contents" / "Info.plist").read_text(
        encoding="utf-8"
    ) == "old"
