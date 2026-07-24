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
    assert workflow["name"] == "Release artifact validation"


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
        "--app-bundle",
        "--backend sidecar",
        "--check-ffmpeg-runtime",
        "release-artifact-smoke.log",
        "upload-artifact@v4",
    )
    for entry in required:
        assert entry in text
    assert text.index("npm run desktop:build") < text.index("EXPECTED_ARCH")
    assert text.index("EXPECTED_ARCH") < text.index("desktop_runtime_smoke.py")
    assert "Contents/MacOS/Orpheus" not in text


def test_source_validation_keeps_windows_rust_job() -> None:
    text = (ROOT / ".github/workflows/source-validation.yml").read_text(encoding="utf-8")
    assert "windows-latest" in text
    assert "cargo test --locked -- --test-threads=1" in text
    assert "cargo check --locked" in text
