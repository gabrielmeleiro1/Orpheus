"""
Unit tests for iterative feedback-loop runner quality gates.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from tools.feedback_loop_runner import (
    analyze_cleaned_text,
    analyze_cleaner_stats,
    build_parser,
    validate_api_backend_args,
)


def test_api_backend_requires_explicit_endpoint_configuration() -> None:
    parser = build_parser()
    args = parser.parse_args(["--source", "book.epub", "--cleaner-backend", "api"])

    assert args.cleaner_api_base_url is None
    assert args.cleaner_api_key_env is None
    with pytest.raises(SystemExit, match="--cleaner-api-base-url, --cleaner-api-key-env"):
        validate_api_backend_args(args)

    args = parser.parse_args(
        [
            "--source",
            "book.epub",
            "--cleaner-backend",
            "api",
            "--cleaner-api-base-url",
            "https://example.invalid/v1",
            "--cleaner-api-key-env",
            "EXAMPLE_API_KEY",
        ]
    )
    validate_api_backend_args(args)


def _test_detects_markdown_artifacts() -> None:
    bad = (
        "|Action|Conscious Reason|Subconscious Reason|\n"
        "|---|---|---|\n"
        "|1|foo|bar|\n"
        "line<br>break"
    )
    issues = analyze_cleaned_text(bad)
    assert any("table rows" in issue for issue in issues)
    assert any("separator" in issue for issue in issues)
    assert any("<br>" in issue for issue in issues)
    print("✓ detects markdown table and html-break artifacts")


def _test_accepts_cleaned_text_without_artifacts() -> None:
    good = (
        "I am letting my loss run because I am hoping and trying to avoid pain. "
        "I am taking profits because you cannot go broke taking a profit."
    )
    issues = analyze_cleaned_text(good)
    assert issues == []
    print("✓ clean prose passes artifact checks")


def _test_detects_llm_fallback_and_missing_llm_usage() -> None:
    issues = analyze_cleaner_stats(
        chapter_char_count=3000,
        llm_success=0,
        llm_fallback=2,
        rule_only=0,
        strict_llm=True,
    )
    assert any("fell back" in issue for issue in issues)
    assert any("No successful LLM-cleaned chunks" in issue for issue in issues)
    print("✓ detects fallback and strict-llm violations")


def test_feedback_loop_runner() -> bool:
    print("\n" + "=" * 50)
    print("FEEDBACK LOOP RUNNER TEST SUITE")
    print("=" * 50 + "\n")

    _test_detects_markdown_artifacts()
    _test_accepts_cleaned_text_without_artifacts()
    _test_detects_llm_fallback_and_missing_llm_usage()

    print("\n" + "=" * 50)
    print("ALL FEEDBACK LOOP RUNNER TESTS PASSED ✓")
    print("=" * 50 + "\n")
    return True


if __name__ == "__main__":
    success = test_feedback_loop_runner()
    raise SystemExit(0 if success else 1)
