#!/usr/bin/env python3
"""Install the built macOS desktop app by replacing the existing app bundle."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_APP_BUNDLE = (
    PROJECT_ROOT
    / "desktop"
    / "src-tauri"
    / "target"
    / "release"
    / "bundle"
    / "macos"
    / "Orpheus.app"
)
DEFAULT_APPLICATIONS_DIR = Path("/Applications")


def install_app_bundle(
    source_app: Path = DEFAULT_APP_BUNDLE,
    *,
    applications_dir: Path = DEFAULT_APPLICATIONS_DIR,
    remove_source_after_install: bool = False,
) -> Path:
    source_app = Path(source_app).expanduser()
    applications_dir = Path(applications_dir).expanduser()

    if not source_app.is_dir():
        raise FileNotFoundError(f"built app bundle not found: {source_app}")
    if source_app.suffix != ".app":
        raise ValueError(f"source must be a .app bundle: {source_app}")

    applications_dir.mkdir(parents=True, exist_ok=True)
    target_app = applications_dir / source_app.name

    if source_app.resolve() == target_app.resolve():
        return target_app

    temp_app = applications_dir / f".{source_app.stem}.installing.app"
    if temp_app.exists():
        shutil.rmtree(temp_app)

    previous_app: Path | None = None
    try:
        shutil.copytree(source_app, temp_app, symlinks=True)
        if target_app.exists():
            previous_app = applications_dir / (
                f".{source_app.stem}.previous-{uuid4().hex}.app"
            )
            target_app.replace(previous_app)
        try:
            temp_app.replace(target_app)
        except BaseException:
            if previous_app is not None and previous_app.exists() and not target_app.exists():
                previous_app.replace(target_app)
            raise
        if previous_app is not None:
            shutil.rmtree(previous_app)
    finally:
        if temp_app.exists():
            shutil.rmtree(temp_app)
    if remove_source_after_install:
        shutil.rmtree(source_app)
    return target_app


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replace /Applications/Orpheus.app with the latest built app bundle.",
    )
    parser.add_argument(
        "--remove-source-after-install",
        action="store_true",
        help="Remove the source .app after replacement to avoid duplicate Spotlight results.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if sys.platform != "darwin":
        print(
            "install_desktop_app.py replaces the macOS app bundle in /Applications. "
            "On Windows, build the Inno Setup installer instead "
            "(docs/RUN_APPLICATION.md, 'Windows installer').",
            file=sys.stderr,
        )
        return 2
    installed_app = install_app_bundle(
        remove_source_after_install=args.remove_source_after_install,
    )
    print(f"Installed {installed_app}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
