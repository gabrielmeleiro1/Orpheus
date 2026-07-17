from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


FIXTURE_DIR = Path("tests/fixtures/performance_regression_dashboard")


def run_report(tmp_path: Path, *extra_args: str, expected_returncode: int = 1) -> tuple[Path, dict]:
    output_dir = tmp_path / "report"
    cmd = [
        sys.executable,
        "scripts/performance_regression_report.py",
        str(FIXTURE_DIR / "current_repeated_summary.json"),
        "--output-dir",
        str(output_dir),
        *extra_args,
    ]
    completed = subprocess.run(cmd, text=True, capture_output=True)
    assert completed.returncode == expected_returncode
    report_json = output_dir / "report.json"
    report_md = output_dir / "report.md"
    assert report_json.exists()
    assert report_md.exists()
    return output_dir, json.loads(report_json.read_text(encoding="utf-8"))


def case_by_name(report: dict, name: str | None) -> dict:
    return next(case for case in report["cases"] if case["name"] == name)


def metric(case: dict, name: str) -> dict:
    return case["metrics"][name]


def test_report_json_contains_required_repeated_summary_fields(tmp_path: Path) -> None:
    output_dir, report = run_report(
        tmp_path,
        "--baseline",
        str(FIXTURE_DIR / "baseline_repeated_summary.json"),
    )

    assert report["input_path"].endswith("current_repeated_summary.json")
    assert report["schema_version"] == 1
    assert report["generated_at"]
    assert report["status"] == "fail"
    assert report["provenance"]["created_at"] == "2026-05-10T11:00:00Z"
    assert report["provenance"]["source_path"] == "/fixtures/book.epub"
    assert report["provenance"]["command"][0] == "benchmark_nagel_repeated.py"
    assert report["provenance"]["environment"]["python_version"] == "3.12.0"
    assert report["validity"]["status"] == "fail"
    assert report["validity"]["gates"]["provenance"] == {"status": "pass", "missing": []}
    assert report["validity"]["gates"]["repetitions"]["failures"] == [
        {"case": "default", "actual": 2, "minimum": 3},
        {"case": "overlap", "actual": 2, "minimum": 3},
        {"case": None, "actual": 1, "minimum": 3},
        {"case": "metadata_gap", "actual": 1, "minimum": 3},
    ]
    assert report["validity"]["gates"]["artifacts"]["failures"] == [
        {"case": "default", "run": 1, "key": "tts_chunk_profile"},
        {"case": None, "run": 1, "key": "metrics_path"},
    ]
    assert report["validity"]["gates"]["thresholds"]["failures"] == [
        {"case": "default", "metric": "parse_seconds"},
        {"case": "overlap", "metric": "total_duration_ms"},
        {"case": None, "metric": "wall_time_seconds"},
        {"case": "metadata_gap", "metric": "wall_time_seconds"},
    ]
    assert report["validity"]["gates"]["variance"]["failures"] == [
        {"case": "default", "metric": "parse_seconds", "spread_pct_of_median": 11.1}
    ]

    default = case_by_name(report, "default")
    assert default["runs"] == 2
    assert default["settings"] == {"chunk_size": 700, "enable_chunk_overlap": False}
    assert default["status"] == "fail"
    assert metric(default, "wall_time_seconds") == {
        "median": 10.8,
        "min": 10.6,
        "max": 11.0,
        "mean": 10.8,
        "stdev": 0.28,
        "spread": 0.4,
        "spread_pct_of_median": 3.7,
        "threshold": {"max": 10.0},
        "threshold_status": "fail",
        "delta_abs": 0.8,
        "delta_pct": 8.0,
        "delta_status": "regression",
        "matched_baseline_case": "default",
        "match_reason": "name_and_settings",
    }
    assert metric(default, "parse_seconds")["threshold_status"] == "not_configured"
    assert metric(default, "parse_seconds")["delta_status"] == "non_regression"
    assert default["artifacts"][0]["key"] == "metrics_path"
    assert default["artifacts"][0]["path"].endswith("benchmark_summary.json")
    assert default["artifacts"][0]["exists"] is True
    assert any("missing artifact" in warning for warning in report["warnings"])

    overlap = case_by_name(report, "overlap")
    assert metric(overlap, "wall_time_seconds")["delta_status"] == "non_regression"
    assert metric(overlap, "total_duration_ms")["delta_status"] == "non_regression"

    unnamed = case_by_name(report, None)
    assert metric(unnamed, "wall_time_seconds")["match_reason"] == "unique_settings"
    assert metric(unnamed, "wall_time_seconds")["delta_status"] == "non_regression"

    metadata_gap = case_by_name(report, "metadata_gap")
    assert metadata_gap["settings"] is None
    assert metric(metadata_gap, "wall_time_seconds")["threshold_status"] == "not_configured"
    assert metric(metadata_gap, "wall_time_seconds")["delta_status"] == "not_available"
    assert any("missing settings" in warning for warning in report["warnings"])
    assert any("missing threshold" in warning for warning in report["warnings"])
    assert (output_dir / "report.md").read_text(encoding="utf-8").startswith(
        "# Performance Regression Report"
    )


def test_report_without_baseline_marks_deltas_not_available(tmp_path: Path) -> None:
    _, report = run_report(tmp_path)

    assert report["status"] == "fail"
    assert all(
        metric_data["delta_status"] == "not_available"
        for case in report["cases"]
        for metric_data in case["metrics"].values()
    )
    assert any("baseline not provided" in warning for warning in report["warnings"])


def test_cli_rejects_non_repeated_schema(tmp_path: Path) -> None:
    bad_input = tmp_path / "bad.json"
    bad_input.write_text(json.dumps({"schema_version": 3}), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/performance_regression_report.py",
            str(bad_input),
            "--output-dir",
            str(tmp_path / "out"),
        ],
        text=True,
        capture_output=True,
    )

    assert completed.returncode != 0
    assert "schema_version=1" in completed.stderr
