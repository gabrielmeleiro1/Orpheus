"""The script-mode cleaner gate must propagate pytest failures to its caller."""

from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("pytest_exit", [0, 1, 2, 5])
def test_cleaner_script_propagates_pytest_exit(pytest_exit):
    root = Path(__file__).resolve().parents[1]
    code = (
        "import pytest, runpy; "
        f"pytest.main=lambda *args, **kwargs: {pytest_exit}; "
        "runpy.run_path('tests/test_cleaner_text_preservation.py', run_name='__main__')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=root,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == pytest_exit, result.stderr
