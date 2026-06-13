"""Compare sequential and forced chapter workers for one source.

This runner exists because multiprocessing spawn cannot use a ``python -``
stdin entrypoint. It is intentionally small and delegates to the real pipeline.

Examples:
  MLX cleaner baseline:
    python tools/benchmark_parallel_workers.py book.epub --workers 1 2 3

  CUDA cleaner on a specific GPU:
    python tools/benchmark_parallel_workers.py book.epub --workers 1 2 3 \\
      --cleaner-backend cuda --cleaner-cuda-device cuda:0 --cleaner-cuda-quantization 8bit
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from modules.pipeline.models import PipelineConfig  # noqa: E402
from modules.pipeline.orchestrator import ConversionPipeline  # noqa: E402
from scripts.benchmark_nagel_pipeline import (  # noqa: E402
    CLEANER_BACKEND_CHOICES,
    CUDA_CLEANER_QUANTIZATION_CHOICES,
    _BenchmarkForcedMemoryPolicy,
    _apply_cleaner_env,
)


def _run_case(
    *,
    source: Path,
    output_root: Path,
    temp_root: Path,
    label: str,
    workers: int,
    tts_engine: str,
    voice: str,
    cleaner_backend: str,
    cleaner_cuda_device: str,
    cleaner_cuda_quantization: str,
    chapter_limit: int | None,
) -> dict[str, Any]:
    config = PipelineConfig(
        output_dir=output_root / label,
        temp_dir=temp_root / label,
        tts_engine=tts_engine,
        voice=voice,
        cleaner_backend=cleaner_backend,
        cleaner_cuda_device=cleaner_cuda_device,
        cleaner_cuda_quantization=cleaner_cuda_quantization,
        cleaner_api_key="benchmark-dummy-key",
        cleaner_skip_llm_when_not_needed=True,
        chapter_limit=chapter_limit,
        enable_parallel=workers > 1,
        max_parallel_chapters=workers,
        requested_parallel_chapters=workers,
        parallel_worker_mode="manual",
    )
    if workers > 1:
        config.memory_policy = _BenchmarkForcedMemoryPolicy()

    pipeline = ConversionPipeline(config)
    started_at = time.perf_counter()
    result = pipeline.convert(source)
    wall_seconds = time.perf_counter() - started_at
    return {
        "label": label,
        "workers": workers,
        "success": bool(result.success),
        "wall_time_seconds": round(wall_seconds, 4),
        "duration_ms": int(result.total_duration_ms or 0),
        "error": result.error,
        "effective_parallel_chapters": int(
            getattr(pipeline, "_selected_parallel_chapters", workers) or workers
        ),
        "selected_worker_count_reason": getattr(
            pipeline,
            "_selected_parallel_worker_count_reason",
            None,
        ),
    }


def main() -> int:
    defaults = PipelineConfig()
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("--output-root", type=Path, default=Path("temp/worker_bench"))
    parser.add_argument("--temp-root", type=Path, default=Path("temp/worker_bench_tmp"))
    parser.add_argument("--tts-engine", default="kokoro")
    parser.add_argument("--voice", default="am_adam")
    parser.add_argument(
        "--cleaner-backend",
        default="mlx",
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
    parser.add_argument("--chapter-limit", type=int, default=None)
    parser.add_argument("--workers", type=int, nargs="+", default=[1, 2, 3])
    args = parser.parse_args()

    _apply_cleaner_env(
        cleaner_backend=args.cleaner_backend,
        cleaner_cuda_device=args.cleaner_cuda_device,
        cleaner_cuda_quantization=args.cleaner_cuda_quantization,
    )

    source = args.source.expanduser().resolve()
    output_root = args.output_root
    temp_root = args.temp_root
    output_root.mkdir(parents=True, exist_ok=True)
    temp_root.mkdir(parents=True, exist_ok=True)

    results = []
    baseline_wall: float | None = None
    for workers in args.workers:
        label = f"{args.tts_engine}_{workers}w"
        metrics = _run_case(
            source=source,
            output_root=output_root,
            temp_root=temp_root,
            label=label,
            workers=max(1, int(workers)),
            tts_engine=args.tts_engine,
            voice=args.voice,
            cleaner_backend=args.cleaner_backend,
            cleaner_cuda_device=args.cleaner_cuda_device,
            cleaner_cuda_quantization=args.cleaner_cuda_quantization,
            chapter_limit=args.chapter_limit,
        )
        if baseline_wall is None:
            baseline_wall = float(metrics["wall_time_seconds"])
        metrics["speedup_vs_first_percent"] = round(
            (baseline_wall - float(metrics["wall_time_seconds"]))
            / max(baseline_wall, 1e-9)
            * 100,
            2,
        )
        results.append(metrics)
        print(json.dumps(metrics, sort_keys=True), flush=True)

    summary = {"source": str(source), "results": results}
    (output_root / "parallel_worker_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return 0 if all(item["success"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
