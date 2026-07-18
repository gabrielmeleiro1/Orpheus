from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_source_setup_is_documented() -> None:
    dependencies = (ROOT / "docs" / "DEPENDENCIES.md").read_text(encoding="utf-8")
    run_guide = (ROOT / "docs" / "RUN_APPLICATION.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    required_commands = (
        "py -3.12 -m venv .venv",
        r".\.venv\Scripts\Activate.ps1",
        "python scripts/setup_runtime.py --profile base --dry-run",
        "python scripts/setup_runtime.py --profile base",
        "python -m pip install -r requirements-dev.txt",
        "python run_tests.py --contract",
        "npm ci",
        "npm run desktop:dev",
    )
    for command in required_commands:
        assert command in dependencies

    assert "PowerShell" in dependencies
    assert "Windows" in run_guide
    assert "Windows" in readme
    assert "model weights are optional" in dependencies.lower()


def test_ci_validates_sources_without_producing_releases() -> None:
    workflow = ROOT / ".github" / "workflows" / "source-validation.yml"
    assert workflow.is_file()
    contents = workflow.read_text(encoding="utf-8").lower()

    required_entries = (
        "macos-14",
        "windows-latest",
        "python run_tests.py --contract",
        "python -m ruff check .",
        "npm run typecheck",
        "npm test -- --run",
        "cargo test",
        "cargo check",
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
