"""Run a copied hook with inert tools; never run Git push or real validation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


HOOK = Path(__file__).resolve().parents[1] / "scripts/hooks/pre-push"


def _bash() -> str:
    candidates = [shutil.which("bash")]
    if os.name == "nt":
        candidates.insert(0, str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/bin/bash.exe"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    pytest.skip("Bash is required to execute the copied pre-push hook")


def _shell_path(path: Path) -> str:
    value = path.as_posix()
    return f"/{value[0].lower()}{value[2:]}" if path.drive else value


def _executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _run_copied_hook(tmp_path: Path, layout: str, *, fail_npm: bool = False):
    root = tmp_path / "checkout with spaces"
    root.mkdir()
    (root / "desktop/src-tauri").mkdir(parents=True)
    hook = root / "pre-push"
    hook.write_bytes(HOOK.read_bytes())
    tools = root / "fake-tools"
    # No real Git is reachable. Returning failure selects the temporary cwd.
    _executable(tools / "git", "exit 1\n")
    _executable(tools / "ffmpeg", "exit 0\n")
    # A global Python must never replace the checkout-owned interpreter.
    _executable(tools / "python", "exit 93\n")
    _executable(tools / "python3.12", "exit 94\n")
    interpreter = root / ".venv" / layout
    _executable(interpreter, '''
printf '%s\\t%s\\t%s\\t%s\\n' "$0" "$*" "${CC-}" "${AR-}" >> "$AUDIT_HOOK_LOG"
cat >/dev/null
if [[ "${AUDIT_FAIL_NPM:-0}" == 1 && "$3 $4" == "npm ci" ]]; then
  exit 37
fi
exit 0
''')
    log = root / "commands.log"
    env = dict(os.environ)
    env.update(AUDIT_HOOK_LOG=_shell_path(log), AUDIT_FAIL_NPM=str(int(fail_npm)), CC="audit-cc", AR="audit-ar")
    # PATH is confined inside Bash, so no installed npm/cargo/python executes.
    completed = subprocess.run(
        [_bash(), "--noprofile", "--norc", "-c",
         'export PATH="$1:/usr/bin:/bin"; exec bash "$2" audit audit://local',
         "audit-hook", _shell_path(tools), _shell_path(hook)],
        cwd=root, env=env, capture_output=True, text=True, timeout=30,
    )
    calls = [line.split("\t") for line in log.read_text().splitlines()] if log.exists() else []
    return completed, calls


@pytest.mark.parametrize("layout", ["Scripts/python.exe", "bin/python"])
def test_hook_runs_all_gates_with_checkout_python_and_preserves_compiler_selection(tmp_path, layout):
    completed, calls = _run_copied_hook(tmp_path, layout)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert len(calls) == 2, json.dumps(calls)
    assert all(call[0].endswith("/.venv/" + layout) for call in calls)
    assert calls[0][1] == "- 180 npm ci"
    # Every other gate comes from check_desktop.py, the command list CI shares.
    assert calls[1][1].endswith(
        "scripts/check_desktop.py --only lint python frontend rust"
    )
    assert all(call[2:] == ["audit-cc", "audit-ar"] for call in calls)
    assert "PRE-PUSH VALIDATION PASSED" in completed.stdout


def test_hook_propagates_a_failed_gate_without_running_later_commands(tmp_path):
    completed, calls = _run_copied_hook(tmp_path, "Scripts/python.exe", fail_npm=True)
    assert completed.returncode == 37, completed.stdout + completed.stderr
    assert len(calls) == 1, json.dumps(calls)
    assert calls[-1][1] == "- 180 npm ci"
    assert "PRE-PUSH VALIDATION PASSED" not in completed.stdout
