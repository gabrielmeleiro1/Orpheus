"""Contract tests for the pre-push validation hook."""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_documented_project_venv_is_ignored() -> None:
    ignore_rules = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert ".venv/" in ignore_rules
