"""Contract tests for the pre-push validation hook."""

from __future__ import annotations

import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRE_PUSH_HOOK = PROJECT_ROOT / "scripts" / "hooks" / "pre-push"


def test_pre_push_hook_has_valid_bash_syntax() -> None:
    completed = subprocess.run(
        ["bash", "-n", str(PRE_PUSH_HOOK)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_pre_push_hook_prefers_project_venv_then_active_python_312() -> None:
    script = PRE_PUSH_HOOK.read_text(encoding="utf-8")

    assert "python_supports_validation()" in script
    assert 'PYTHON="$ROOT/venv/bin/python"' in script
    assert 'elif command -v python >/dev/null 2>&1 && python_supports_validation "$(command -v python)"; then' in script
    assert 'PYTHON="$(command -v python)"' in script
    assert 'elif command -v python3.12 >/dev/null 2>&1; then' in script
    assert 'PYTHON="$(command -v python3.12)"' in script
    assert "Python 3.12 is required for local validation" in script
    assert "run_with_timeout 120 \"$PYTHON\" -m ruff check ." in script
    assert "run_with_timeout 600 \"$PYTHON\" -m pytest tests" in script
    assert "run_with_timeout 120 python -m ruff check ." not in script
    assert "run_with_timeout 600 python -m pytest tests" not in script
