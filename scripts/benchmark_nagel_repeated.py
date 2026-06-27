#!/usr/bin/env python3
"""Benchmark-only repeated runner for the Nagel pipeline benchmark."""

from __future__ import annotations

import argparse
import json
import math
import platform
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orpheus.bench.validity import (  # noqa: E402
    MAX_SPREAD_PCT_OF_MEDIAN,
    MIN_REPETITIONS_FOR_VALIDITY,
)
from scripts.benchmark_nagel_pipeline import run_benchmark  # noqa: E402


METRIC_KEYS = (
    "wall_time_seconds",
    "processing_time_seconds",
    "parse_seconds",
    "chunk_seconds",
    "extracted_chars",
    "chunks",
    "chapters",
    "tts_seconds_total",
    "clean_seconds_total",
    "encode_seconds_total",
    "package_seconds_total",
)


def _round(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, 4)


def _case_matrix(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.case:
        return [_parse_case_spec(spec) for spec in args.case]
    return [
        {
            "name": "default_current",
            "chunk_size": None,
            "enable_chunk_overlap": False,
            "enable_tts_file_batch_results": False,
        }
    ]


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"invalid boolean value: {value}")


def _parse_case_spec(spec: str) -> dict[str, Any]:
    fields: dict[str, str] = {}
    for part in spec.split(","):
        if "=" not in part:
            raise argparse.ArgumentTypeError(f"invalid case field: {part}")
        key, value = part.split("=", 1)
        fields[key.strip()] = value.strip()
    if not fields.get("name"):
        raise argparse.ArgumentTypeError("case spec requires name")
    allowed = {"name", "chunk_size", "enable_chunk_overlap", "enable_tts_file_batch_results"}
    unknown = sorted(set(fields) - allowed)
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown case field(s): {', '.join(unknown)}")
    chunk_size = fields.get("chunk_size")
    return {
        "name": fields["name"],
        "chunk_size": int(chunk_size) if chunk_size not in {None, ""} else None,
        "enable_chunk_overlap": _parse_bool(fields.get("enable_chunk_overlap", "false")),
        "enable_tts_file_batch_results": _parse_bool(
            fields.get("enable_tts_file_batch_results", "false")
        ),
    }


