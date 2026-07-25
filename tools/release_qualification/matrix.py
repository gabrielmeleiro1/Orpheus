"""Declarative local validation matrix for release qualification."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .commands import CommandSpec

FOCUSED_DESKTOP_WEB_TESTS: tuple[str, ...] = (
    "tests/test_web_api.py",
    "tests/test_web_events.py",
    "tests/test_web_desktop_auth.py",
    "tests/test_desktop_backend_launcher.py",
    "tests/test_desktop_runtime_contract.py",
)


@dataclass(frozen=True)
class MatrixDecision:
    ok: bool
    blocks_build_qualification: bool
    failed_labels: tuple[str, ...]


@dataclass(frozen=True)
class ValidationMatrix:
    commands: tuple[CommandSpec, ...]

    def evaluate_results(self, results: Sequence[tuple[str, int]]) -> MatrixDecision:
        known = {spec.label for spec in self.commands}
        failed = tuple(
            label for label, code in results if label in known and int(code) != 0
        )
        blocked = bool(failed)
        return MatrixDecision(
            ok=not blocked,
            blocks_build_qualification=blocked,
            failed_labels=failed,
        )


def build_validation_matrix(*, repo_root: Path) -> ValidationMatrix:
    root = Path(repo_root)
    python = str(root / ".venv" / "bin" / "python")
    desktop = str(root / "desktop")
    tauri = str(root / "desktop" / "src-tauri")
    commands = (
        CommandSpec(
            label="ruff-check",
            argv=(python, "-m", "ruff", "check", "."),
            cwd=str(root),
        ),
        CommandSpec(
            label="python-contract",
            argv=(python, "run_tests.py", "--contract"),
            cwd=str(root),
        ),
        CommandSpec(
            label="verify-profiles",
            argv=(
                python,
                "run_tests.py",
                "--verify-profile",
                "base",
                "--verify-profile",
                "mlx",
            ),
            cwd=str(root),
        ),
        CommandSpec(
            label="focused-desktop-web",
            argv=(
                python,
                "-m",
                "pytest",
                "--no-cov",
                *FOCUSED_DESKTOP_WEB_TESTS,
                "-q",
                "-o",
                "addopts=",
            ),
            cwd=str(root),
        ),
        CommandSpec(
            label="npm-ci",
            argv=("npm", "ci"),
            cwd=desktop,
        ),
        CommandSpec(
            label="npm-typecheck",
            argv=("npm", "run", "typecheck"),
            cwd=desktop,
        ),
        CommandSpec(
            label="npm-test",
            argv=("npm", "test", "--", "--run"),
            cwd=desktop,
        ),
        CommandSpec(
            label="npm-build",
            argv=("npm", "run", "build"),
            cwd=desktop,
        ),
        CommandSpec(
            label="cargo-test",
            argv=("cargo", "test", "--locked", "--", "--test-threads=1"),
            cwd=tauri,
        ),
        CommandSpec(
            label="cargo-check",
            argv=("cargo", "check", "--locked"),
            cwd=tauri,
        ),
        CommandSpec(
            label="bench-quick",
            argv=(python, "-m", "orpheus", "bench", "quick"),
            cwd=str(root),
        ),
    )
    return ValidationMatrix(commands=commands)
