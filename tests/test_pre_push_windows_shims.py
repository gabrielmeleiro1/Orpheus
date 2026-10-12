"""Exercise the real Python timeout wrapper with an inert Windows CMD shim."""

import os
from pathlib import Path
import re
import subprocess
import sys

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows CMD command resolution")
@pytest.mark.parametrize("explicit_suffix", [False, True])
def test_hook_timeout_wrapper_resolves_cmd_shims(tmp_path, explicit_suffix):
    root = Path(__file__).resolve().parents[1]
    hook = (root / "scripts/hooks/pre-push").read_text(encoding="utf-8")
    body = re.search(r"run_with_timeout\(\).*?<<'PY'\n(.*?)\nPY", hook, re.S).group(1)
    shim = tmp_path / "audit-hook-shim.cmd"
    shim.write_text("@echo off\necho audit-shim-reached\nexit /b 37\n", encoding="ascii")
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    name = "audit-hook-shim.cmd" if explicit_suffix else "audit-hook-shim"
    completed = subprocess.run(
        [sys.executable, "-", "10", name], input=body, text=True,
        capture_output=True, cwd=tmp_path, env=env, timeout=15,
    )
    assert completed.returncode == 37, completed.stdout + completed.stderr
    assert "audit-shim-reached" in completed.stdout
