#!/usr/bin/env python3
"""Repo-level smoke and contract test entrypoints."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT = "script"
PYTEST = "pytest"
CORE_MARKER_EXPRESSION = "not property and not benchmark and not slow and not ui"
REPO_ROOT = Path(__file__).resolve().parent

# Smoke modules to run (in order)
SMOKE_TEST_MODULES: list[tuple[str, str]] = [
    ("tests/test_pdf_parser.py", SCRIPT),
    ("tests/test_epub_parser.py", SCRIPT),
    ("tests/test_storage_migrations.py", SCRIPT),
    ("tests/test_storage_repository.py", SCRIPT),
    ("tests/test_storage_maintenance.py", PYTEST),
    ("tests/test_cleaner.py", SCRIPT),
    ("tests/test_cleaner_golden_set.py", SCRIPT),
    ("tests/test_cleaner_hallucination_guard.py", SCRIPT),
    ("tests/test_cleaner_heuristic.py", SCRIPT),
    ("tests/test_cleaner_latency.py", SCRIPT),
    ("tests/test_cleaner_api_global_limiter.py", PYTEST),
    ("tests/test_cleaner_api_trace_multichapter.py", SCRIPT),
    ("tests/test_cleaner_text_preservation.py", SCRIPT),
    ("tests/test_pipeline_packaging.py", SCRIPT),
    ("tests/test_feedback_loop_runner.py", SCRIPT),
    ("tests/test_parallel_fallback.py", SCRIPT),
    ("tests/test_runtime_recovery.py", SCRIPT),
    ("tests/test_tts_runtime_lock.py", SCRIPT),
    ("tests/test_library_service.py", PYTEST),
    ("tests/test_markdown_parser.py", PYTEST),
    ("tests/test_text_parser.py", PYTEST),
    ("tests/test_voice_service_thread_safety.py", PYTEST),
    ("tests/test_tts_resilience.py", PYTEST),
    ("tests/test_events.py", SCRIPT),
    ("tests/test_conversion_service.py", PYTEST),
    ("tests/test_run_tests.py", PYTEST),
    ("tests/test_web_api.py", SCRIPT),
    ("tests/test_web_events.py", SCRIPT),
    ("tests/test_voice_recommender.py", PYTEST),
    ("tests/test_audio.py", PYTEST),
    ("tests/test_ingestion.py", PYTEST),
    ("tests/test_memory_management.py", PYTEST),
    ("tests/test_pool_manager.py", PYTEST),
    ("tests/test_parallel_pipeline.py", PYTEST),
    ("tests/test_pipeline.py", PYTEST),
    ("tests/test_tts.py", PYTEST),
    ("tests/test_tts_engine_modularization.py", PYTEST),
    ("tests/test_tts_factory_contract.py", PYTEST),
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
    args = parser.parse_args(argv)
    if args.allow_skipped_profiles and not args.verify_profile:
        parser.error("--allow-skipped-profiles requires --verify-profile")
    return args


def build_smoke_command(test_path: str, mode: str) -> list[str]:
    """Build the command used for a smoke-suite test module."""
    if mode == PYTEST:
        return [sys.executable, "-m", "pytest", test_path, "-q", "-o", "addopts="]
    return [sys.executable, test_path]


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


def run_test(test_path: str, mode: str) -> tuple[bool, float]:
    """
    Run a single test module.

    Returns:
        Tuple of (passed, duration_seconds)
    """
    start = time.time()
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    result = subprocess.run(
        build_smoke_command(test_path, mode),
        capture_output=True,
        env=env,
        text=True,
        cwd=REPO_ROOT,
    )
    duration = time.time() - start

    passed = result.returncode == 0

    if not passed:
        print(f"\n{'=' * 50}")
        print(f"FAILED: {test_path} ({mode})")
        print(f"{'=' * 50}")
        print(result.stdout)
        print(result.stderr)

    return passed, duration


def run_smoke_suite() -> int:
    """Run the fast local smoke suite."""
    print("\n" + "=" * 60)
    print("   ORPHEUS - SMOKE SUITE")
    print("=" * 60 + "\n")

    results: list[tuple[str, str, bool, float]] = []
    total_start = time.time()

    for test_path, mode in SMOKE_TEST_MODULES:
        if not (REPO_ROOT / test_path).exists():
            print(f"WARN SKIP: {test_path} (not found)")
            continue

        print(f"> Running {test_path} [{mode}]...", end=" ", flush=True)
        passed, duration = run_test(test_path, mode)

        status = "PASS" if passed else "FAIL"
        print(f"{status} ({duration:.1f}s)")

        results.append((test_path, mode, passed, duration))

    total_duration = time.time() - total_start

    # Summary
    print("\n" + "=" * 60)
    print("   RESULTS SUMMARY")
    print("=" * 60)

    passed_count = sum(1 for _, _, p, _ in results if p)
    failed_count = len(results) - passed_count

    for test_path, mode, passed, duration in results:
        status = "PASS" if passed else "FAIL"
        name = Path(test_path).stem
        print(f"  {status} {name} [{mode}]: {duration:.1f}s")

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

    result = subprocess.run(build_contract_command(), cwd=REPO_ROOT)
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    """Run either the smoke suite or the authoritative pytest contract."""
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

    exit_code = run_smoke_suite()
    if exit_code == 0:
        print("Local smoke suite passed; strict pytest contract was not checked.")
    else:
        print("Local smoke suite failed; strict pytest contract was not checked.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
