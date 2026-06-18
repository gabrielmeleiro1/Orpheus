"""Tests for cleaner choice parity in CLI parsers."""

from __future__ import annotations

from modules.tts.cleaner_backends import available_cleaner_backends, register_cleaner_backend
from tests.fixtures.fake_cleaner_backend import FakeCleanerBackend


def _parser_choices(parser, option: str) -> tuple[str, ...]:
    for action in parser._actions:
        if option in action.option_strings:
            return tuple(action.choices)
    raise AssertionError(f"Parser option not found: {option}")


def test_benchmark_parser_cleaner_choices_reflect_late_registry_mutation(
    registry_snapshot,
):
    from scripts import benchmark_nagel_pipeline

    register_cleaner_backend("late", FakeCleanerBackend)

    args = benchmark_nagel_pipeline.parse_args([
        "book.epub",
        "--cleaner-backend",
        "late",
    ])

    assert args.cleaner_backend == "late"


def test_kokoro_benchmark_parser_cleaner_choices_reflect_late_registry_mutation(
    registry_snapshot,
):
    from benchmarks.kokoro_single_book import run_kokoro_single_book

    register_cleaner_backend("late", FakeCleanerBackend)

    args = run_kokoro_single_book.parse_args([
        "book.epub",
        "--cleaner-backend",
        "late",
    ])

    assert args.cleaner_backend == "late"


def test_parallel_worker_benchmark_parser_cleaner_choices_reflect_late_registry_mutation(
    registry_snapshot,
):
    from tools import benchmark_parallel_workers

    register_cleaner_backend("late", FakeCleanerBackend)

    args = benchmark_parallel_workers.parse_args([
        "book.epub",
        "--cleaner-backend",
        "late",
    ])

    assert args.cleaner_backend == "late"


def test_feedback_loop_parser_cleaner_choices_match_current_canonical_sources(
    registry_snapshot,
    monkeypatch,
):
    from modules.tts import cleaner_config
    from tools import feedback_loop_runner

    register_cleaner_backend("late", FakeCleanerBackend)
    monkeypatch.setattr(
        cleaner_config,
        "CUDA_CLEANER_QUANTIZATIONS",
        ("test-quant",),
    )

    parser = feedback_loop_runner.build_parser()

    assert _parser_choices(parser, "--cleaner-backend") == available_cleaner_backends()
    assert _parser_choices(parser, "--cleaner-cuda-quantization") == ("test-quant",)