def _artifact_pointer(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {"path": None, "exists": False}
    artifact_path = Path(path)
    return {"path": str(artifact_path), "exists": artifact_path.exists()}


def _run_artifacts(metrics: dict[str, Any], *, parse_only: bool) -> dict[str, Any]:
    artifacts = {
        "metrics_path": _artifact_pointer(metrics.get("metrics_path")),
    }
    if not parse_only:
        artifacts["output_path"] = _artifact_pointer(metrics.get("output_path"))
    profile = metrics.get("tts_chunk_profile") or {}
    artifacts["tts_chunk_profile"] = _artifact_pointer(profile.get("path"))
    return artifacts


def _metric_stats(values: list[float], threshold: dict[str, float] | None = None) -> dict[str, Any]:
    median = statistics.median(values)
    stdev = statistics.stdev(values) if len(values) > 1 else 0.0
    spread = max(values) - min(values)
    status = "pass"
    if threshold is not None:
        if "max" in threshold and median > threshold["max"]:
            status = "fail"
        if "min" in threshold and median < threshold["min"]:
            status = "fail"
    return {
        "values": [_round(value) for value in values],
        "median": _round(median),
        "min": _round(min(values)),
        "max": _round(max(values)),
        "mean": _round(statistics.mean(values)),
        "stdev": _round(stdev),
        "stdev_pct_of_median": _round((stdev / median) * 100) if median else None,
        "spread": _round(spread),
        "spread_pct_of_median": _round((spread / median) * 100) if median else None,
        "threshold": threshold,
        "status": status,
    }


def _punctuation_counts(text: str) -> dict[str, int]:
    return {
        "commas": text.count(","),
        "periods": text.count("."),
        "semicolons": text.count(";"),
        "colons": text.count(":"),
        "question_marks": text.count("?"),
        "exclamation_marks": text.count("!"),
    }


def analyze_tts_chunk_profile(path: str | Path | None) -> dict[str, Any]:
    if not path:
        return {"status": "not_requested", "path": None, "rows": 0, "chunks": []}
    profile_path = Path(path)
    if not profile_path.exists():
        return {
            "status": "missing",
            "path": str(profile_path),
            "rows": 0,
            "chunks": [],
            "message": f"profile not found: {profile_path}",
        }

    chunks: list[dict[str, Any]] = []
    parse_errors: list[str] = []
    for line_number, line in enumerate(profile_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            parse_errors.append(f"line {line_number}: {exc}")
            continue
        text = str(row.get("text_preview") or row.get("text") or "")
        text_chars = int(row.get("text_chars") or len(text) or 0)
        elapsed = float(row.get("elapsed_seconds") or 0.0)
        duration_ms = row.get("duration_ms")
        audio_seconds = (float(duration_ms) / 1000.0) if duration_ms is not None else None
        chunk = {
            "call_index": row.get("call_index"),
            "method": row.get("method"),
            "output_mode": row.get("output_mode"),
            "text_chars": text_chars,
            "elapsed_seconds": _round(elapsed),
            "audio_duration_seconds": _round(audio_seconds),
            "real_time_factor": _round(elapsed / audio_seconds) if audio_seconds else None,
            "chars_per_second": _round(text_chars / elapsed) if elapsed else None,
            "seconds_per_100_chars": _round((elapsed / text_chars) * 100) if text_chars else None,
            "punctuation": _punctuation_counts(text),
            "text_clues": {
                "ends_with_punctuation": text.rstrip().endswith((".", "!", "?", ";", ":")),
                "contains_quote": '"' in text or "'" in text,
                "contains_newline": "\n" in text,
            },
        }
        chunks.append(chunk)

    return {
        "status": "ok" if not parse_errors else "partial",
        "path": str(profile_path),
        "rows": len(chunks),
        "parse_errors": parse_errors,
        "chunks": chunks,
        "slowest_by_elapsed": sorted(
            chunks,
            key=lambda item: item.get("elapsed_seconds") or 0,
            reverse=True,
        )[:5],
        "highest_real_time_factor": sorted(
            chunks,
            key=lambda item: item.get("real_time_factor") or 0,
            reverse=True,
        )[:5],
    }


def _build_provenance(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_path": str(Path(args.source).expanduser().resolve()),
        "command": [str(Path(__file__)), *sys.argv[1:]],
        "environment": {
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "executable": sys.executable,
        },
    }


def _summarize_cases(
    cases: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    thresholds: dict[str, dict[str, float]],
) -> list[dict[str, Any]]:
    summaries = []
    for case in cases:
        case_runs = [run for run in runs if run["case_name"] == case["name"]]
        metrics: dict[str, Any] = {}
        for key in METRIC_KEYS:
            values = [
                float(run["metrics"][key])
                for run in case_runs
                if isinstance(run["metrics"].get(key), (int, float))
            ]
            if values:
                metrics[key] = _metric_stats(values, thresholds.get(key))
        summaries.append(
            {
                "name": case["name"],
                "runs": len(case_runs),
                "settings": {
                    "chunk_size": case["chunk_size"],
                    "enable_chunk_overlap": case["enable_chunk_overlap"],
                    "enable_tts_file_batch_results": case["enable_tts_file_batch_results"],
                },
                "metrics": metrics,
                "status": "fail"
                if any(metric.get("status") == "fail" for metric in metrics.values())
                else "pass",
            }
        )
    return summaries


def _build_validity(summary: dict[str, Any]) -> dict[str, Any]:
    provenance = summary.get("provenance") or {}
    missing_provenance = [
        key
        for key in ("created_at", "source_path", "command", "environment")
        if not provenance.get(key)
    ]
    missing_artifacts = []
    for run in summary.get("runs") or []:
        case_name = run.get("case_name") or "<unnamed>"
        repetition = run.get("repetition") or "?"
        for key, artifact in (run.get("artifacts") or {}).items():
            if key == "tts_chunk_profile" and not artifact.get("path"):
                continue
            if not artifact.get("exists"):
                missing_artifacts.append(f"{case_name}/r{repetition}/{key}")
    variance_violations = []
    for case in summary.get("case_summaries") or []:
        case_name = case.get("name") or "<unnamed>"
        wall = (case.get("metrics") or {}).get("wall_time_seconds") or {}
        spread_pct = wall.get("spread_pct_of_median")
        if isinstance(spread_pct, (int, float)) and spread_pct > MAX_SPREAD_PCT_OF_MEDIAN:
            variance_violations.append(f"{case_name}/wall_time_seconds")
    repetitions = int(summary.get("repetitions") or 0)
    gates = {
        "artifacts": {
            "status": "pass" if not missing_artifacts else "fail",
            "missing": missing_artifacts,
        },
        "provenance": {
            "status": "pass" if not missing_provenance else "fail",
            "missing": missing_provenance,
        },
        "repetitions": {
            "status": "pass" if repetitions >= MIN_REPETITIONS_FOR_VALIDITY else "fail",
            "minimum": MIN_REPETITIONS_FOR_VALIDITY,
            "actual": repetitions,
        },
        "thresholds": {
            "status": "pass",
            "missing": [],
        },
        "variance": {
            "status": "pass" if not variance_violations else "fail",
            "max_spread_pct_of_median": MAX_SPREAD_PCT_OF_MEDIAN,
            "violations": variance_violations,
        },
    }
    return {
        "status": "pass"
        if all(gate["status"] == "pass" for gate in gates.values())
        else "fail",
        "gates": gates,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("--parse-only", action="store_true")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--output-root", type=Path, default=Path("output/nagel_repeated"))
    parser.add_argument("--temp-root", type=Path, default=Path("temp/nagel_repeated"))
    parser.add_argument("--chunk-size", type=int, default=700)
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        help=(
            "Explicit benchmark case, e.g. "
            "name=chunk_700,chunk_size=700,enable_chunk_overlap=false"
        ),
    )
    parser.add_argument("--chapter-limit", type=int, default=None)
    parser.add_argument("--min-extracted-chars", type=int, default=1)
    parser.add_argument("--max-median-wall-seconds", type=float, default=None)
    parser.add_argument("--profile-tts-chunks", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_root = args.output_root
    temp_root = args.temp_root
    output_root.mkdir(parents=True, exist_ok=True)
    temp_root.mkdir(parents=True, exist_ok=True)
    cases = _case_matrix(args)
    parse_only = args.parse_only or not args.full
    runs: list[dict[str, Any]] = []

    for repetition in range(1, args.repetitions + 1):
        for case in cases:
            run_output = output_root / case["name"] / f"r{repetition}" / "output"
            run_temp = temp_root / case["name"] / f"r{repetition}" / "temp"
            metrics = run_benchmark(
                source_path=Path(args.source),
                output_dir=run_output,
                temp_dir=run_temp,
                parse_only=parse_only,
                chunk_size=case["chunk_size"],
                enable_chunk_overlap=case["enable_chunk_overlap"],
                enable_tts_file_batch_results=case["enable_tts_file_batch_results"],
                min_extracted_chars=args.min_extracted_chars,
                chapter_limit=args.chapter_limit,
                profile_tts_chunks=args.profile_tts_chunks,
            )
            profile = metrics.get("tts_chunk_profile") or {}
            profile_path = profile.get("path")
            runs.append(
                {
                    "case_name": case["name"],
                    "repetition": repetition,
                    "parse_only": parse_only,
                    "success": metrics.get("success"),
                    "metrics": metrics,
                    "artifacts": _run_artifacts(metrics, parse_only=parse_only),
                    "profile_analysis": analyze_tts_chunk_profile(profile_path),
                }
            )

    summary_path = output_root / "repeated_benchmark_summary.json"
    thresholds: dict[str, dict[str, float]] = {}
    if args.max_median_wall_seconds is not None:
        thresholds["wall_time_seconds"] = {"max": args.max_median_wall_seconds}
    summary = {
        "schema_version": 1,
        "repetitions": args.repetitions,
        "parse_only": parse_only,
        "provenance": _build_provenance(args),
        "cases": cases,
        "runs": runs,
        "case_summaries": _summarize_cases(cases, runs, thresholds),
        "artifact_pointers": {"summary_path": str(summary_path)},
    }
    summary["validity"] = _build_validity(summary)
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    failed = any(case["status"] == "fail" for case in summary["case_summaries"])
    failed = failed or any(run.get("success") is False for run in runs)
    failed = failed or summary["validity"]["status"] == "fail"
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
