#!/usr/bin/env python3
"""Run a single-book Kokoro elapsed-time benchmark matrix.

Examples:
  Default MLX cleaner:
    python benchmarks/kokoro_single_book/run_kokoro_single_book.py book.epub

  CUDA cleaner on a specific GPU:
    python benchmarks/kokoro_single_book/run_kokoro_single_book.py book.epub \\
      --cleaner-backend cuda --cleaner-cuda-device cuda:0 --cleaner-cuda-quantization 8bit
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from modules.pipeline.orchestrator import PipelineConfig  # noqa: E402
from scripts.benchmark_nagel_pipeline import (  # noqa: E402
    CLEANER_BACKEND_CHOICES,
    CUDA_CLEANER_QUANTIZATION_CHOICES,
    _apply_cleaner_env,
    run_benchmark,
)


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in value.strip().lower()).strip("_")


def _case_name(
    *,
    chunk_size: int,
    file_results: bool,
    overlap: bool,
) -> str:
    prefix = "file_result" if file_results else "baseline_array"
    suffix = "_overlap" if overlap else ""
    return f"{prefix}_{chunk_size}{suffix}"


def _build_cases(
    chunk_sizes: list[int],
    include_overlap: bool,
    include_array_baselines: bool = False,
) -> list[dict[str, Any]]:
    normalized_chunk_sizes = list(dict.fromkeys(max(1, int(size)) for size in chunk_sizes))
    if not normalized_chunk_sizes:
        raise ValueError("At least one chunk size is required")

    cases: list[dict[str, Any]] = []

    for index, chunk_size in enumerate(normalized_chunk_sizes):
        if include_array_baselines or index == 0:
            cases.append(
                {
                    "name": _case_name(
                        chunk_size=int(chunk_size),
                        file_results=False,
                        overlap=False,
                    ),
                    "chunk_size": int(chunk_size),
                    "enable_tts_file_batch_results": False,
                    "enable_chunk_overlap": False,
                }
            )
        cases.append(
            {
                "name": _case_name(
                    chunk_size=int(chunk_size),
                    file_results=True,
                    overlap=False,
                ),
                "chunk_size": int(chunk_size),
                "enable_tts_file_batch_results": True,
                "enable_chunk_overlap": False,
            }
        )
        if include_overlap:
            cases.append(
                {
                    "name": _case_name(
                        chunk_size=int(chunk_size),
                        file_results=True,
                        overlap=True,
                    ),
                    "chunk_size": int(chunk_size),
                    "enable_tts_file_batch_results": True,
                    "enable_chunk_overlap": True,
                }
            )

    return cases


def _summary_case(metrics: dict[str, Any], case: dict[str, Any], repetition: int) -> dict[str, Any]:
    return {
        "name": case["name"],
        "repetition": repetition,
        "success": bool(metrics.get("success")),
        "wall_time_seconds": metrics.get("wall_time_seconds"),
        "processing_time_seconds": metrics.get("processing_time_seconds"),
        "tts_seconds_total": metrics.get("tts_seconds_total"),
        "clean_seconds_total": metrics.get("clean_seconds_total"),
        "clean_tts_overlap_seconds_total": metrics.get("clean_tts_overlap_seconds_total"),
        "encode_seconds_total": metrics.get("encode_seconds_total"),
        "package_seconds_total": metrics.get("package_seconds_total"),
        "chunks": metrics.get("chunks"),
        "chapters": metrics.get("chapters"),
        "chunk_size": case["chunk_size"],
        "enable_tts_file_batch_results": case["enable_tts_file_batch_results"],
        "enable_chunk_overlap": case["enable_chunk_overlap"],
        "output_path": metrics.get("output_path"),
        "metrics_path": metrics.get("metrics_path"),
        "error": metrics.get("error"),
    }


def _numeric_values(runs: list[dict[str, Any]], key: str) -> list[float]:
    values: list[float] = []
    for run in runs:
        value = run.get(key)
        if isinstance(value, int | float):
            values.append(float(value))
    return values


def _mean_or_none(values: list[float]) -> float | None:
    if not values:
        return None
    return round(statistics.fmean(values), 4)


def _median_or_none(values: list[float]) -> float | None:
    if not values:
        return None
    return round(float(statistics.median(values)), 4)


def _stdev_or_none(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    return round(float(statistics.stdev(values)), 4)


def _aggregate_case(case: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    wall_values = _numeric_values(runs, "wall_time_seconds")
    tts_values = _numeric_values(runs, "tts_seconds_total")
    processing_values = _numeric_values(runs, "processing_time_seconds")
    successful_runs = [run for run in runs if run.get("success")]
    return {
        "name": case["name"],
        "runs": len(runs),
        "successful_runs": len(successful_runs),
        "success": len(successful_runs) == len(runs),
        "chunk_size": case["chunk_size"],
        "enable_tts_file_batch_results": case["enable_tts_file_batch_results"],
        "enable_chunk_overlap": case["enable_chunk_overlap"],
        "chunks": successful_runs[-1].get("chunks") if successful_runs else None,
        "wall_time_seconds_mean": _mean_or_none(wall_values),
        "wall_time_seconds_median": _median_or_none(wall_values),
        "wall_time_seconds_min": min(wall_values) if wall_values else None,
        "wall_time_seconds_max": max(wall_values) if wall_values else None,
        "wall_time_seconds_stdev": _stdev_or_none(wall_values),
        "processing_time_seconds_mean": _mean_or_none(processing_values),
        "tts_seconds_total_mean": _mean_or_none(tts_values),
        "tts_seconds_total_median": _median_or_none(tts_values),
        "tts_seconds_total_min": min(tts_values) if tts_values else None,
        "metrics_paths": [run.get("metrics_path") for run in runs],
        "errors": [run.get("error") for run in runs if run.get("error")],
    }


def _aggregate_cases(
    cases: list[dict[str, Any]],
    summary_cases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        _aggregate_case(
            case,
            [run for run in summary_cases if run["name"] == case["name"]],
        )
        for case in cases
    ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    defaults = PipelineConfig()
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("source", type=Path, help="PDF, EPUB, TXT, or Markdown source")
    parser.add_argument("--output-root", type=Path, default=Path("output/kokoro_single_book"))
    parser.add_argument("--temp-root", type=Path, default=Path("temp/kokoro_single_book"))
    parser.add_argument("--chunk-sizes", type=int, nargs="+", default=[500, 700, 900, 1100, 1300])
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--include-array-baselines", action="store_true")
    parser.add_argument("--include-overlap", action="store_true")
    parser.add_argument("--chapter-limit", type=int, default=None)
    parser.add_argument("--voice", default=defaults.voice)
    parser.add_argument(
        "--cleaner-backend",
        default=defaults.cleaner_backend,
        choices=CLEANER_BACKEND_CHOICES,
        help="Cleaner backend: mlx (Apple Silicon), cuda (NVIDIA GPU), or api (remote)",
    )
    parser.add_argument(
        "--cleaner-cuda-device",
        default=defaults.cleaner_cuda_device,
        help="CUDA device for cleaner when --cleaner-backend=cuda (also CLEANER_CUDA_DEVICE)",
    )
    parser.add_argument(
        "--cleaner-cuda-quantization",
        default=defaults.cleaner_cuda_quantization,
        choices=CUDA_CLEANER_QUANTIZATION_CHOICES,
        help="CUDA quant mode when --cleaner-backend=cuda (also CLEANER_CUDA_QUANTIZATION)",
    )
    parser.add_argument("--cleaner-model", default=None)
    parser.add_argument("--pdf-backend", default=defaults.pdf_extraction_backend)
    parser.add_argument("--min-extracted-chars", type=int, default=1)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _apply_cleaner_env(
        cleaner_backend=args.cleaner_backend,
        cleaner_cuda_device=args.cleaner_cuda_device,
        cleaner_cuda_quantization=args.cleaner_cuda_quantization,
    )
    source_path = args.source.expanduser().resolve()
    output_root = args.output_root
    temp_root = args.temp_root
    output_root.mkdir(parents=True, exist_ok=True)
    temp_root.mkdir(parents=True, exist_ok=True)

    cases = _build_cases(
        chunk_sizes=[int(size) for size in args.chunk_sizes],
        include_overlap=bool(args.include_overlap),
        include_array_baselines=bool(args.include_array_baselines),
    )
    summary_cases: list[dict[str, Any]] = []

    repetitions = max(1, int(args.repetitions))
    for repetition in range(1, repetitions + 1):
        for case in cases:
            case_name = str(case["name"])
            case_output_dir = output_root / _slug(case_name) / f"r{repetition}"
            case_temp_dir = temp_root / _slug(case_name) / f"r{repetition}"
            metrics = run_benchmark(
                source_path=source_path,
                output_dir=case_output_dir,
                temp_dir=case_temp_dir,
                parse_only=False,
                tts_engine="kokoro",
                tts_quantization="bf16",
                cleaner_backend=args.cleaner_backend,
                cleaner_cuda_device=args.cleaner_cuda_device,
                cleaner_cuda_quantization=args.cleaner_cuda_quantization,
                cleaner_model_name=args.cleaner_model,
                pdf_extraction_backend=args.pdf_backend,
                voice=args.voice,
                chunk_size=int(case["chunk_size"]),
                chapter_limit=args.chapter_limit,
                enable_parallel=False,
                max_parallel_chapters=1,
                parallel_worker_mode="auto",
                enable_chunk_overlap=bool(case["enable_chunk_overlap"]),
                enable_tts_file_batch_results=bool(case["enable_tts_file_batch_results"]),
                min_extracted_chars=args.min_extracted_chars,
            )
            summary_cases.append(_summary_case(metrics, case, repetition))

    case_summaries = _aggregate_cases(cases, summary_cases)
    successful_case_summaries = [case for case in case_summaries if case["success"]]
    fastest_mean = min(
        successful_case_summaries,
        key=lambda case: case["wall_time_seconds_mean"] or float("inf"),
        default=None,
    )
    fastest_min = min(
        successful_case_summaries,
        key=lambda case: case["wall_time_seconds_min"] or float("inf"),
        default=None,
    )

    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_path": str(source_path),
        "output_root": str(output_root),
        "temp_root": str(temp_root),
        "chunk_sizes": [case["chunk_size"] for case in cases if case["enable_tts_file_batch_results"] and not case["enable_chunk_overlap"]],
        "repetitions": repetitions,
        "fastest_by_mean_wall_time": fastest_mean,
        "fastest_by_min_wall_time": fastest_min,
        "cases": summary_cases,
        "case_summaries": case_summaries,
    }
    summary_path = output_root / "kokoro_single_book_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if all(case["success"] for case in summary_cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
