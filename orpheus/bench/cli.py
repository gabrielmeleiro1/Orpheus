from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orpheus.bench.metrics import skipped_sections_from_metrics
from orpheus.bench.presets import SUPPORTED_PRESETS, get_preset
from orpheus.bench.report import ReportError, write_report_outputs

DEFAULT_OUTPUT_ROOT = Path(".artifacts/benchmarks")
DEFAULT_TEMP_ROOT = DEFAULT_OUTPUT_ROOT / "temp"
SUMMARY_FILENAME = "bench_summary.json"
SUMMARY_SCHEMA_VERSION = 1
SUPPORTED_MODES = ("parse", "clean", "full")


def _load_benchmark_runner() -> Any:
    with contextlib.redirect_stdout(io.StringIO()):
        from scripts import benchmark_nagel_pipeline

    return benchmark_nagel_pipeline


def run_benchmark(**kwargs: Any) -> dict[str, Any]:
    return _load_benchmark_runner().run_benchmark(**kwargs)


def _invoke_run_benchmark(**kwargs: Any) -> dict[str, Any]:
    from orpheus import bench

    return bench.run_benchmark(**kwargs)


def cleaner_backend_choices() -> tuple[str, ...]:
    return _load_benchmark_runner().cleaner_backend_choices()


def cleaner_cuda_quantization_choices() -> tuple[str, ...]:
    return _load_benchmark_runner().cleaner_cuda_quantization_choices()


@dataclass(frozen=True)
class RunSpec:
    command: str
    source_path: Path
    label: str


def _source_label(source_path: Path) -> str:
    label = re.sub(r"[^A-Za-z0-9_.-]+", "_", source_path.stem).strip("._-")
    return label or "source"


def _mode_flags(command: str) -> dict[str, bool]:
    if command == "parse":
        return {"parse_only": True, "cleaner_gate": False}
    if command == "clean":
        return {"parse_only": False, "cleaner_gate": True}
    if command == "full":
        return {"parse_only": False, "cleaner_gate": False}
    raise ValueError(f"unsupported benchmark command: {command}")


def _run_output_dir(output_root: Path, spec: RunSpec, index: int, *, suite: bool) -> Path:
    if suite:
        return output_root / f"{index:02d}-{spec.label}"
    return output_root / spec.command / spec.label


def _reset_run_dir(path: Path) -> None:
    if path.exists():
        if not path.is_dir():
            raise ValueError(f"benchmark path exists and is not a directory: {path}")
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _metrics_path(metrics: dict[str, Any], output_dir: Path) -> str:
    path = metrics.get("metrics_path")
    return str(path) if path else str(output_dir / "benchmark_summary.json")


def _audio_duration_seconds(metrics: dict[str, Any]) -> float | None:
    duration_ms = metrics.get("total_duration_ms")
    if not isinstance(duration_ms, int | float) or duration_ms <= 0:
        return None
    return round(float(duration_ms) / 1000.0, 4)


def _audio_realtime_factor(metrics: dict[str, Any]) -> float | None:
    audio_seconds = _audio_duration_seconds(metrics)
    wall_seconds = metrics.get("wall_time_seconds")
    if (
        audio_seconds is None
        or not isinstance(wall_seconds, int | float)
        or wall_seconds <= 0
    ):
        return None
    return round(audio_seconds / float(wall_seconds), 4)


