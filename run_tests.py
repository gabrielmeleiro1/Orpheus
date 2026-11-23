#!/usr/bin/env python3
"""Repo-level smoke and contract test entrypoints."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CORE_MARKER_EXPRESSION = "not property and not benchmark and not slow"
REPO_ROOT = Path(__file__).resolve().parent

# Smoke modules to run (in order)
SMOKE_TEST_MODULES: list[str] = [
    "tests/test_pdf_parser.py",
    "tests/test_epub_parser.py",
    "tests/test_storage_migrations.py",
    "tests/test_storage_repository.py",
    "tests/test_storage_maintenance.py",
    "tests/test_cleaner.py",
    "tests/test_cleaner_golden_set.py",
    "tests/test_cleaner_hallucination_guard.py",
    "tests/test_cleaner_heuristic.py",
    "tests/test_cleaner_latency.py",
    "tests/test_cleaner_api_global_limiter.py",
    "tests/test_cleaner_api_trace_multichapter.py",
    "tests/test_cleaner_text_preservation.py",
    "tests/test_pipeline_packaging.py",
    "tests/test_parallel_fallback.py",
    "tests/test_runtime_recovery.py",
    "tests/test_tts_runtime_lock.py",
    "tests/test_library_service.py",
    "tests/test_markdown_parser.py",
    "tests/test_text_parser.py",
    "tests/test_voice_service_thread_safety.py",
    "tests/test_tts_resilience.py",
    "tests/test_events.py",
    "tests/test_conversion_service.py",
    "tests/test_run_tests.py",
    "tests/test_web_api.py",
    "tests/test_web_events.py",
    "tests/test_voice_recommender.py",
    "tests/test_audio.py",
    "tests/test_ingestion.py",
    "tests/test_pool_manager.py",
    "tests/test_parallel_pipeline.py",
    "tests/test_pipeline.py",
    "tests/test_tts.py",
    "tests/test_tts_engine_modularization.py",
    "tests/test_tts_factory_contract.py",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the test runner."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--contract",
        action="store_true",
        help="Run the authoritative pytest contract instead of the smoke suite.",
    )
    mode.add_argument(
        "--verify-profile",
        action="append",
        choices=("base", "mlx", "cuda"),
        help="Opt in to a clean install/audit gate for a runtime profile (repeatable).",
    )
    parser.add_argument(
        "--allow-skipped-profiles",
        action="store_true",
        help="Allow requested profiles with unavailable hardware to be reported as skipped.",
    )
    parser.add_argument(
        "--isolated",
        action="store_true",
        help="Run every smoke module in its own pytest process instead of one shared run.",
    )
    args = parser.parse_args(argv)
    if args.allow_skipped_profiles and not args.verify_profile:
        parser.error("--allow-skipped-profiles requires --verify-profile")
    if args.isolated and (args.contract or args.verify_profile):
        parser.error("--isolated applies only to the smoke suite")
    return args


def build_smoke_command(test_path: str) -> list[str]:
    """Build the command used for a smoke-suite test module."""
    return [sys.executable, "-m", "pytest", test_path, "-q", "-o", "addopts="]


SMOKE_RESULTS_OPTION = "--orpheus-smoke-results"
_SMOKE_BATCH_ENV = "ORPHEUS_SMOKE_BATCH"


def build_smoke_batch_command(test_paths: list[str], results_path: Path) -> list[str]:
    """Build one pytest run over several smoke modules that records per-module results.

    It matches build_smoke_command's options; ``-p run_tests`` loads this
    file's hooks, which write each module's outcome to results_path.
    """
    return [
        sys.executable, "-m", "pytest", *test_paths, "-q", "-o", "addopts=",
        # One module failing to import must not stop the others from running.
        "--continue-on-collection-errors",
        "-p", "run_tests", SMOKE_RESULTS_OPTION, str(results_path),
    ]


def pytest_addoption(parser) -> None:
    """Register the batch smoke results option when pytest loads this module."""
    parser.addoption(SMOKE_RESULTS_OPTION, default=None, help=argparse.SUPPRESS)


def pytest_configure(config) -> None:
    results_path = config.getoption(SMOKE_RESULTS_OPTION)
    if results_path:
        config.pluginmanager.register(_SmokeResultRecorder(Path(results_path)))


class _SmokeResultRecorder:
    """Collect pass/fail and test time per module inside a batch smoke run."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.modules: dict[str, dict[str, object]] = {}

    def _module(self, nodeid: str) -> dict[str, object]:
        module = nodeid.split("::", 1)[0]
        return self.modules.setdefault(module, {"failed": False, "duration": 0.0, "reports": 0})

    def pytest_collectreport(self, report) -> None:
        if report.failed:
            self._module(report.nodeid)["failed"] = True

    def pytest_runtest_logreport(self, report) -> None:
        entry = self._module(report.nodeid)
        entry["reports"] = int(entry["reports"]) + 1
        entry["duration"] = float(entry["duration"]) + float(report.duration)
        if report.failed:
            entry["failed"] = True

    def pytest_sessionfinish(self, exitstatus) -> None:
        payload = {"exitstatus": int(exitstatus), "modules": self.modules}
        self.path.write_text(json.dumps(payload), encoding="utf-8")


