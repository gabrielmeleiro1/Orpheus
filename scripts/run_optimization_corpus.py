#!/usr/bin/env python3
"""Run the standard optimization benchmark corpus and write a summary JSON."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import statistics
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.benchmark_nagel_pipeline import run_benchmark  # noqa: E402

CORPUS_CASES = (
    {
        "book_key": "self-reliance",
        "source_env": "ORPHEUS_BENCH_SELF_RELIANCE",
        "default_source": r"C:\Users\iavas\Documents\Books\self-reliance.pdf",
    },
    {
        "book_key": "notes-underground",
        "source_env": "ORPHEUS_BENCH_NOTES_UNDERGROUND",
        "default_source": r"C:\Users\iavas\Documents\Books\notes-underground.pdf",
    },
    {
        "book_key": "frederick-second",
        "source_env": "ORPHEUS_BENCH_FREDERICK_SECOND",
        "default_source": r"C:\Users\iavas\Documents\Books\frederick-second.epub",
    },
)


def _resolve_source(case: dict[str, str]) -> Path:
    override = os.environ.get(case["source_env"], "").strip()
    path = Path(override or case["default_source"])
    if not path.exists():
        raise FileNotFoundError(f"benchmark source not found: {path}")
    return path.resolve()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_expected_source_hashes(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("expected source hash manifest must be a JSON object")
    hashes: dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("expected source hash manifest entries must be strings")
        hashes[key] = value.lower()
    return hashes


def _validate_expected_source_hash_manifest(
    expected_hashes: dict[str, str],
    corpus_cases: tuple[dict[str, str], ...] | None = None,
) -> None:
    """Require expected hashes for every configured corpus case."""
    if not expected_hashes:
        return
    if corpus_cases is None:
        corpus_cases = CORPUS_CASES
    expected_keys = {case["book_key"] for case in corpus_cases}
    manifest_keys = set(expected_hashes)
    missing = sorted(expected_keys - manifest_keys)
    extra = sorted(manifest_keys - expected_keys)
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append(f"missing keys: {', '.join(missing)}")
        if extra:
            details.append(f"unknown keys: {', '.join(extra)}")
        raise ValueError(
            "expected source hash manifest must exactly match corpus book keys "
            f"({'; '.join(details)})"
        )


def _validate_source_hash(
    *,
    book_key: str,
    source_path: Path,
    expected_hashes: dict[str, str],
    allow_mismatch: bool,
) -> str:
    source_hash = _sha256_file(source_path)
    expected_hash = expected_hashes.get(book_key)
    if expected_hash and source_hash.lower() != expected_hash:
        message = (
            f"{book_key} source hash mismatch: expected {expected_hash}, "
            f"got {source_hash} for {source_path}"
        )
        if not allow_mismatch:
            raise ValueError(message)
        print(f"WARNING: {message}", file=sys.stderr)
    return source_hash


def _extract_case_metrics(metrics: dict[str, Any], *, book_key: str, chapter_limit: int) -> dict[str, Any]:
    artifact_quality = metrics.get("artifact_quality") or {}
    return {
        "book_key": book_key,
        "chapter_start": metrics.get("chapter_start"),
        "chapter_limit": chapter_limit,
        "chapter_titles": metrics.get("chapter_titles", []),
        "chunk_size": metrics.get("chunk_size"),
        "wall_time_seconds": metrics.get("wall_time_seconds"),
        "processing_time_seconds": metrics.get("processing_time_seconds"),
        "parse_seconds": metrics.get("parse_seconds"),
        "clean_seconds_total": metrics.get("clean_seconds_total"),
        "tts_seconds_total": metrics.get("tts_seconds_total"),
        "package_seconds_total": metrics.get("package_seconds_total"),
        "chunks": metrics.get("chunks"),
        "cleaned_chars": artifact_quality.get("cleaned_chars"),
        "artifact_quality": {
            "success": artifact_quality.get("success"),
            "issues": artifact_quality.get("issues", []),
        },
        "success": metrics.get("success"),
        "error": metrics.get("error"),
        "output_path": metrics.get("output_path"),
        "source_hash": metrics.get("source_hash"),
        "source_path": metrics.get("source_path"),
        "metrics_path": metrics.get("metrics_path"),
    }


def _aggregate(cases: list[dict[str, Any]]) -> dict[str, Any]:
    wall_times = [
        float(case["wall_time_seconds"])
        for case in cases
        if isinstance(case.get("wall_time_seconds"), (int, float))
    ]
    quality_pass = sum(
        1
        for case in cases
        if case.get("success") is True
        and (case.get("artifact_quality") or {}).get("success") is True
    )
    issue_count = sum(
        len((case.get("artifact_quality") or {}).get("issues") or []) for case in cases
    )
    return {
        "case_count": len(cases),
        "success_count": sum(1 for case in cases if case.get("success") is True),
        "quality_pass_count": quality_pass,
        "artifact_issue_count": issue_count,
        "wall_time_seconds": {
            "median": round(statistics.median(wall_times), 4) if wall_times else None,
            "mean": round(statistics.mean(wall_times), 4) if wall_times else None,
            "worst": round(max(wall_times), 4) if wall_times else None,
            "total": round(sum(wall_times), 4) if wall_times else None,
        },
    }


def _repeat_aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    totals = [
        float(((run.get("aggregate") or {}).get("wall_time_seconds") or {}).get("total"))
        for run in runs
        if isinstance(((run.get("aggregate") or {}).get("wall_time_seconds") or {}).get("total"), (int, float))
    ]
    case_count = sum(int((run.get("aggregate") or {}).get("case_count") or 0) for run in runs)
    success_count = sum(
        int((run.get("aggregate") or {}).get("success_count") or 0) for run in runs
    )
    quality_pass_count = sum(
        int((run.get("aggregate") or {}).get("quality_pass_count") or 0)
        for run in runs
    )
    artifact_issue_count = sum(
        int((run.get("aggregate") or {}).get("artifact_issue_count") or 0)
        for run in runs
    )
    return {
        "repeat_count": len(runs),
        "case_count": case_count,
        "success_count": success_count,
        "quality_pass_count": quality_pass_count,
        "artifact_issue_count": artifact_issue_count,
        "total_wall_time_seconds": {
            "median": round(statistics.median(totals), 4) if totals else None,
            "mean": round(statistics.mean(totals), 4) if totals else None,
            "best": round(min(totals), 4) if totals else None,
            "worst": round(max(totals), 4) if totals else None,
        },
    }


def run_corpus(
    *,
    run_name: str,
    chapter_start: int = 1,
    chapter_limit: int = 3,
    chunk_size: int | None = None,
    enable_chapter_aac_packaging: bool = False,
    enable_background_chapter_aac_encoding: bool = True,
    artifacts_root: Path | None = None,
    cleaner_backend: str = "cuda",
    cleaner_cuda_device: str = "cuda:0",
    cleaner_cuda_quantization: str = "4bit",
    expected_source_hashes: dict[str, str] | None = None,
    allow_source_hash_mismatch: bool = False,
) -> dict[str, Any]:
    artifacts_root = artifacts_root or (REPO_ROOT / ".artifacts" / "benchmarks")
    run_root = artifacts_root / run_name
    output_parent = run_root / "output"
    temp_parent = run_root / "temp"
    cases: list[dict[str, Any]] = []
    expected_source_hashes = expected_source_hashes or {}
    _validate_expected_source_hash_manifest(expected_source_hashes)

    for case_def in CORPUS_CASES:
        book_key = case_def["book_key"]
        source_path = _resolve_source(case_def)
        _validate_source_hash(
            book_key=book_key,
            source_path=source_path,
            expected_hashes=expected_source_hashes,
            allow_mismatch=allow_source_hash_mismatch,
        )
        output_dir = output_parent / book_key
        temp_dir = temp_parent / book_key
        shutil.rmtree(output_dir, ignore_errors=True)
        shutil.rmtree(temp_dir, ignore_errors=True)
        metrics = run_benchmark(
            source_path=source_path,
            output_dir=output_dir,
            temp_dir=temp_dir,
            parse_only=False,
            cleaner_backend=cleaner_backend,
            cleaner_cuda_device=cleaner_cuda_device,
            cleaner_cuda_quantization=cleaner_cuda_quantization,
            chapter_start=chapter_start,
            chapter_limit=chapter_limit,
            chunk_size=chunk_size,
            enable_chapter_aac_packaging=enable_chapter_aac_packaging,
            enable_background_chapter_aac_encoding=enable_background_chapter_aac_encoding,
        )
        cases.append(
            _extract_case_metrics(metrics, book_key=book_key, chapter_limit=chapter_limit)
        )

    summary = {
        "run_name": run_name,
        "chapter_start": chapter_start,
        "chapter_limit": chapter_limit,
        "chunk_size": chunk_size,
        "enable_chapter_aac_packaging": enable_chapter_aac_packaging,
        "enable_background_chapter_aac_encoding": enable_background_chapter_aac_encoding,
        "cleaner_backend": cleaner_backend,
        "cleaner_cuda_device": cleaner_cuda_device,
        "cleaner_cuda_quantization": cleaner_cuda_quantization,
        "cases": cases,
        "aggregate": _aggregate(cases),
    }
    run_root.mkdir(parents=True, exist_ok=True)
    summary_path = run_root / (
        "baseline_summary.json" if run_name == "optimization-baseline" else "summary.json"
    )
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    return summary


def run_repeated_corpus(
    *,
    repeat: int,
    run_name: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run the corpus repeatedly and summarize timing variance."""
    repeat = max(1, int(repeat))
    if repeat == 1:
        return run_corpus(run_name=run_name, **kwargs)

    artifacts_root = kwargs.get("artifacts_root") or (REPO_ROOT / ".artifacts" / "benchmarks")
    runs: list[dict[str, Any]] = []
    for index in range(1, repeat + 1):
        child_run_name = f"{run_name}-r{index:02d}"
        child_summary = run_corpus(run_name=child_run_name, **kwargs)
        runs.append(child_summary)

    summary = {
        "run_name": run_name,
        "repeat": repeat,
        "runs": runs,
        "aggregate": _repeat_aggregate(runs),
    }
    run_root = artifacts_root / run_name
    run_root.mkdir(parents=True, exist_ok=True)
    summary_path = run_root / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-name", default="optimization-baseline")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--chapter-start", type=int, default=1)
    parser.add_argument("--chapter-limit", type=int, default=3)
    parser.add_argument("--chunk-size", type=int, default=None)
    parser.add_argument("--enable-chapter-aac-packaging", action="store_true")
    parser.add_argument(
        "--enable-background-chapter-aac-encoding",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--artifacts-root",
        type=Path,
        default=REPO_ROOT / ".artifacts" / "benchmarks",
    )
    parser.add_argument("--cleaner-backend", default="cuda")
    parser.add_argument("--cleaner-cuda-device", default="cuda:0")
    parser.add_argument("--cleaner-cuda-quantization", default="4bit")
    parser.add_argument(
        "--expected-source-hashes",
        type=Path,
        help="JSON object mapping book_key to expected SHA-256 source hash.",
    )
    parser.add_argument(
        "--allow-source-hash-mismatch",
        action="store_true",
        help="Warn instead of failing when --expected-source-hashes does not match.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    summary = run_repeated_corpus(
        repeat=args.repeat,
        run_name=args.run_name,
        chapter_start=args.chapter_start,
        chapter_limit=args.chapter_limit,
        chunk_size=args.chunk_size,
        enable_chapter_aac_packaging=args.enable_chapter_aac_packaging,
        enable_background_chapter_aac_encoding=args.enable_background_chapter_aac_encoding,
        artifacts_root=args.artifacts_root,
        cleaner_backend=args.cleaner_backend,
        cleaner_cuda_device=args.cleaner_cuda_device,
        cleaner_cuda_quantization=args.cleaner_cuda_quantization,
        expected_source_hashes=_load_expected_source_hashes(args.expected_source_hashes),
        allow_source_hash_mismatch=args.allow_source_hash_mismatch,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    aggregate = summary["aggregate"]
    return 0 if aggregate["success_count"] == aggregate["case_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
