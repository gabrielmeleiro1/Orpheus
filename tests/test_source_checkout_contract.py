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
