"""Regression tests for the repo-level test runner contract."""

from __future__ import annotations

import sys

import run_tests


def test_parse_args_defaults_to_smoke_mode() -> None:
    args = run_tests.parse_args([])
    assert args.contract is False


def test_build_contract_command_uses_repo_pytest_contract() -> None:
    command = run_tests.build_contract_command()

    assert command[:4] == [sys.executable, "-m", "pytest", "tests"]
    assert command[-2:] == ["-m", run_tests.CORE_MARKER_EXPRESSION]
    assert "-o" not in command
    assert "addopts=" not in " ".join(command)


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


def test_smoke_suite_resolves_tests_from_repo_root(monkeypatch, tmp_path) -> None:
    calls: list[tuple[str, str]] = []

    def fake_run_test(test_path: str, mode: str) -> tuple[bool, float]:
        calls.append((test_path, mode))
        return True, 0.0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run_tests, "SMOKE_TEST_MODULES", [("tests/test_run_tests.py", run_tests.PYTEST)])
    monkeypatch.setattr(run_tests, "run_test", fake_run_test)

    assert run_tests.run_smoke_suite() == 0
    assert calls == [("tests/test_run_tests.py", run_tests.PYTEST)]


def test_smoke_suite_fails_when_no_tests_run(monkeypatch) -> None:
    monkeypatch.setattr(run_tests, "SMOKE_TEST_MODULES", [("tests/not_a_real_test.py", run_tests.PYTEST)])

    assert run_tests.run_smoke_suite() == 1
