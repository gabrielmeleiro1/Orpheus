"""Contract tests for the pre-push validation hook."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_documented_project_venv_is_ignored() -> None:
    ignore_rules = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert ".venv/" in ignore_rules


@pytest.mark.skipif(shutil.which("git") is None or shutil.which("bash") is None, reason="needs git and bash")
def test_hook_installer_works_in_a_linked_worktree(tmp_path) -> None:
    # Hooks export GIT_DIR and friends; never let them aim git at this checkout.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

    def git(*args: str, cwd: Path) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
        ).stdout.strip()

    main = tmp_path / "main"
    (main / "scripts").mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / "scripts/install-git-hooks.sh", main / "scripts")
    git("init", "-q", cwd=main)
    git("add", "-A", cwd=main)
    git("-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-qm", "init", cwd=main)
    worktree = tmp_path / "worktree"
    git("worktree", "add", "-q", str(worktree), "-b", "feature", cwd=main)

    installed = subprocess.run(
        ["bash", "scripts/install-git-hooks.sh"],
        cwd=worktree, env=env, capture_output=True, text=True,
    )

    assert installed.returncode == 0, installed.stderr
    assert git("config", "core.hooksPath", cwd=main) == "scripts/hooks"


def test_tracked_hook_is_executable() -> None:
    mode = subprocess.run(
        ["git", "ls-files", "-s", "scripts/hooks/pre-push"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()[0]
    assert mode == "100755"