def run_smoke_batch(test_paths: list[str]) -> dict[str, tuple[bool, float]] | None:
    """Run test_paths in one pytest process and return per-module results.

    Modules without a result (no tests reported) are left out. Returns None
    when the run cannot be attributed to modules -- it crashed, stopped early,
    or failed without any module failing -- so the caller runs each module on
    its own instead.
    """
    if os.environ.get(_SMOKE_BATCH_ENV):
        # A smoke run started from inside a batch (the runner's own tests)
        # must not start another batch, or it would recurse.
        return None
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env[_SMOKE_BATCH_ENV] = "1"
    with tempfile.TemporaryDirectory(prefix="orpheus-smoke-") as temp:
        results_path = Path(temp) / "results.json"
        completed = subprocess.run(
            build_smoke_batch_command(test_paths, results_path),
            capture_output=True,
            env=env,
            text=True,
            cwd=REPO_ROOT,
        )
        try:
            payload = json.loads(results_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    if completed.returncode not in (0, 1) or payload.get("exitstatus") != completed.returncode:
        return None
    results: dict[str, tuple[bool, float]] = {}
    any_failed = False
    for test_path in test_paths:
        entry = payload["modules"].get(Path(test_path).as_posix())
        if not entry:
            continue
        any_failed = any_failed or bool(entry["failed"])
        if entry["reports"]:
            results[test_path] = (not entry["failed"], float(entry["duration"]))
    if any_failed != (completed.returncode == 1):
        return None
    return results


def build_contract_command() -> list[str]:
    """Build the authoritative pytest contract command."""
    return [
        sys.executable,
        "-m",
        "pytest",
        "tests",
        "-m",
        CORE_MARKER_EXPRESSION,
    ]


def build_profile_verification_command(
    profiles: list[str], *, allow_skipped: bool = False
) -> list[str]:
    """Build the isolated runtime-profile verification command."""
    command = [sys.executable, str(REPO_ROOT / "scripts" / "verify_runtime_profiles.py")]
    for profile in profiles:
        command.extend(("--profile", profile))
    if allow_skipped:
        command.append("--allow-skipped")
    return command


def run_test(test_path: str) -> tuple[bool, float]:
    """
    Run a single test module.

    Returns:
        Tuple of (passed, duration_seconds)
    """
    start = time.time()
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    result = subprocess.run(
        build_smoke_command(test_path),
        capture_output=True,
        env=env,
        text=True,
        cwd=REPO_ROOT,
    )
    duration = time.time() - start

    passed = result.returncode == 0

    if not passed:
        print(f"\n{'=' * 50}")
        print(f"FAILED: {test_path}")
        print(f"{'=' * 50}")
        print(result.stdout)
        print(result.stderr)

    return passed, duration


def _run_smoke_modules(
    batch: dict[str, tuple[bool, float]],
) -> list[tuple[str, bool, float]]:
    """Report each module, running on its own any module batch did not pass."""
    results: list[tuple[str, bool, float]] = []
    for test_path in SMOKE_TEST_MODULES:
        if not (REPO_ROOT / test_path).exists():
            # A renamed or deleted module means the suite silently stopped
            # covering something, so record it as a failure rather than
            # reporting PASSED for a shorter run than the caller asked for.
            print(f"FAIL: {test_path} (not found)")
            results.append((test_path, False, 0.0))
            continue

        combined = batch.get(test_path)
        if combined is not None and combined[0]:
            passed, duration = combined
            print(f"> {test_path}... PASS ({duration:.1f}s)")
        else:
            # Failures (and modules the combined run did not report) run in
            # their own process, which decides the result and shows the output.
            print(f"> Running {test_path}...", end=" ", flush=True)
            passed, duration = run_test(test_path)
            print(f"{'PASS' if passed else 'FAIL'} ({duration:.1f}s)")

        results.append((test_path, passed, duration))
    return results


def run_smoke_suite(*, isolated: bool = False) -> int:
    """Run the fast local smoke suite.

    Modules share one pytest process unless ``isolated``; any module that
    fails there is run again on its own, and that run decides its result.
    """
    print("\n" + "=" * 60)
    print("   ORPHEUS - SMOKE SUITE")
    print("=" * 60 + "\n")

    total_start = time.time()

    batch: dict[str, tuple[bool, float]] = {}
    present = [path for path in SMOKE_TEST_MODULES if (REPO_ROOT / path).exists()]
    if present and not isolated:
        print(f"> Running {len(present)} modules in one pytest process...", flush=True)
        combined = run_smoke_batch(present)
        if combined is None:
            print("  The combined run could not be attributed to modules; "
                  "running each module on its own.")
        else:
            batch = combined

    results = _run_smoke_modules(batch)
    batch_failures = {path for path, (passed, _) in batch.items() if not passed}
    if batch_failures and all(
        passed for path, passed, _ in results if path in batch_failures
    ):
        # No module reproduces the combined failure on its own (for example a
        # session teardown error reported against whichever test ran last), so
        # fall back to the fully isolated run to find out which module fails.
        print("\n  No module reproduced the combined run's failure on its own; "
              "running every module on its own.")
        results = _run_smoke_modules({})

    total_duration = time.time() - total_start

    # Summary
    print("\n" + "=" * 60)
    print("   RESULTS SUMMARY")
    print("=" * 60)

    passed_count = sum(1 for _, p, _ in results if p)
    failed_count = len(results) - passed_count

    for test_path, passed, duration in results:
        status = "PASS" if passed else "FAIL"
        name = Path(test_path).stem
        print(f"  {status} {name}: {duration:.1f}s")

    print(f"\n  Total: {passed_count} passed, {failed_count} failed")
    print(f"  Duration: {total_duration:.1f}s")
    print("=" * 60 + "\n")

    if not results:
        print("SMOKE SUITE FAILED - no tests ran")
        return 1

    if failed_count > 0:
        print("SMOKE SUITE FAILED")
        return 1

    print("SMOKE SUITE PASSED")
    return 0


def run_contract_suite() -> int:
    """Run the authoritative pytest contract."""
    print("\n" + "=" * 60)
    print("   ORPHEUS - PYTEST CONTRACT")
    print("=" * 60 + "\n")
    print(f"> Running {' '.join(build_contract_command())}\n")

    kwargs: dict[str, object] = {}
    if os.name == "nt":
        # Run the suite on its own hidden console. A console control event
        # raised anywhere in the pytest process tree is delivered to every
        # process on the same console — including this wrapper and the CI
        # step shell, which then reports failure for a green run. Events
        # cannot cross consoles; explicit std handles keep the output in
        # the step log.
        kwargs = {
            "creationflags": subprocess.CREATE_NO_WINDOW,
            "stdin": subprocess.DEVNULL,
            "stdout": sys.stdout,
            "stderr": sys.stderr,
        }
    result = subprocess.run(build_contract_command(), cwd=REPO_ROOT, **kwargs)
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    """Run either the smoke suite or the authoritative pytest contract."""
    if os.name == "nt":
        # A console CTRL_C_EVENT reaches every process on the console,
        # including this wrapper. Our only job is to relay the child suite's
        # exit code; dying to a stray Ctrl+C aimed at (or leaked from) the
        # child turns a green run into a failed one. A real Ctrl+C still
        # stops the run: the child receives it and exits, and we return.
        import signal

        signal.signal(signal.SIGINT, signal.SIG_IGN)
    args = parse_args(argv)

    if args.verify_profile:
        command = build_profile_verification_command(
            args.verify_profile,
            allow_skipped=args.allow_skipped_profiles,
        )
        print("\nRunning clean runtime profile verification...\n", flush=True)
        return subprocess.run(command, cwd=REPO_ROOT).returncode

    if args.contract:
        exit_code = run_contract_suite()
        if exit_code == 0:
            print("\nPYTEST CONTRACT PASSED - Safe to commit")
        else:
            print("\nPYTEST CONTRACT FAILED - Fix before commit")
        return exit_code

    exit_code = run_smoke_suite(isolated=args.isolated)
    if exit_code == 0:
        print("Local smoke suite passed; strict pytest contract was not checked.")
    else:
        print("Local smoke suite failed; strict pytest contract was not checked.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