def _summary_run(
    *,
    spec: RunSpec,
    output_dir: Path,
    temp_dir: Path,
    metrics: dict[str, Any],
) -> dict[str, Any]:
    return {
        "command": spec.command,
        "label": spec.label,
        "source_path": str(spec.source_path.expanduser().resolve()),
        "mode": metrics.get("mode"),
        "success": bool(metrics.get("success")),
        "metrics_path": _metrics_path(metrics, output_dir),
        "output_dir": str(output_dir),
        "temp_dir": str(temp_dir),
        "wall_time_seconds": metrics.get("wall_time_seconds"),
        "parse_seconds": metrics.get("parse_seconds"),
        "clean_seconds_total": metrics.get("clean_seconds_total"),
        "tts_provider": metrics.get("tts_provider"),
        "tts_device": metrics.get("tts_device"),
        "tts_seconds_total": metrics.get("tts_seconds_total"),
        "audio_duration_seconds": _audio_duration_seconds(metrics),
        "audio_realtime_factor": _audio_realtime_factor(metrics),
        "extracted_chars": metrics.get("extracted_chars"),
        "chapters": metrics.get("chapters"),
        "chunks": metrics.get("chunks"),
        "cleaner_gate_rule_only_chunks": metrics.get("cleaner_gate_rule_only_chunks"),
        "cleaner_gate_llm_needed_chunks": metrics.get("cleaner_gate_llm_needed_chunks"),
        "skipped_non_prose_sections": skipped_sections_from_metrics(metrics),
        "artifact_quality": metrics.get("artifact_quality"),
        "error": metrics.get("error"),
    }


def _write_summary(
    *,
    command: str,
    output_root: Path,
    temp_root: Path,
    runs: list[dict[str, Any]],
    started_at: float,
) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    summary_path = output_root / SUMMARY_FILENAME
    summary = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "command": command,
        "status": "pass" if all(run["success"] for run in runs) else "fail",
        "created_unix_seconds": round(time.time(), 3),
        "wall_time_seconds": round(max(0.0, time.perf_counter() - started_at), 4),
        "output_root": str(output_root),
        "temp_root": str(temp_root),
        "summary_path": str(summary_path),
        "runs": runs,
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def _apply_preset_defaults(args: argparse.Namespace, defaults: dict[str, Any]) -> None:
    for key, value in defaults.items():
        attr = key.replace("-", "_")
        setattr(args, attr, value)


