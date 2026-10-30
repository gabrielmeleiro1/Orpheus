"""Tests for the local release validation matrix command specs."""

from __future__ import annotations

from pathlib import Path

from tools.release_qualification.commands import CommandSpec
from tools.release_qualification.matrix import (
    FOCUSED_DESKTOP_WEB_TESTS,
    ValidationMatrix,
    build_validation_matrix,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = str(REPO_ROOT / ".venv" / "bin" / "python")


def test_focused_desktop_web_tests_exist():
    assert FOCUSED_DESKTOP_WEB_TESTS
    for path in FOCUSED_DESKTOP_WEB_TESTS:
        assert (REPO_ROOT / path).is_file(), path


def test_validation_matrix_creates_exact_argv_and_cwd_specs():
    matrix = build_validation_matrix(repo_root=REPO_ROOT)
    assert isinstance(matrix, ValidationMatrix)
    by_label = {spec.label: spec for spec in matrix.commands}

    ruff = by_label["ruff-check"]
    assert ruff.argv == (VENV_PYTHON, "-m", "ruff", "check", ".")
    assert ruff.cwd == str(REPO_ROOT)
    assert ruff.shell is False

    contract = by_label["python-contract"]
    assert contract.argv == (VENV_PYTHON, "run_tests.py", "--contract")
    assert contract.cwd == str(REPO_ROOT)

    profiles = by_label["verify-profiles"]
    assert profiles.argv == (
        VENV_PYTHON,
        "run_tests.py",
        "--verify-profile",
        "base",
        "--verify-profile",
        "mlx",
    )
    assert profiles.cwd == str(REPO_ROOT)

    focused = by_label["focused-desktop-web"]
    assert focused.argv == (
        VENV_PYTHON,
        "-m",
        "pytest",
        "--no-cov",
        *FOCUSED_DESKTOP_WEB_TESTS,
        "-q",
        "-o",
        "addopts=",
    )
    assert focused.cwd == str(REPO_ROOT)

    desktop = str(REPO_ROOT / "desktop")
    assert by_label["npm-ci"].argv == ("npm", "ci")
    assert by_label["npm-ci"].cwd == desktop
    assert by_label["npm-typecheck"].argv == ("npm", "run", "typecheck")
    assert by_label["npm-typecheck"].cwd == desktop
    assert by_label["npm-test"].argv == ("npm", "test", "--", "--run")
    assert by_label["npm-test"].cwd == desktop
    assert by_label["npm-build"].argv == ("npm", "run", "build")
    assert by_label["npm-build"].cwd == desktop

    tauri = str(REPO_ROOT / "desktop" / "src-tauri")
    assert by_label["cargo-test"].argv == (
        "cargo",
        "test",
        "--locked",
        "--",
        "--test-threads=1",
    )
    assert by_label["cargo-test"].cwd == tauri
    assert by_label["cargo-check"].argv == ("cargo", "check", "--locked")
    assert by_label["cargo-check"].cwd == tauri


def test_matrix_commands_never_use_shell_true():
    matrix = build_validation_matrix(repo_root=REPO_ROOT)
    for spec in matrix.commands:
        assert isinstance(spec, CommandSpec)
        assert spec.shell is False
        assert isinstance(spec.argv, tuple)
        assert all(isinstance(part, str) for part in spec.argv)


def test_failed_matrix_command_fails_decision():
    matrix = build_validation_matrix(repo_root=REPO_ROOT)
    results = [
        ("ruff-check", 0),
        ("python-contract", 1),
        ("verify-profiles", 0),
    ]
    decision = matrix.evaluate_results(results)
    assert decision.ok is False
    assert decision.failed_labels == ("python-contract",)

    all_pass = [(spec.label, 0) for spec in matrix.commands]
    passed = matrix.evaluate_results(all_pass)
    assert passed.ok is True
    assert passed.failed_labels == ()
