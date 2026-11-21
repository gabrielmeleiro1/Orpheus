"""Regression tests for the repo-level test runner contract."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

import run_tests


_CONFTEST_SPEC = importlib.util.spec_from_file_location(
    "project_conftest",
    Path(__file__).with_name("conftest.py"),
)
assert _CONFTEST_SPEC and _CONFTEST_SPEC.loader
conftest = importlib.util.module_from_spec(_CONFTEST_SPEC)
_CONFTEST_SPEC.loader.exec_module(conftest)


def test_parse_args_defaults_to_smoke_mode() -> None:
    args = run_tests.parse_args([])
    assert args.contract is False
    assert args.verify_profile is None


def test_build_contract_command_uses_repo_pytest_contract() -> None:
    command = run_tests.build_contract_command()

    assert command[:4] == [sys.executable, "-m", "pytest", "tests"]
    assert command[-2:] == ["-m", run_tests.CORE_MARKER_EXPRESSION]
    assert "-o" not in command
    assert "addopts=" not in " ".join(command)


def _run_coverage_threshold_hook(markexpr: str, args: list[str]) -> int:
    cov_plugin = SimpleNamespace(options=SimpleNamespace(cov_fail_under=85))
    config = SimpleNamespace(
        args=args,
        option=SimpleNamespace(markexpr=markexpr),
        pluginmanager=SimpleNamespace(
            getplugin=lambda name: cov_plugin if name == "_cov" else None
        ),
    )
    hook = conftest.pytest_runtestloop(SimpleNamespace(config=config))
    next(hook)
    with pytest.raises(StopIteration):
        hook.send(True)
    return cov_plugin.options.cov_fail_under


@pytest.mark.parametrize(
    ("markexpr", "args", "expected_threshold"),
    [
        ("property", [], 0),
        ("benchmark", [], 0),
        ("slow", [], 0),
        ("", ["tests/test_web_route_contract.py"], 0),
        ("not slow", [], 85),
        ("property or benchmark", [], 85),
        ("", [], 85),
        ("", ["tests/test_web_route_contract.py", "tests/test_run_tests.py"], 85),
    ],
)
def test_focused_runs_disable_the_global_coverage_threshold(
    markexpr: str,
    args: list[str],
    expected_threshold: int,
) -> None:
    assert _run_coverage_threshold_hook(markexpr, args) == expected_threshold


def test_main_smoke_success_mentions_contract_not_checked(
    monkeypatch,
    capsys,
) -> None:
    called = {"smoke": 0, "contract": 0}

    def fake_smoke() -> int:
        called["smoke"] += 1
        return 0

    def fake_contract() -> int:
        called["contract"] += 1
        return 0

    monkeypatch.setattr(run_tests, "run_smoke_suite", fake_smoke)
    monkeypatch.setattr(run_tests, "run_contract_suite", fake_contract)

    exit_code = run_tests.main([])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert called == {"smoke": 1, "contract": 0}
    assert "strict pytest contract was not checked" in captured.out


def test_main_contract_mode_runs_contract_suite(monkeypatch) -> None:
    called = {"smoke": 0, "contract": 0}

    def fake_smoke() -> int:
        called["smoke"] += 1
        return 0

    def fake_contract() -> int:
        called["contract"] += 1
        return 0

    monkeypatch.setattr(run_tests, "run_smoke_suite", fake_smoke)
    monkeypatch.setattr(run_tests, "run_contract_suite", fake_contract)

    exit_code = run_tests.main(["--contract"])

    assert exit_code == 0
    assert called == {"smoke": 0, "contract": 1}


def test_profile_verification_is_explicit_and_uses_argument_array(monkeypatch) -> None:
    commands: list[list[str]] = []

    class Result:
        returncode = 0

    def fake_run(command, **kwargs):
        commands.append(list(command))
        assert kwargs["cwd"] == run_tests.REPO_ROOT
        return Result()

    monkeypatch.setattr(run_tests.subprocess, "run", fake_run)

    assert run_tests.main(["--verify-profile", "base", "--verify-profile", "mlx"]) == 0
    assert commands == [
        [
            sys.executable,
            str(run_tests.REPO_ROOT / "scripts" / "verify_runtime_profiles.py"),
            "--profile",
            "base",
            "--profile",
            "mlx",
        ]
    ]


def test_profile_verification_can_allow_hardware_skips() -> None:
    args = run_tests.parse_args(["--verify-profile", "cuda", "--allow-skipped-profiles"])
    assert (
        run_tests.build_profile_verification_command(
            args.verify_profile, allow_skipped=args.allow_skipped_profiles
        )[-1]
        == "--allow-skipped"
    )


def test_smoke_suite_resolves_tests_from_repo_root(monkeypatch, tmp_path) -> None:
    calls: list[str] = []

    def fake_run_test(test_path: str) -> tuple[bool, float]:
        calls.append(test_path)
        return True, 0.0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run_tests, "SMOKE_TEST_MODULES", ["tests/test_run_tests.py"])
    monkeypatch.setattr(run_tests, "run_test", fake_run_test)

    assert run_tests.run_smoke_suite() == 0
    assert calls == ["tests/test_run_tests.py"]


def test_smoke_suite_fails_when_no_tests_run(monkeypatch) -> None:
    monkeypatch.setattr(run_tests, "SMOKE_TEST_MODULES", ["tests/not_a_real_test.py"])

    assert run_tests.run_smoke_suite() == 1


def test_smoke_suite_fails_when_one_module_is_missing(monkeypatch) -> None:
    """A renamed module must not be skipped into a green run.

    Previously only the all-missing case failed, so dropping a single entry --
    e.g. renaming tests/test_tts.py -- silently stopped running it while the
    suite still reported PASSED.
    """
    calls: list[str] = []
    monkeypatch.setattr(
        run_tests, "run_test", lambda path: (calls.append(path), (True, 0.0))[1]
    )
    monkeypatch.setattr(
        run_tests,
        "SMOKE_TEST_MODULES",
        ["tests/test_run_tests.py", "tests/renamed_away.py"],
    )

    assert run_tests.run_smoke_suite() == 1
    assert calls == ["tests/test_run_tests.py"]


def test_every_smoke_module_runs_all_of_its_tests_through_pytest() -> None:
    """A module's __main__ block runs one aggregator; pytest collects every test."""
    for test_path in run_tests.SMOKE_TEST_MODULES:
        assert run_tests.build_smoke_command(test_path) == [
            sys.executable, "-m", "pytest", test_path, "-q", "-o", "addopts=",
        ]


def test_every_smoke_module_exists() -> None:
    missing = [p for p in run_tests.SMOKE_TEST_MODULES if not (run_tests.REPO_ROOT / p).is_file()]
    assert not missing
