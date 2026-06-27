"""Benchmark CLI, presets, validity thresholds, and regression reporting."""

from orpheus.bench.cli import (
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_TEMP_ROOT,
    SUMMARY_FILENAME,
    SUMMARY_SCHEMA_VERSION,
    RunSpec,
    build_parser,
    cleaner_backend_choices,
    cleaner_cuda_quantization_choices,
    main,
    run_benchmark,
)
from orpheus.bench.metrics import skipped_sections_from_document, skipped_sections_from_metrics
from orpheus.bench.policy import (
    POLICY_FIXTURES_DIR,
    load_policy_manifest,
    policy_bench_fixture_paths,
    policy_fixture_paths,
    validate_policy_parse_metrics,
)
from orpheus.bench.presets import PRESETS, SUPPORTED_PRESETS, PresetSpec, get_preset
from orpheus.bench.report import ReportError, build_report, write_report_outputs
from orpheus.bench.validity import (
    DELTA_THRESHOLD_PCT,
    MAX_SPREAD_PCT_OF_MEDIAN,
    MIN_REPETITIONS_FOR_VALIDITY,
)

__all__ = [
    "DEFAULT_OUTPUT_ROOT",
    "DEFAULT_TEMP_ROOT",
    "DELTA_THRESHOLD_PCT",
    "MAX_SPREAD_PCT_OF_MEDIAN",
    "MIN_REPETITIONS_FOR_VALIDITY",
    "POLICY_FIXTURES_DIR",
    "load_policy_manifest",
    "policy_bench_fixture_paths",
    "policy_fixture_paths",
    "validate_policy_parse_metrics",
    "PRESETS",
    "PresetSpec",
    "ReportError",
    "RunSpec",
    "SUMMARY_FILENAME",
    "SUMMARY_SCHEMA_VERSION",
    "SUPPORTED_PRESETS",
    "build_parser",
    "build_report",
    "cleaner_backend_choices",
    "cleaner_cuda_quantization_choices",
    "get_preset",
    "main",
    "run_benchmark",
    "skipped_sections_from_document",
    "skipped_sections_from_metrics",
    "write_report_outputs",
]
