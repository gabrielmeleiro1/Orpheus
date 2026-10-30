"""Static contract checks for packaged release artifact validation."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _workflow() -> tuple[dict, str]:
    path = ROOT / ".github/workflows/release-artifact-validation.yml"
    text = path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(text)
    return parsed, text


def test_release_workflow_has_manual_and_release_triggers() -> None:
    workflow, text = _workflow()
    triggers = workflow.get("on", workflow.get(True, {}))
    assert "workflow_dispatch" in triggers
    assert "push" in triggers
    assert "tags" in triggers["push"]
    assert "branches" in triggers["push"]


def test_release_workflow_builds_then_verifies_and_smokes_packaged_app() -> None:
    _parsed, text = _workflow()
    required = (
        "ORPHEUS_RUNTIME_PROFILE: base",
        "python run_tests.py --contract",
        "npm run desktop:build",
        "EXPECTED_ARCH",
        "Contents/Resources/binaries/orpheus-desktop-backend",
        'APP_EXECUTABLE="$(find "$APP_BUNDLE/Contents/MacOS"',
        'test -x "$APP_EXECUTABLE"',
        'file "$executable"',
        'FFMPEG="$APP_BUNDLE',
        'FFPROBE="$APP_BUNDLE',
        'LICENSE_FILE="$APP_BUNDLE/Contents/Resources/LICENSE"',
        'THIRD_PARTY_LICENSES="$APP_BUNDLE/Contents/Resources/THIRD_PARTY_LICENSES.md"',
        'RUNTIME_PROFILE_MARKER="$APP_BUNDLE/Contents/Resources/binaries/orpheus-desktop-backend/runtime-profile.json"',
        'assert payload == {"profile": "base"}',
        "--app-bundle",
        "--backend sidecar",
        "--check-ffmpeg-runtime",
        "release-artifact-smoke.log",
        "upload-artifact@v7",
    )
    for entry in required:
        assert entry in text
    assert text.index("npm run desktop:build") < text.index("EXPECTED_ARCH")
    assert text.index("EXPECTED_ARCH") < text.index("desktop_runtime_smoke.py")


def test_release_workflow_verifies_base_profile_in_a_clean_environment() -> None:
    _parsed, text = _workflow()

    assert "python run_tests.py --verify-profile base" in text
    assert text.index("python run_tests.py --verify-profile base") < text.index(
        "npm run desktop:build"
    )


def test_source_validation_keeps_windows_rust_job() -> None:
    text = (ROOT / ".github/workflows/source-validation.yml").read_text(encoding="utf-8")
    assert "windows-latest" in text
    assert "cargo test --locked -- --test-threads=1" in text
    assert "cargo check --locked" in text