def _execute_specs(
    *,
    command: str,
    specs: list[RunSpec],
    args: argparse.Namespace,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    output_root = Path(args.output_root)
    temp_root = Path(args.temp_root)
    output_root.mkdir(parents=True, exist_ok=True)
    temp_root.mkdir(parents=True, exist_ok=True)

    runs: list[dict[str, Any]] = []
    suite_like = command in {"suite", *SUPPORTED_PRESETS}
    for index, spec in enumerate(specs, start=1):
        output_dir = _run_output_dir(output_root, spec, index, suite=suite_like)
        temp_dir = temp_root / output_dir.relative_to(output_root)
        _reset_run_dir(output_dir)
        _reset_run_dir(temp_dir)
        flags = _mode_flags(spec.command)
        with contextlib.redirect_stdout(sys.stderr):
            metrics = _invoke_run_benchmark(
                source_path=spec.source_path,
                output_dir=output_dir,
                temp_dir=temp_dir,
                parse_only=flags["parse_only"],
                cleaner_gate=flags["cleaner_gate"],
                cleaner_backend=args.cleaner_backend,
                cleaner_model_name=args.cleaner_model,
                cleaner_cuda_device=args.cleaner_cuda_device,
                cleaner_cuda_quantization=args.cleaner_cuda_quantization,
                cleaner_skip_llm_when_not_needed=not args.cleaner_always_llm,
                pdf_extraction_backend=args.pdf_extraction_backend,
                voice=args.voice,
                chunk_size=args.chunk_size,
                chapter_limit=args.chapter_limit,
                enable_chunk_overlap=args.enable_chunk_overlap,
                min_extracted_chars=args.min_extracted_chars,
                enable_parallel=not args.no_parallel,
                max_parallel_chapters=args.max_parallel_chapters,
                parallel_worker_mode=args.parallel_worker_mode,
                profile_tts_chunks=args.profile_tts_chunks,
            )
        runs.append(
            _summary_run(
                spec=spec,
                output_dir=output_dir,
                temp_dir=temp_dir,
                metrics=metrics,
            )
        )

    return _write_summary(
        command=command,
        output_root=output_root,
        temp_root=temp_root,
        runs=runs,
        started_at=started_at,
    )


def _parse_modes(value: str) -> tuple[str, ...]:
    modes = tuple(mode.strip() for mode in value.split(",") if mode.strip())
    if not modes:
        raise argparse.ArgumentTypeError("at least one mode is required")
    unknown = [mode for mode in modes if mode not in SUPPORTED_MODES]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unsupported mode(s): {', '.join(unknown)}; expected parse, clean, or full"
        )
    return modes


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--temp-root", type=Path, default=DEFAULT_TEMP_ROOT)
    parser.add_argument("--chunk-size", type=int, default=None)
    parser.add_argument("--chapter-limit", type=int, default=None)
    parser.add_argument("--min-extracted-chars", type=int, default=1)
    parser.add_argument("--pdf-extraction-backend", default="fitz")
    parser.add_argument("--voice", default=None)
    parser.add_argument("--cleaner-model", default=None)
    parser.add_argument(
        "--cleaner-backend",
        default=None,
        choices=cleaner_backend_choices(),
    )
    parser.add_argument("--cleaner-cuda-device", default=None)
    parser.add_argument(
        "--cleaner-cuda-quantization",
        default=None,
        choices=cleaner_cuda_quantization_choices(),
    )
    parser.add_argument("--cleaner-always-llm", action="store_true")
    parser.add_argument(
        "--enable-chunk-overlap",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    parser.add_argument("--no-parallel", action="store_true")
    parser.add_argument("--max-parallel-chapters", type=int, default=2)
    parser.add_argument(
        "--parallel-worker-mode",
        choices=("auto", "manual"),
        default="auto",
    )
    parser.add_argument("--profile-tts-chunks", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orpheus bench",
        description="Run empirical Orpheus benchmarks against local sources.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in SUPPORTED_MODES:
        mode_parser = subparsers.add_parser(command)
        mode_parser.add_argument("source", type=Path)
        _add_common_options(mode_parser)

    suite_parser = subparsers.add_parser("suite")
    suite_parser.add_argument("sources", type=Path, nargs="+")
    suite_parser.add_argument("--modes", type=_parse_modes, default=("parse", "clean"))
    _add_common_options(suite_parser)

    for preset_name in SUPPORTED_PRESETS:
        preset_parser = subparsers.add_parser(preset_name)
        _add_common_options(preset_parser)

    compare_parser = subparsers.add_parser(
        "compare",
        help="Compare repeated benchmark summaries and write a regression report.",
    )
    compare_parser.add_argument("baseline", type=Path)
    compare_parser.add_argument("current", type=Path)
    compare_parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory for report.json and report.md (default: <current>/report).",
    )

    report_parser = subparsers.add_parser(
        "report",
        help="Build a performance regression report from a repeated benchmark summary.",
    )
    report_parser.add_argument("summary_path", type=Path)
    report_parser.add_argument("--baseline", type=Path)
    report_parser.add_argument("--output-dir", type=Path, required=True)

    return parser


def _specs_from_args(args: argparse.Namespace) -> list[RunSpec]:
    if args.command == "suite":
        return [
            RunSpec(command=mode, source_path=source, label=_source_label(source))
            for source in args.sources
            for mode in args.modes
        ]
    if args.command in SUPPORTED_PRESETS:
        preset = get_preset(args.command)
        _apply_preset_defaults(args, preset.defaults)
        return [
            RunSpec(command=mode, source_path=source, label=_source_label(source))
            for source in preset.sources
            for mode in preset.modes
        ]
    source = Path(args.source)
    return [RunSpec(command=args.command, source_path=source, label=_source_label(source))]


def _run_compare(args: argparse.Namespace) -> int:
    output_dir = args.output_dir or (args.current.parent / "report")
    try:
        report = write_report_outputs(
            summary_path=args.current,
            output_dir=output_dir,
            baseline_path=args.baseline,
        )
    except (OSError, json.JSONDecodeError, ReportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report["status"] == "fail" else 0


def _run_report(args: argparse.Namespace) -> int:
    try:
        report = write_report_outputs(
            summary_path=args.summary_path,
            output_dir=args.output_dir,
            baseline_path=args.baseline,
        )
    except (OSError, json.JSONDecodeError, ReportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report["status"] == "fail" else 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "compare":
        return _run_compare(args)
    if args.command == "report":
        return _run_report(args)
    specs = _specs_from_args(args)
    summary = _execute_specs(command=args.command, specs=specs, args=args)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
