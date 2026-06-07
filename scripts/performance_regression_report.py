#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DELTA_THRESHOLD_PCT = 5.0
METRIC_FIELDS = ("median", "min", "max", "mean", "stdev", "spread", "spread_pct_of_median")
MIN_REPETITIONS_FOR_VALIDITY = 3
MAX_SPREAD_PCT_OF_MEDIAN = 10.0


class ReportError(Exception):
    pass


def load_repeated_summary(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ReportError(f"{path} must be repeated_benchmark_summary.json schema_version=1")
    if not isinstance(data.get("case_summaries"), list):
        raise ReportError(f"{path} is missing case_summaries")
    return data


def is_timing_metric(name: str) -> bool:
    return name.endswith("_seconds") or name == "total_duration_ms"


def settings_equal(left: Any, right: Any) -> bool:
    return left is not None and right is not None and left == right


def case_label(case: dict[str, Any]) -> str | None:
    name = case.get("name")
    return name if name not in ("", None) else None


def build_baseline_matcher(
    baseline: dict[str, Any] | None,
    current: dict[str, Any],
    warnings: list[str],
) -> Any:
    if baseline is None:
        warnings.append("baseline not provided; metric deltas are not_available")
        return lambda case: (None, "baseline_not_provided")

    current_source = (current.get("provenance") or {}).get("source_path")
    baseline_source = (baseline.get("provenance") or {}).get("source_path")
    if current_source and baseline_source and current_source != baseline_source:
        warnings.append(
            "baseline source_path does not match current source_path; metric deltas are not_available"
        )
        return lambda case: (None, "source_path_mismatch")

    baseline_cases = baseline.get("case_summaries") or []

    def match(case: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        name = case_label(case)
        settings = case.get("settings")
        if name is not None:
            named = [candidate for candidate in baseline_cases if case_label(candidate) == name]
            if settings is not None:
                named_settings = [
                    candidate for candidate in named if settings_equal(settings, candidate.get("settings"))
                ]
                if len(named_settings) == 1:
                    return named_settings[0], "name_and_settings"
            elif len(named) == 1:
                return named[0], "name"
            return None, "unmatched_name"
        if settings is not None:
            settings_matches = [
                candidate
                for candidate in baseline_cases
                if case_label(candidate) is None and settings_equal(settings, candidate.get("settings"))
            ]
            if len(settings_matches) == 1:
                return settings_matches[0], "unique_settings"
            return None, "ambiguous_or_missing_settings_match"
        return None, "missing_name_and_settings"

    return match


def summarize_artifacts(case_name: str | None, runs: list[dict[str, Any]], warnings: list[str]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for index, run in enumerate(runs, start=1):
        for key, value in (run.get("artifacts") or {}).items():
            if not isinstance(value, dict):
                continue
            artifact = {
                "run": index,
                "key": key,
                "path": value.get("path"),
                "exists": bool(value.get("exists")),
            }
            artifacts.append(artifact)
            if not artifact["exists"]:
                warnings.append(
                    f"missing artifact for case {case_name or '<unnamed>'} run {index}: {key}"
                )
    return artifacts


def runs_for_case(summary: dict[str, Any], case: dict[str, Any]) -> list[dict[str, Any]]:
    name = case_label(case)
    settings = case.get("settings")
    runs = []
    for run in summary.get("runs") or []:
        run_name = run.get("case_name")
        if name is not None and run_name == name:
            runs.append(run)
        elif name is None and run_name in (None, ""):
            runs.append(run)
    if runs:
        return runs
    if settings is not None:
        return [
            run
            for run in summary.get("runs") or []
            if (run.get("metrics") or {}).get("settings") == settings
        ]
    return []


def build_validity(
    current: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    provenance = current.get("provenance") or {}
    missing_provenance = [
        key
        for key in ("created_at", "source_path", "command", "environment")
        if not provenance.get(key)
    ]
    artifact_failures = []
    repetition_failures = []
    threshold_failures = []
    variance_failures = []
    for case in current.get("case_summaries") or []:
        name = case_label(case)
        runs = case.get("runs")
        if not isinstance(runs, int) or runs < MIN_REPETITIONS_FOR_VALIDITY:
            repetition_failures.append(
                {"case": name, "actual": runs, "minimum": MIN_REPETITIONS_FOR_VALIDITY}
            )
        for metric_name, metric_data in (case.get("metrics") or {}).items():
            if not isinstance(metric_data, dict):
                continue
            if not metric_data.get("threshold"):
                threshold_failures.append({"case": name, "metric": metric_name})
            spread_pct = metric_data.get("spread_pct_of_median")
            if isinstance(spread_pct, (int, float)) and spread_pct > MAX_SPREAD_PCT_OF_MEDIAN:
                variance_failures.append(
                    {
                        "case": name,
                        "metric": metric_name,
                        "spread_pct_of_median": spread_pct,
                    }
                )
        for run_index, run in enumerate(runs_for_case(current, case), start=1):
            for key, artifact in (run.get("artifacts") or {}).items():
                if isinstance(artifact, dict) and not artifact.get("exists"):
                    artifact_failures.append({"case": name, "run": run_index, "key": key})

    gates = {
        "artifacts": {
            "status": "pass" if not artifact_failures else "fail",
            "failures": artifact_failures,
        },
        "provenance": {
            "status": "pass" if not missing_provenance else "fail",
            "missing": missing_provenance,
        },
        "repetitions": {
            "status": "pass" if not repetition_failures else "fail",
            "minimum": MIN_REPETITIONS_FOR_VALIDITY,
            "failures": repetition_failures,
        },
        "thresholds": {
            "status": "pass" if not threshold_failures else "fail",
            "failures": threshold_failures,
        },
        "variance": {
            "status": "pass" if not variance_failures else "fail",
            "max_spread_pct_of_median": MAX_SPREAD_PCT_OF_MEDIAN,
            "failures": variance_failures,
        },
    }
    for gate_name, gate in gates.items():
        if gate["status"] == "fail":
            warnings.append(f"validity gate failed: {gate_name}")
    return {
        "status": "pass"
        if all(gate["status"] == "pass" for gate in gates.values())
        else "fail",
        "gates": gates,
    }


def metric_report(
    metric_name: str,
    metric_data: dict[str, Any],
    baseline_case: dict[str, Any] | None,
    match_reason: str,
    case_name: str | None,
    warnings: list[str],
) -> dict[str, Any]:
    report = {field: metric_data.get(field) for field in METRIC_FIELDS if field in metric_data}
    threshold = metric_data.get("threshold")
    report["threshold"] = threshold
    report["threshold_status"] = metric_data.get("status") if threshold else "not_configured"
    if not threshold:
        warnings.append(f"missing threshold for case {case_name or '<unnamed>'} metric {metric_name}")

    report.update(
        {
            "delta_abs": None,
            "delta_pct": None,
            "delta_status": "not_available",
            "matched_baseline_case": None,
            "match_reason": match_reason,
        }
    )
    if baseline_case is None:
        warnings.append(
            f"unmatched baseline for case {case_name or '<unnamed>'} metric {metric_name}: {match_reason}"
        )
        return report
    report["matched_baseline_case"] = case_label(baseline_case)

    if not is_timing_metric(metric_name):
        return report
    baseline_metric = (baseline_case.get("metrics") or {}).get(metric_name)
    baseline_median = baseline_metric.get("median") if isinstance(baseline_metric, dict) else None
    current_median = metric_data.get("median")
    if not isinstance(current_median, (int, float)) or not isinstance(baseline_median, (int, float)):
        warnings.append(
            f"missing median for case {case_name or '<unnamed>'} metric {metric_name}; delta unavailable"
        )
        return report
    if baseline_median == 0:
        warnings.append(
            f"zero baseline median for case {case_name or '<unnamed>'} metric {metric_name}; delta unavailable"
        )
        return report

    delta_abs = current_median - baseline_median
    delta_pct = (delta_abs / baseline_median) * 100.0
    report["delta_abs"] = round(delta_abs, 4)
    report["delta_pct"] = round(delta_pct, 4)
    report["delta_status"] = "regression" if delta_pct > DELTA_THRESHOLD_PCT else "non_regression"
    return report


def build_report(
    current_path: Path,
    current: dict[str, Any],
    baseline: dict[str, Any] | None,
) -> dict[str, Any]:
    warnings: list[str] = []
    provenance = current.get("provenance") or {}
    if not provenance.get("created_at"):
        warnings.append("missing metadata: provenance.created_at")
    if not provenance.get("source_path"):
        warnings.append("missing metadata: provenance.source_path")
    if not provenance.get("command"):
        warnings.append("missing metadata: provenance.command")
    if not provenance.get("environment"):
        warnings.append("missing metadata: provenance.environment")
    validity = build_validity(current, warnings)

    match_baseline = build_baseline_matcher(baseline, current, warnings)
    cases = []
    report_status = "pass"
    for case in current.get("case_summaries") or []:
        name = case_label(case)
        if not name:
            warnings.append("missing metadata: case name")
        if case.get("settings") is None:
            warnings.append(f"missing settings for case {name or '<unnamed>'}")
        baseline_case, match_reason = match_baseline(case)
        case_runs = runs_for_case(current, case)
        case_report = {
            "name": name,
            "runs": case.get("runs"),
            "settings": case.get("settings"),
            "status": case.get("status"),
            "success": case.get("status") == "pass",
            "metrics": {},
            "artifacts": summarize_artifacts(name, case_runs, warnings),
        }
        if case.get("status") == "fail":
            report_status = "fail"
        for metric_name, metric_data in sorted((case.get("metrics") or {}).items()):
            if not isinstance(metric_data, dict):
                continue
            current_metric = metric_report(
                metric_name,
                metric_data,
                baseline_case,
                match_reason,
                name,
                warnings,
            )
            if current_metric["threshold_status"] == "fail":
                report_status = "fail"
            if current_metric["delta_status"] == "regression":
                report_status = "fail"
            case_report["metrics"][metric_name] = current_metric
        cases.append(case_report)
    if validity["status"] == "fail":
        report_status = "fail"

    return {
        "input_path": str(current_path),
        "schema_version": current.get("schema_version"),
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "status": report_status,
        "provenance": {
            "created_at": provenance.get("created_at"),
            "source_path": provenance.get("source_path"),
            "command": provenance.get("command"),
            "environment": provenance.get("environment"),
        },
        "validity": validity,
        "warnings": sorted(set(warnings)),
        "cases": cases,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Performance Regression Report",
        "",
        f"- Input: `{report['input_path']}`",
        f"- Schema version: `{report['schema_version']}`",
        f"- Generated at: `{report['generated_at']}`",
        f"- Status: `{report['status']}`",
        "",
        "## Provenance",
        "",
        f"- Created at: `{report['provenance'].get('created_at')}`",
        f"- Source path: `{report['provenance'].get('source_path')}`",
        f"- Command: `{json.dumps(report['provenance'].get('command'))}`",
        f"- Environment: `{json.dumps(report['provenance'].get('environment'), sort_keys=True)}`",
        "",
        "## Cases",
        "",
    ]
    for case in report["cases"]:
        lines.extend(
            [
                f"### {case['name'] or '<unnamed>'}",
                "",
                f"- Runs: `{case['runs']}`",
                f"- Status: `{case['status']}`",
                f"- Settings: `{json.dumps(case['settings'], sort_keys=True)}`",
                "",
                "| Metric | Median | Min | Max | Mean | Stdev | Spread | Spread % | Threshold | Threshold status | Delta % | Delta status |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: | --- |",
            ]
        )
        for metric_name, metric in case["metrics"].items():
            lines.append(
                "| "
                + " | ".join(
                    [
                        metric_name,
                        str(metric.get("median")),
                        str(metric.get("min")),
                        str(metric.get("max")),
                        str(metric.get("mean")),
                        str(metric.get("stdev")),
                        str(metric.get("spread")),
                        str(metric.get("spread_pct_of_median")),
                        f"`{json.dumps(metric.get('threshold'), sort_keys=True)}`",
                        str(metric.get("threshold_status")),
                        str(metric.get("delta_pct")),
                        str(metric.get("delta_status")),
                    ]
                )
                + " |"
            )
        if case["artifacts"]:
            lines.extend(["", "Artifacts:"])
            for artifact in case["artifacts"]:
                lines.append(
                    f"- run {artifact['run']} `{artifact['key']}`: "
                    f"`{artifact['path']}` exists={artifact['exists']}"
                )
        lines.append("")
    if report["warnings"]:
        lines.extend(["## Warnings", ""])
        lines.extend(f"- {warning}" for warning in report["warnings"])
        lines.append("")
    return "\n".join(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary_path", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        current = load_repeated_summary(args.summary_path)
        baseline = load_repeated_summary(args.baseline) if args.baseline else None
        report = build_report(args.summary_path, current, baseline)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (args.output_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
    except (OSError, json.JSONDecodeError, ReportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 1 if report["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
