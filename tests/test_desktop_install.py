"""Tests for installing the packaged macOS app by replacement."""

from __future__ import annotations

from pathlib import Path

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
