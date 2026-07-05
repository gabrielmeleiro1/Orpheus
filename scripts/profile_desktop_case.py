#!/usr/bin/env python3
"""Profile a single benchmark corpus case with per-chunk TTS timing enabled."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.benchmark_nagel_pipeline import run_benchmark  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--run-name", default="profile-case")
    parser.add_argument("--chapter-start", type=int, default=1)
    parser.add_argument("--chapter-limit", type=int, default=3)
    parser.add_argument("--cleaner-backend", default="cuda")
    parser.add_argument("--cleaner-cuda-device", default="cuda:0")
    parser.add_argument("--cleaner-cuda-quantization", default="4bit")
    parser.add_argument("--enable-chunk-overlap", action="store_true")
    parser.add_argument("--cleaner-always-llm", action="store_true")
    args = parser.parse_args()

    run_root = REPO_ROOT / ".artifacts" / "benchmarks" / args.run_name
    metrics = run_benchmark(
        source_path=args.source,
        output_dir=run_root / "output",
        temp_dir=run_root / "temp",
        parse_only=False,
        cleaner_backend=args.cleaner_backend,
        cleaner_cuda_device=args.cleaner_cuda_device,
        cleaner_cuda_quantization=args.cleaner_cuda_quantization,
        cleaner_skip_llm_when_not_needed=not args.cleaner_always_llm,
        chapter_start=args.chapter_start,
        chapter_limit=args.chapter_limit,
        enable_chapter_aac_packaging=True,
        enable_background_chapter_aac_encoding=True,
        enable_chunk_overlap=args.enable_chunk_overlap,
        profile_tts_chunks=True,
    )
    keep = (
        "success", "error", "wall_time_seconds", "processing_time_seconds",
        "parse_seconds", "clean_seconds_total", "tts_seconds_total",
        "tts_model_load_seconds_total", "encode_seconds_total",
        "package_seconds_total", "clean_tts_overlap_seconds_total",
        "total_duration_ms", "chunks", "chapters", "tts_provider",
        "tts_device", "tts_chunk_profile",
    )
    summary = {key: metrics.get(key) for key in keep}
    (run_root / "profile_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if metrics.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
