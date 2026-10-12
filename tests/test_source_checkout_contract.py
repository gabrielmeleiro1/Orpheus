import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_source_setup_is_documented() -> None:
    dependencies = (ROOT / "docs" / "DEPENDENCIES.md").read_text(encoding="utf-8")
    run_guide = (ROOT / "docs" / "RUN_APPLICATION.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    required_commands = (
        "py -3.12 -m venv .venv",
        r".\.venv\Scripts\python.exe scripts/setup_runtime.py --profile base --dry-run",
        r".\.venv\Scripts\python.exe scripts/setup_runtime.py --profile base",
        r".\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt",
        r".\.venv\Scripts\python.exe run_tests.py --contract",
        "npm.cmd ci",
        "npm.cmd run desktop:dev",
    )
    for command in required_commands:
        assert command in dependencies

    assert "PowerShell" in dependencies
    assert "Windows" in run_guide
    assert "Windows" in readme
    assert "model weights are optional" in dependencies.lower()


def test_tauri_macos_minimum_system_version_is_14() -> None:
    """Release target is macOS 14+; Tauri defaults to 10.13 unless overridden."""
    import json

    conf_path = ROOT / "desktop" / "src-tauri" / "tauri.conf.json"
    payload = json.loads(conf_path.read_text(encoding="utf-8"))
    mac = payload.get("bundle", {}).get("macOS", {})
    assert mac.get("minimumSystemVersion") == "14.0"


def test_tauri_macos_signing_identity_is_adhoc_dash() -> None:
    """Outer ad-hoc seal (-) so Gatekeeper sees a complete unsigned bundle seal."""
    import json

    conf_path = ROOT / "desktop" / "src-tauri" / "tauri.conf.json"
    payload = json.loads(conf_path.read_text(encoding="utf-8"))
    mac = payload.get("bundle", {}).get("macOS", {})
    assert mac.get("signingIdentity") == "-"


def test_ci_validates_sources_without_producing_releases() -> None:
    workflow = ROOT / ".github" / "workflows" / "source-validation.yml"
    assert workflow.is_file()
    contents = workflow.read_text(encoding="utf-8").lower()

    required_entries = (
        "macos-15",
        "windows-latest",
        "python run_tests.py --contract",
        "python -m ruff check .",
        "npm run typecheck",
        "npm test -- --run",
        "cargo test --locked -- --test-threads=1",
        "cargo check --locked",
    )
    for entry in required_entries:
        assert entry in contents

    prohibited_entries = (
        "desktop:build",
        "desktop:release",
        "build_desktop_backend.py",
        "upload-artifact",
        "codesign",
        "notar",
        "installer",
        "release-action",
    )
    for entry in prohibited_entries:
        assert entry not in contents


def test_common_local_build_outputs_are_ignored() -> None:
    generated_paths = (
        ".tox/py312/marker",
        ".nox/tests/marker",
        ".eggs/cache/marker",
        "orpheus.egg-info/PKG-INFO",
        "build/local-output",
        "dist/local-output",
    )
    for path in generated_paths:
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", path],
            cwd=ROOT,
            check=False,
        )
        assert result.returncode == 0, f"expected generated path to be ignored: {path}"
