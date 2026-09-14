"""Provenance-rich diagnostics for the historical real-TTS workload."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import platform
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HISTORICAL_TEXT = "Hello world. This is a test of the TTS engine performance. " * 10
HISTORICAL_VOICE = "af_bella"
HISTORICAL_SPEED = 1.0
HISTORICAL_QUANTIZATION = "bf16"
DEFAULT_REPETITIONS = 7
MIN_REPETITIONS = 3
DEFAULT_WARMUPS = 2
MIN_WARMUPS = 1
DIAGNOSTIC_SCHEMA_VERSION = 1
_PACKAGES = (
    "mlx", "mlx-audio", "mlx-lm", "mlx-metal", "numpy", "soundfile",
    "misaki", "spacy", "transformers",
)


class DiagnosticError(RuntimeError):
    """Raised when a diagnostic cannot be configured or executed."""


def _integer(value: str, minimum: int, option: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{option} must be an integer") from exc
    if parsed < minimum:
        raise argparse.ArgumentTypeError(f"{option} must be at least {minimum}")
    return parsed


def validate_audio(audio: Any) -> tuple[bool, int, str | None]:
    """Return validity, sample count, and an explanatory failure, if any."""
    try:
        import numpy as np

        samples = np.asarray(audio)
    except Exception as exc:  # pragma: no cover - NumPy's exact error varies
        return False, 0, f"audio could not be converted to an array: {exc}"

    count = int(samples.size)
    if count == 0:
        return False, 0, "audio is empty"
    if samples.ndim != 1:
        return False, count, f"audio is not mono: shape={samples.shape}"
    if not np.issubdtype(samples.dtype, np.number):
        return False, count, f"audio dtype is not numeric: {samples.dtype}"
    if np.iscomplexobj(samples):
        return False, count, "audio samples are complex"
    if not bool(np.all(np.isfinite(samples))):
        return False, count, "audio contains non-finite samples"
    return True, count, None


def _package_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for package in _PACKAGES:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return versions


def _source_info(root: Path) -> dict[str, Any]:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=root, check=True, capture_output=True, text=True,
        ).stdout.strip())
        return {"revision": revision, "dirty": dirty}
    except (OSError, subprocess.CalledProcessError) as exc:
        return {"revision": None, "dirty": None, "error": str(exc)}


def _mlx_info() -> dict[str, Any]:
    try:
        import mlx.core as mx

        return {
            "available": True,
            "default_device": str(mx.default_device()),
            "device_info": mx.device_info(),
            "active_memory_bytes": mx.get_active_memory(),
            "peak_memory_bytes": mx.get_peak_memory(),
        }
    except (ImportError, AttributeError, RuntimeError) as exc:
        return {"available": False, "error": str(exc)}


def _engine_info(engine: Any) -> dict[str, Any]:
    # This is the fixed TTSEngine benchmark, so capture its concrete config.
    model_path = engine._model_path
    cached_revision = None
    try:
        from huggingface_hub import try_to_load_from_cache

        config_path = try_to_load_from_cache(model_path, "config.json")
        if isinstance(config_path, str):
            cached_revision = Path(config_path).parent.name
    except (OSError, ValueError):
        pass
    return {
        "model_path": model_path,
        "cached_model_revision": cached_revision,
        "config": asdict(engine.config),
    }


def _timed_synthesis(
    engine: Any, chunks: list[str], total_chars: int, repetition: int,
    clock: Callable[[], float],
) -> dict[str, Any]:
    """Time all chunk synthesis; validate every result after stopping the clock."""
    started = clock()
    audio: list[Any] = []
    error: str | None = None
    try:
        for chunk in chunks:
            audio.append(engine.synthesize(
                chunk, voice=HISTORICAL_VOICE, speed=HISTORICAL_SPEED,
            ))
    except Exception as exc:  # Keep failed repetitions visible in raw_timings.
        error = f"synthesis failed: {exc}"
    try:
        elapsed = float(clock() - started)
    except Exception:
        elapsed = float("nan")

    valid = error is None
    sample_count = 0
    errors: list[str] = []
    if error:
        errors.append(error)
    else:
        for result in audio:
            result_valid, count, validation_error = validate_audio(result)
            valid &= result_valid
            sample_count += count
            if validation_error:
                errors.append(validation_error)
    timing_valid = math.isfinite(elapsed) and elapsed > 0
    if not timing_valid:
        errors.append("elapsed time must be finite and positive")
    return {
        "repetition": repetition,
        "elapsed_seconds": elapsed if math.isfinite(elapsed) else None,
        "chars_per_second": total_chars / elapsed if valid and timing_valid else None,
        "timing_valid": timing_valid,
        "audio_valid": valid,
        "audio_samples": sample_count,
        "validation_error": "; ".join(errors) or None,
    }


def run_tts_diagnostic(
    *, repetitions: int = DEFAULT_REPETITIONS,
    warmups: int = DEFAULT_WARMUPS,
    engine_factory: Callable[[], Any] | None = None,
    clock: Callable[[], float] = time.perf_counter,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Run the fixed workload. Inject ``engine_factory`` and ``clock`` in tests."""
    if repetitions < MIN_REPETITIONS:
        raise DiagnosticError(f"repetitions must be at least {MIN_REPETITIONS}")
    if warmups < MIN_WARMUPS:
        raise DiagnosticError(f"warmups must be at least {MIN_WARMUPS}")

    from modules.tts.chunker import TextChunker

    chunks = TextChunker().chunk(HISTORICAL_TEXT)
    if not chunks:
        raise DiagnosticError("historical workload produced no chunks")
    total_chars = sum(map(len, chunks))
    effective_text = "".join(chunks)

    if engine_factory is None:
        def engine_factory() -> Any:  # type: ignore[no-redef]
            from modules.tts.engine import TTSEngine, TTSConfig

            return TTSEngine(TTSConfig(quantization=HISTORICAL_QUANTIZATION))

    engine = engine_factory()
    try:
        engine.load_model()
        for _ in range(warmups):
            for chunk in chunks:
                engine.synthesize(
                    chunk, voice=HISTORICAL_VOICE, speed=HISTORICAL_SPEED,
                )
        raw_timings = [
            _timed_synthesis(engine, chunks, total_chars, repetition, clock)
            for repetition in range(1, repetitions + 1)
        ]
        engine_info = _engine_info(engine)
        mlx_info = _mlx_info()
    finally:
        engine.unload_model(keep_in_cache=False)

    throughputs = [
        row["chars_per_second"] for row in raw_timings
        if row["chars_per_second"] is not None
    ]
    complete = len(throughputs) == repetitions
    median = statistics.median(throughputs) if complete else None
    spread = max(throughputs) - min(throughputs) if complete else None
    valid_audio = sum(row["audio_valid"] for row in raw_timings)
    failures = [
        {"repetition": row["repetition"], "error": row["validation_error"]}
        for row in raw_timings if row["validation_error"]
    ]
    root = repo_root or Path(__file__).resolve().parents[2]
    return {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "diagnostic_status": "complete" if complete else "invalid",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "workload": {
            "raw_text_chars": len(HISTORICAL_TEXT),
            "text_chars": total_chars,
            "chunk_count": len(chunks),
            "chunk_lengths": [len(chunk) for chunk in chunks],
            "text_sha256": hashlib.sha256(effective_text.encode()).hexdigest(),
            "voice": HISTORICAL_VOICE,
            "speed": HISTORICAL_SPEED,
            "quantization": HISTORICAL_QUANTIZATION,
            "repetitions": repetitions,
            "warmups": warmups,
        },
        "raw_timings": raw_timings,
        "throughput": {
            "median_chars_per_second": median,
            "spread_chars_per_second": spread,
            "spread_percent_of_median": 100 * spread / median if median else None,
            "valid_repetitions": len(throughputs),
        },
        "audio": {
            "timed_runs": repetitions,
            "valid_nonempty_finite_runs": valid_audio,
            "invalid_runs": repetitions - valid_audio,
            "valid_sample_count_total": sum(
                row["audio_samples"] for row in raw_timings if row["audio_valid"]
            ),
        },
        "failures": failures,
        "engine": engine_info,
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
        },
        "platform": {
            "system": platform.system(), "release": platform.release(),
            "version": platform.version(), "machine": platform.machine(),
            "platform": platform.platform(),
        },
        "packages": _package_versions(),
        "mlx_device": mlx_info,
        "source": _source_info(root),
    }


def write_report(report: dict[str, Any], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the fixed historical real-TTS diagnostic.")
    parser.add_argument(
        "--repetitions", type=lambda value: _integer(value, MIN_REPETITIONS, "--repetitions"),
        default=DEFAULT_REPETITIONS,
        help=f"Timed repetitions (minimum {MIN_REPETITIONS}; default {DEFAULT_REPETITIONS}).",
    )
    parser.add_argument(
        "--warmups", type=lambda value: _integer(value, MIN_WARMUPS, "--warmups"),
        default=DEFAULT_WARMUPS,
        help=f"Warmup repetitions (minimum {MIN_WARMUPS}; default {DEFAULT_WARMUPS}).",
    )
    parser.add_argument("--output", type=Path, required=True, help="Required JSON report path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_tts_diagnostic(repetitions=args.repetitions, warmups=args.warmups)
    write_report(report, args.output)
    print(f"Wrote TTS diagnostic report to {args.output}")
    return 0 if report["diagnostic_status"] == "complete" else 1
