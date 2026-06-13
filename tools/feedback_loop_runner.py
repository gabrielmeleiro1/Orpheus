#!/usr/bin/env python3
"""
Iterative feedback-loop runner for large-book conversion.

Runs conversion chapter-by-chapter, prints live logs, validates outputs after each
chapter, and stops immediately when a quality/runtime issue is detected.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.pipeline.orchestrator import ConversionPipeline, PipelineConfig  # noqa: E402


SUPPORTED_SOURCE_SUFFIXES = {".pdf", ".epub"}
CLEANER_BACKEND_CHOICES = ("mlx", "api", "cuda")
CUDA_CLEANER_QUANTIZATION_CHOICES = ("4bit", "8bit", "fp16")


def _apply_cleaner_env(
    *,
    cleaner_backend: str,
    cleaner_cuda_device: str,
    cleaner_cuda_quantization: str,
) -> None:
    os.environ["CLEANER_BACKEND"] = cleaner_backend
    os.environ["CLEANER_CUDA_DEVICE"] = cleaner_cuda_device
    os.environ["CLEANER_CUDA_QUANTIZATION"] = cleaner_cuda_quantization


@dataclass
class ChapterFeedback:
    chapter_number: int
    chapter_title: str
    success: bool
    issues: list[str]
    llm_success: int
    llm_fallback: int
    rule_only: int
    before_clean_path: Optional[str]
    llm_input_path: Optional[str]
    cleaning_trace_path: Optional[str]
    cleaned_path: Optional[str]
    mp3_path: Optional[str]


def _safe_title(value: str) -> str:
    return "".join(c if c.isalnum() or c in " -_" else "_" for c in value)


def resolve_source(source: Path, contains: Optional[str] = None) -> Path:
    if source.is_file():
        if source.suffix.lower() not in SUPPORTED_SOURCE_SUFFIXES:
            suffixes = ", ".join(sorted(SUPPORTED_SOURCE_SUFFIXES))
            raise ValueError(f"Unsupported source file: {source}. Expected one of: {suffixes}")
        return source

    if not source.is_dir():
        raise FileNotFoundError(f"Source path not found: {source}")

    candidates = sorted(
        [
            p
            for p in source.iterdir()
            if p.is_file() and p.suffix.lower() in SUPPORTED_SOURCE_SUFFIXES
        ]
    )
    if contains:
        lowered = contains.lower()
        filtered = [p for p in candidates if lowered in p.name.lower()]
        if filtered:
            candidates = filtered

    if not candidates:
        suffixes = ", ".join(sorted(SUPPORTED_SOURCE_SUFFIXES))
        raise FileNotFoundError(
            f"No source files found in directory: {source} (expected {suffixes})"
        )

    return candidates[0]


def analyze_cleaned_text(cleaned_text: str) -> list[str]:
    issues: list[str] = []
    lowered = cleaned_text.lower()

    if "<br" in lowered:
        issues.append("Found HTML line break tags (<br>) in cleaned text")

    if re.search(r"(?m)^\s*\|.+\|\s*$", cleaned_text):
        issues.append("Found markdown table rows in cleaned text")

    if re.search(r"\|\s*---+\s*\|", cleaned_text):
        issues.append("Found markdown table separator in cleaned text")

    return issues


def analyze_cleaner_stats(
    chapter_char_count: int,
    llm_success: int,
    llm_fallback: int,
    rule_only: int,
    strict_llm: bool,
) -> list[str]:
    issues: list[str] = []
    if llm_fallback > 0:
        issues.append(f"Cleaner fell back from LLM to rule-based mode ({llm_fallback} chunks)")

    if strict_llm and chapter_char_count >= 1200 and llm_success == 0:
        issues.append(
            "No successful LLM-cleaned chunks on a long chapter "
            f"({chapter_char_count} chars; rule_only={rule_only})"
        )

    return issues


def _snapshot_cleaner_stats(pipeline: ConversionPipeline) -> dict[str, int]:
    cleaner = pipeline._text_cleaner
    if cleaner is None:
        return {"llm_success": 0, "llm_fallback": 0, "rule_only": 0, "total_chunks": 0}
    stats_fn = getattr(cleaner, "stats_snapshot", None)
    if not callable(stats_fn):
        return {"llm_success": 0, "llm_fallback": 0, "rule_only": 0, "total_chunks": 0}
    return stats_fn()


def _delta(after: dict[str, int], before: dict[str, int], key: str) -> int:
    return after.get(key, 0) - before.get(key, 0)


def run_feedback_loop(args: argparse.Namespace) -> int:
    source_file = resolve_source(Path(args.source).expanduser().resolve(), args.contains)
    print(f"[runner] source: {source_file}")

    _apply_cleaner_env(
        cleaner_backend=args.cleaner_backend,
        cleaner_cuda_device=args.cleaner_cuda_device,
        cleaner_cuda_quantization=args.cleaner_cuda_quantization,
    )

    config = PipelineConfig(
        tts_engine="kokoro",
        tts_model_name="mlx-community/Kokoro-82M",
        tts_quantization=args.tts_quantization,
        cleaner_model_name=args.cleaner_model,
        cleaner_backend=args.cleaner_backend,
        cleaner_cuda_device=args.cleaner_cuda_device,
        cleaner_cuda_quantization=args.cleaner_cuda_quantization,
        cleaner_api_base_url=args.cleaner_api_base_url,
        cleaner_api_key_env=args.cleaner_api_key_env,
        cleaner_api_model=args.cleaner_api_model,
        save_cleaning_debug=not args.no_cleaning_debug,
        voice=args.voice,
        speed=args.speed,
        output_dir=Path(args.output_dir).expanduser().resolve(),
        temp_dir=Path(args.temp_dir).expanduser().resolve(),
        enable_parallel=False,  # deterministic chapter-by-chapter feedback
        max_parallel_chapters=1,
    )

    def on_verbose(message: str, log_type: str) -> None:
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] [{log_type}] {message}")

    pipeline = ConversionPipeline(config=config, verbose_callback=on_verbose)
    feedback_rows: list[ChapterFeedback] = []

    try:
        document = pipeline._ingest(source_file)
        book_title = document.title or source_file.stem
        safe_title = _safe_title(book_title)
        book_output_dir = config.output_dir / safe_title
        chapters_dir = book_output_dir / "chapters"
        book_output_dir.mkdir(parents=True, exist_ok=True)
        chapters_dir.mkdir(parents=True, exist_ok=True)
        pipeline._save_intermediate_source(document, book_output_dir)
        pipeline._parallel_execution_active = False
        pipeline._warmup_models()

        chapters = document.chapters
        start_idx = max(0, args.start_chapter - 1)
        end_idx = len(chapters)
        if args.max_chapters is not None:
            end_idx = min(end_idx, start_idx + args.max_chapters)

        print(
            f"[runner] processing chapters {start_idx + 1}..{end_idx} "
            f"of {len(chapters)} (strict_llm={args.strict_llm})"
        )

        for idx in range(start_idx, end_idx):
            chapter = chapters[idx]
            before_stats = _snapshot_cleaner_stats(pipeline)
            result = pipeline._process_chapter(
                chapter=chapter,
                chapter_idx=idx,
                total_chapters=len(chapters),
                output_dir=chapters_dir,
            )
            after_stats = _snapshot_cleaner_stats(pipeline)

            llm_success = _delta(after_stats, before_stats, "llm_success")
            llm_fallback = _delta(after_stats, before_stats, "llm_fallback")
            rule_only = _delta(after_stats, before_stats, "rule_only")

            chapter_issues: list[str] = []
            if result.error:
                chapter_issues.append(f"Chapter pipeline error: {result.error}")

            cleaned_path = chapters_dir / f"chapter_{chapter.number:02d}_cleaned.md"
            before_clean_path = chapters_dir / f"chapter_{chapter.number:02d}_before_cleaning.md"
            llm_input_path = chapters_dir / f"chapter_{chapter.number:02d}_llm_input.md"
            cleaning_trace_path = chapters_dir / f"chapter_{chapter.number:02d}_cleaning_trace.jsonl"
            cleaned_text = ""
            if cleaned_path.exists():
                cleaned_text = cleaned_path.read_text(encoding="utf-8")
                chapter_issues.extend(analyze_cleaned_text(cleaned_text))
            else:
                chapter_issues.append(f"Missing cleaned markdown output: {cleaned_path}")

            chapter_issues.extend(
                analyze_cleaner_stats(
                    chapter_char_count=len(chapter.content),
                    llm_success=llm_success,
                    llm_fallback=llm_fallback,
                    rule_only=rule_only,
                    strict_llm=args.strict_llm,
                )
            )

            if not result.mp3_path or not Path(result.mp3_path).exists():
                chapter_issues.append("Missing MP3 output for chapter")

            feedback = ChapterFeedback(
                chapter_number=chapter.number,
                chapter_title=chapter.title,
                success=not chapter_issues,
                issues=chapter_issues,
                llm_success=llm_success,
                llm_fallback=llm_fallback,
                rule_only=rule_only,
                before_clean_path=str(before_clean_path) if before_clean_path.exists() else None,
                llm_input_path=str(llm_input_path) if llm_input_path.exists() else None,
                cleaning_trace_path=str(cleaning_trace_path) if cleaning_trace_path.exists() else None,
                cleaned_path=str(cleaned_path) if cleaned_path.exists() else None,
                mp3_path=str(result.mp3_path) if result.mp3_path else None,
            )
            feedback_rows.append(feedback)

            print(
                "[feedback] "
                f"chapter={chapter.number} "
                f"llm_success={llm_success} llm_fallback={llm_fallback} rule_only={rule_only} "
                f"issues={len(chapter_issues)}"
            )
            for issue in chapter_issues:
                print(f"  - {issue}")

            if chapter_issues:
                print(f"[runner] stopping on chapter {chapter.number} due to issues")
                break

        report_path = (
            Path(args.report).expanduser().resolve()
            if args.report
            else book_output_dir / "feedback_loop_report.json"
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_data = {
            "source": str(source_file),
            "processed_chapters": len(feedback_rows),
            "stopped_on_issue": any(not row.success for row in feedback_rows),
            "chapters": [asdict(row) for row in feedback_rows],
        }
        report_path.write_text(json.dumps(report_data, indent=2), encoding="utf-8")
        print(f"[runner] report: {report_path}")

        if any(not row.success for row in feedback_rows):
            return 2
        return 0
    finally:
        pipeline._cleanup_models()
        pipeline._cleanup_temp()


def build_parser() -> argparse.ArgumentParser:
    defaults = PipelineConfig()
    parser = argparse.ArgumentParser(
        description="Run iterative chapter-by-chapter conversion with quality gates.",
        epilog=(
            "Examples:\n"
            "  MLX cleaner (default):\n"
            "    python tools/feedback_loop_runner.py --source book.epub\n\n"
            "  CUDA cleaner on a specific GPU:\n"
            "    python tools/feedback_loop_runner.py --source book.epub \\\n"
            "      --cleaner-backend cuda --cleaner-cuda-device cuda:0 \\\n"
            "      --cleaner-cuda-quantization 8bit\n\n"
            "  Remote API cleaner:\n"
            "    python tools/feedback_loop_runner.py --source book.epub \\\n"
            "      --cleaner-backend api --cleaner-model gpt-4o-mini"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Source file (.pdf/.epub) or directory containing source files",
    )
    parser.add_argument(
        "--contains",
        default=None,
        help="If --source is a directory, prefer source files containing this text",
    )
    parser.add_argument(
        "--start-chapter",
        type=int,
        default=1,
        help="1-based chapter number to start from",
    )
    parser.add_argument(
        "--max-chapters",
        type=int,
        default=None,
        help="Maximum chapters to process in this run",
    )
    parser.add_argument(
        "--voice",
        default="am_adam",
        help="TTS voice id",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="Speech speed (0.5-2.0)",
    )
    parser.add_argument(
        "--tts-quantization",
        default="bf16",
        choices=["bf16", "8bit", "6bit", "4bit"],
        help="Kokoro quantization model",
    )
    parser.add_argument(
        "--cleaner-model",
        default="mlx-community/Llama-3.2-3B-Instruct-4bit",
        help="Cleaner model name (local model id or remote API model)",
    )
    parser.add_argument(
        "--cleaner-backend",
        default="mlx",
        choices=CLEANER_BACKEND_CHOICES,
        help="Cleaner backend: mlx (local MLX), cuda (local NVIDIA GPU), or api (remote)",
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
    parser.add_argument(
        "--cleaner-api-base-url",
        default="https://api.moonshot.cn/v1",
        help="OpenAI-compatible API base URL (used when --cleaner-backend=api)",
    )
    parser.add_argument(
        "--cleaner-api-key-env",
        default="KIMI_API_KEY",
        help="Environment variable that holds cleaner API key (backend=api)",
    )
    parser.add_argument(
        "--cleaner-api-model",
        default=None,
        help="Optional API model override (defaults to --cleaner-model)",
    )
    parser.add_argument(
        "--no-cleaning-debug",
        action="store_true",
        help="Disable saving before/llm_input/trace cleaner debug files",
    )
    parser.add_argument(
        "--output-dir",
        default=str(PROJECT_ROOT / "output"),
        help="Output directory",
    )
    parser.add_argument(
        "--temp-dir",
        default=str(PROJECT_ROOT / "temp" / "feedback_loop"),
        help="Temporary directory",
    )
    parser.add_argument(
        "--strict-llm",
        action="store_true",
        help="Fail chapters when long content has no successful LLM cleaning",
    )
    parser.add_argument(
        "--report",
        default=None,
        help="Optional path for JSON feedback report",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return run_feedback_loop(args)


if __name__ == "__main__":
    raise SystemExit(main())
