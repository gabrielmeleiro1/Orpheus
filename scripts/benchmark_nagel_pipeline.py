#!/usr/bin/env python3
"""Benchmark the backend audiobook pipeline on real source files.

The script name is kept for compatibility with the earlier Nagel-only
benchmark, but the harness now supports all parser types exposed by the current
pipeline implementation.

Examples:
  Parse/chunk only (default):
    python scripts/benchmark_nagel_pipeline.py book.epub

  Full conversion with MLX cleaner:
    python scripts/benchmark_nagel_pipeline.py book.epub --full

  Full conversion with CUDA cleaner:
    python scripts/benchmark_nagel_pipeline.py book.epub --full \\
      --cleaner-backend cuda --cleaner-cuda-device cuda:0 --cleaner-cuda-quantization 8bit

  Remote API cleaner:
    python scripts/benchmark_nagel_pipeline.py book.epub --full \\
      --cleaner-backend api --cleaner-model gpt-4o-mini
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import wave
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from modules.ingestion.epub_parser import EPUBParser  # noqa: E402
from modules.ingestion.markdown_parser import MarkdownParser  # noqa: E402
from modules.ingestion.pdf_preflight import analyze_pdf_text_layer  # noqa: E402
from modules.ingestion.pdf_parser import PDFParser  # noqa: E402
from modules.text.document_normalizer import normalize_extracted_document  # noqa: E402
from modules.ingestion.text_parser import TextParser  # noqa: E402
from modules.errors import OCRRequiredPDFError  # noqa: E402
from modules.pipeline.orchestrator import ConversionPipeline, PipelineConfig  # noqa: E402
from modules.pipeline.memory_policy import SafeMemoryPolicy, sample_memory_snapshot  # noqa: E402
from modules.pipeline.stage_metrics import StageTimings, time_instance_method  # noqa: E402
from modules.ingestion.extraction_artifact_signals import scan_cleaner_handoff_artifacts  # noqa: E402
from modules.tts import cleaner_rules as cleaner_rules_backend  # noqa: E402
from modules.tts import cleaner_config  # noqa: E402
from modules.tts.cleaner_backends import available_cleaner_backends  # noqa: E402
from modules.tts.cleaner_config import CleanerConfig, resolve_cleaner_model_name  # noqa: E402
from modules.tts.device_memory import get_combined_device_memory_policy  # noqa: E402
from modules.tts.runtime_options import resolve_tts_runtime  # noqa: E402
from modules.pipeline.models import build_worker_plan  # noqa: E402
from modules.tts.chunker import ChunkConfig, TextChunker  # noqa: E402
from modules.tts.factory import TTSEngineFactory  # noqa: E402


def cleaner_backend_choices() -> tuple[str, ...]:
    return available_cleaner_backends()


def cleaner_cuda_quantization_choices() -> tuple[str, ...]:
    return cleaner_config.CUDA_CLEANER_QUANTIZATIONS


BENCHMARK_SCHEMA_VERSION = 3
BENCHMARK_SCHEMA_KEYS = (
    "schema_version",
    "source_path",
    "source_hash",
    "source_size_bytes",
    "source_type",
    "source_classification",
    "source_classification_confidence",
    "ocr_required",
    "mode",
    "pdf_extraction_backend",
    "cleaner_backend",
    "cleaner_model_name",
    "cleaner_skip_llm_when_not_needed",
    "tts_engine",
    "tts_provider",
    "tts_device",
    "tts_model_name",
    "tts_quantization",
    "voice",
    "chunk_size",
    "chapter_start",
    "chapter_limit",
    "emit_chapter_mp3_artifacts",
    "enable_chapter_aac_packaging",
    "enable_background_chapter_aac_encoding",
    "enable_chunk_overlap",
    "enable_tts_file_batch_results",
    "parallel_requested",
    "requested_parallel_chapters",
    "effective_parallel_chapters",
    "parallel_worker_mode",
    "memory_safe_mode",
    "safe_mode_reason",
    "warmup_memory_delta_bytes",
    "estimated_per_worker_memory_bytes",
    "selected_worker_count_reason",
    "title",
    "author",
    "parse_seconds",
    "chunk_seconds",
    "parse_chars_per_second",
    "extracted_chars",
    "pages",
    "chapters",
    "chapter_titles",
    "chunks",
    "skipped_non_prose_sections",
    "clean_seconds_total",
    "clean_seconds_per_chunk",
    "tts_model_load_seconds_total",
    "tts_seconds_total",
    "tts_seconds_per_chunk",
    "tts_chunk_profile",
    "clean_tts_overlap_seconds_total",
    "encode_seconds_total",
    "package_seconds_total",
    "pipeline_parse_seconds",
    "wall_time_seconds",
    "processing_time_seconds",
    "total_duration_ms",
    "output_path",
    "output_dir",
    "temp_dir",
    "metrics_path",
    "artifact_quality",
    "cleaner_gate_rule_only_chunks",
    "cleaner_gate_llm_needed_chunks",
    "cleaner_gate_artifact_issue_count",
    "cleaner_gate_artifact_issue_types",
    "cleaner_gate_artifact_issues",
    "chapter_errors",
    "success",
    "error",
    "errors",
)

_HEADING_RE = re.compile(r"(?m)^\s*#{1,6}\s+.*$")


class BenchmarkValidationError(RuntimeError):
    """Raised when a benchmark artifact is not valid for comparison."""


def _tts_chunk_profile_summary(
    *,
    enabled: bool,
    path: Path | None = None,
    rows: int = 0,
    errors: int = 0,
) -> dict[str, Any]:
    return {
        "enabled": bool(enabled),
        "path": str(path) if enabled and path is not None else None,
        "rows": int(rows),
        "errors": int(errors),
    }


def _profile_wav_duration_ms(path: Path, fallback_sample_rate: int) -> int | None:
    try:
        with wave.open(str(path), "rb") as wav_file:
            sample_rate = wav_file.getframerate() or fallback_sample_rate
            return int(wav_file.getnframes() / max(1, sample_rate) * 1000)
    except Exception:
        return None


class TTSChunkProfiler:
    """Collect opt-in benchmark-only timing rows for TTS synthesis calls."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows: list[dict[str, Any]] = []
        self._wrapped_engine_ids: set[int] = set()

    def summary(self) -> dict[str, Any]:
        return _tts_chunk_profile_summary(
            enabled=True,
            path=self.path,
            rows=len(self.rows),
            errors=sum(1 for row in self.rows if row.get("error")),
        )

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = "\n".join(json.dumps(row, sort_keys=True) for row in self.rows)
        self.path.write_text((payload + "\n") if payload else "", encoding="utf-8")

    @staticmethod
    def _arg_or_kw(
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        name: str,
        position: int,
    ) -> Any:
        if name in kwargs:
            return kwargs[name]
        if len(args) > position:
            return args[position]
        return None

    @staticmethod
    def _numeric_attr(instance: Any, name: str) -> float | int | None:
        value = getattr(instance, name, None)
        if isinstance(value, (int, float)):
            return value
        return None

    @staticmethod
    def _sample_rate(engine: Any) -> int:
        config = getattr(engine, "config", None)
        return int(getattr(engine, "sample_rate", getattr(config, "sample_rate", 24000)) or 24000)

    def wrap_engine(self, engine: Any) -> None:
        if engine is None:
            return
        for inner_name in ("_engine", "_runtime"):
            inner_engine = getattr(engine, inner_name, None)
            if inner_engine is not None and inner_engine is not engine:
                self.wrap_engine(inner_engine)

        engine_id = id(engine)
        if engine_id in self._wrapped_engine_ids:
            return

        wrapped_any = False
        for method_name in ("synthesize", "synthesize_to_output_file"):
            original = getattr(engine, method_name, None)
            if callable(original):
                setattr(engine, method_name, self._profiled_method(engine, method_name, original))
                wrapped_any = True
        if wrapped_any:
            self._wrapped_engine_ids.add(engine_id)

    def _profiled_method(self, engine: Any, method_name: str, original: Any):
        def profiled(*args: Any, **kwargs: Any) -> Any:
            call_index = len(self.rows) + 1
            text = self._arg_or_kw(args, kwargs, "text", 0)
            output_path_position = 1 if method_name == "synthesize_to_output_file" else 4
            output_path = self._arg_or_kw(args, kwargs, "output_path", output_path_position)
            voice_position = 2 if method_name == "synthesize_to_output_file" else 1
            speed_position = 3 if method_name == "synthesize_to_output_file" else 2
            voice = self._arg_or_kw(args, kwargs, "voice", voice_position)
            speed = self._arg_or_kw(args, kwargs, "speed", speed_position)
            retry_before = self._numeric_attr(engine, "_retry_count")
            pause_before = self._numeric_attr(engine, "_batch_pause_total_ms")
            started_at = time.perf_counter()
            result: Any = None
            error: str | None = None
            try:
                result = original(*args, **kwargs)
                return result
            except Exception as exc:
                error = str(exc)
                raise
            finally:
                elapsed_seconds = _round_seconds(time.perf_counter() - started_at)
                sample_rate = self._sample_rate(engine)
                result_path = Path(result) if isinstance(result, (str, Path)) else None
                requested_path = Path(output_path) if isinstance(output_path, (str, Path)) else None
                audio_path = result_path or requested_path
                audio_samples = (
                    len(result)
                    if hasattr(result, "__len__")
                    and not isinstance(result, (str, bytes, Path))
                    else None
                )
                duration_ms: int | None = None
                if isinstance(audio_samples, int):
                    duration_ms = int(audio_samples / max(1, sample_rate) * 1000)
                elif audio_path is not None:
                    duration_ms = _profile_wav_duration_ms(audio_path, sample_rate)
                retry_after = self._numeric_attr(engine, "_retry_count")
                pause_after = self._numeric_attr(engine, "_batch_pause_total_ms")
                self.rows.append(
                    {
                        "call_index": call_index,
                        "method": method_name,
                        "output_mode": "file" if audio_path is not None else "array",
                        "text_chars": len(text) if isinstance(text, str) else None,
                        "voice": voice,
                        "speed": speed,
                        "sample_rate": sample_rate,
                        "elapsed_seconds": elapsed_seconds,
                        "audio_samples": audio_samples if isinstance(audio_samples, int) else None,
                        "duration_ms": duration_ms,
                        "audio_path": str(audio_path) if audio_path is not None else None,
                        "retry_count_delta": (
                            retry_after - retry_before
                            if isinstance(retry_before, (int, float))
                            and isinstance(retry_after, (int, float))
                            else None
                        ),
                        "batch_pause_delta_ms": (
                            pause_after - pause_before
                            if isinstance(pause_before, (int, float))
                            and isinstance(pause_after, (int, float))
                            else None
                        ),
                        "error": error,
                    }
                )

        return profiled


class _BenchmarkForcedMemoryPolicy:
    """Benchmark-only policy that lets requested worker counts run unchanged."""

    safe_mode_reason = None
    selected_worker_count_reason = "forced benchmark worker count"
    measured_warmup_delta_bytes = None
    estimated_per_worker_memory_bytes = None
    enable_warmup_calibration = False

    def allowed_workers(self, requested_max: int) -> int:
        return max(1, int(requested_max))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _round_seconds(value: float) -> float:
    return round(max(0.0, value), 4)


def _apply_cleaner_env(
    *,
    cleaner_backend: str,
    cleaner_cuda_device: str,
    cleaner_cuda_quantization: str,
) -> None:
    os.environ["CLEANER_BACKEND"] = cleaner_backend
    os.environ["CLEANER_CUDA_DEVICE"] = cleaner_cuda_device
    os.environ["CLEANER_CUDA_QUANTIZATION"] = cleaner_cuda_quantization


def _default_pipeline_config(
    *,
    output_dir: Path,
    temp_dir: Path,
    pdf_extraction_backend: str,
) -> PipelineConfig:
    return PipelineConfig(
        output_dir=output_dir,
        temp_dir=temp_dir,
        pdf_extraction_backend=pdf_extraction_backend,
        enable_parallel=True,
        max_parallel_chapters=2,
        save_cleaning_debug=True,
    )


def _build_config(
    *,
    output_dir: Path,
    temp_dir: Path,
    tts_engine: str | None = None,
    tts_model_name: str | None = None,
    tts_quantization: str | None = None,
    cleaner_backend: str | None = None,
    cleaner_model_name: str | None = None,
    cleaner_cuda_device: str | None = None,
    cleaner_cuda_quantization: str | None = None,
    cleaner_skip_llm_when_not_needed: bool = True,
    pdf_extraction_backend: str = "fitz",
    voice: str | None = None,
    chunk_size: int | None = None,
    chapter_start: int | None = None,
    chapter_limit: int | None = None,
    emit_chapter_mp3_artifacts: bool = False,
    enable_chapter_aac_packaging: bool = False,
    enable_background_chapter_aac_encoding: bool = True,
    enable_chunk_overlap: bool | None = False,
    enable_tts_file_batch_results: bool = True,
    enable_parallel: bool = True,
    max_parallel_chapters: int = 2,
    parallel_worker_mode: str = "auto",
) -> PipelineConfig:
    config = _default_pipeline_config(
        output_dir=output_dir,
        temp_dir=temp_dir,
        pdf_extraction_backend=pdf_extraction_backend,
    )
    if tts_engine is not None:
        config.tts_engine = tts_engine
    if tts_model_name is not None:
        config.tts_model_name = tts_model_name
    if tts_quantization is not None:
        config.tts_quantization = tts_quantization
    if cleaner_backend is not None:
        config.cleaner_backend = cleaner_backend
    if cleaner_model_name is not None:
        config.cleaner_model_name = cleaner_model_name
    if cleaner_cuda_device is not None:
        config.cleaner_cuda_device = cleaner_cuda_device
    if cleaner_cuda_quantization is not None:
        config.cleaner_cuda_quantization = cleaner_cuda_quantization
    config.cleaner_skip_llm_when_not_needed = cleaner_skip_llm_when_not_needed
    if voice is not None:
        config.voice = voice
    if chunk_size is not None:
        config.chunk_size = int(chunk_size)
    if chapter_start is not None:
        config.chapter_start = max(1, int(chapter_start))
    if chapter_limit is not None:
        config.chapter_limit = max(1, int(chapter_limit))
    config.emit_chapter_mp3_artifacts = bool(emit_chapter_mp3_artifacts)
    config.enable_chapter_aac_packaging = bool(enable_chapter_aac_packaging)
    config.enable_background_chapter_aac_encoding = bool(enable_background_chapter_aac_encoding)
    requested_enable_chunk_overlap = enable_chunk_overlap
    config.enable_chunk_overlap = bool(enable_chunk_overlap)
    config.enable_tts_file_batch_results = bool(enable_tts_file_batch_results)
    config.enable_parallel = enable_parallel
    config.max_parallel_chapters = int(max_parallel_chapters)
    config.requested_parallel_chapters = int(max_parallel_chapters)
    config.parallel_worker_mode = str(parallel_worker_mode or "auto")
    config = _finalize_benchmark_config(
        config,
        explicit_cleaner_model=cleaner_model_name is not None,
    )
    if requested_enable_chunk_overlap is None:
        config.enable_chunk_overlap = False
    return config


def _finalize_benchmark_config(
    config: PipelineConfig,
    *,
    explicit_cleaner_model: bool,
) -> PipelineConfig:
    """Re-resolve runtime fields after post-init mutations in the benchmark harness."""
    config.cleaner_model_name = resolve_cleaner_model_name(
        backend=config.cleaner_backend,
        requested_model=None,
        configured_model=config.cleaner_model_name,
        explicit_model=explicit_cleaner_model,
    )
    if (
        str(config.cleaner_backend).strip().lower() == "cuda"
        and str(config.tts_model_name or "").startswith("mlx-community/")
    ):
        config.tts_model_name = None
    config.tts_runtime = None
    config.tts_runtime_options = None
    config.tts_runtime, config.tts_runtime_options = resolve_tts_runtime(
        tts_engine=config.tts_engine,
        tts_model_name=config.tts_model_name,
        tts_quantization=config.tts_quantization,
        tts_runtime=config.tts_runtime,
        tts_runtime_options=config.tts_runtime_options,
    )
    config.tts_engine = config.tts_runtime.engine
    config.tts_model_name = config.tts_runtime.resolved_model()
    config.tts_quantization = config.tts_runtime.quantization
    resolved_cleaner_config = CleanerConfig(
        model_name=config.cleaner_model_name,
        backend=config.cleaner_backend,
        cuda_device=config.cleaner_cuda_device,
        cuda_quantization=config.cleaner_cuda_quantization,
    )
    config.device_memory_policy = get_combined_device_memory_policy(
        config.tts_runtime,
        resolved_cleaner_config,
    )
    config.worker_plan = build_worker_plan(config)
    return config


def _parse_document(source_path: Path, config: PipelineConfig) -> Any:
    suffix = source_path.suffix.lower()
    if suffix == ".pdf":
        document = PDFParser(
            source_path,
            extraction_backend=config.pdf_extraction_backend,
        ).parse()
    elif suffix == ".epub":
        document = EPUBParser(source_path).parse()
    elif suffix == ".txt":
        document = TextParser(source_path).parse()
    elif suffix == ".md":
        document = MarkdownParser(source_path).parse()
    else:
        raise ValueError(f"Unsupported benchmark source type: {suffix}")
    _limit_document_chapters(
        document,
        config.chapter_limit,
        chapter_start=getattr(config, "chapter_start", 1),
    )
    if bool(getattr(config, "pre_chunk_document_cleanup", True)):
        document = normalize_extracted_document(
            document,
            source_type=suffix,
            skip_non_prose_sections=bool(
                getattr(config, "skip_non_prose_sections", True)
            ),
        )
    return document


def _document_chunks(document: Any, config: PipelineConfig) -> list[str]:
    chunker = TextChunker(ChunkConfig(max_tokens=config.chunk_size))
    return [
        chunk
        for chapter in getattr(document, "chapters", [])
        for chunk in chunker.chunk(str(getattr(chapter, "content", "")))
    ]


def _chunk_document(document: Any, config: PipelineConfig) -> tuple[int, float]:
    started_at = time.perf_counter()
    chunks = _document_chunks(document, config)
    return len(chunks), time.perf_counter() - started_at


def _limit_document_chapters(
    document: Any,
    chapter_limit: int | None,
    *,
    chapter_start: int = 1,
) -> Any:
    start_index = max(0, int(chapter_start or 1) - 1)
    if chapter_limit is None and start_index == 0:
        return document
    chapters = list(getattr(document, "chapters", []))
    if chapter_limit is None:
        document.chapters = chapters[start_index:]
    else:
        document.chapters = chapters[start_index : start_index + max(1, int(chapter_limit))]
    return document


def _source_metrics_from_document(
    source_path: Path,
    config: PipelineConfig,
    document: Any,
    *,
    parse_seconds: float,
) -> dict[str, Any]:
    chunks, chunk_seconds = _chunk_document(document, config)
    chapters = list(getattr(document, "chapters", []))
    extracted_chars = sum(len(str(getattr(chapter, "content", ""))) for chapter in chapters)

    return {
        "title": getattr(document, "title", source_path.stem),
        "author": getattr(document, "author", None),
        "pages": getattr(document, "total_pages", None),
        "chapters": len(chapters),
        "chapter_titles": [str(getattr(chapter, "title", "")) for chapter in chapters],
        "extracted_chars": extracted_chars,
        "chunks": chunks,
        "skipped_non_prose_sections": list(
            getattr(document, "skipped_non_prose_sections", []) or []
        ),
        "parse_seconds": _round_seconds(parse_seconds),
        "chunk_seconds": _round_seconds(chunk_seconds),
        "parse_chars_per_second": round(extracted_chars / max(parse_seconds, 1e-9), 2),
    }


def _parse_source_for_metrics(source_path: Path, config: PipelineConfig) -> tuple[dict[str, Any], Any]:
    started_at = time.perf_counter()
    document = _parse_document(source_path, config)
    parse_seconds = time.perf_counter() - started_at
    return (
        _source_metrics_from_document(
            source_path,
            config,
            document,
            parse_seconds=parse_seconds,
        ),
        document,
    )


def _parse_source_metrics(source_path: Path, config: PipelineConfig) -> dict[str, Any]:
    metrics, _document = _parse_source_for_metrics(source_path, config)
    return metrics


def _validate_extraction(metrics: dict[str, Any], *, min_extracted_chars: int) -> None:
    if metrics.get("ocr_required"):
        raise BenchmarkValidationError(str(metrics.get("error") or "OCR required for this PDF"))
    extracted_chars = int(metrics.get("extracted_chars") or 0)
    chunks = int(metrics.get("chunks") or 0)
    if extracted_chars <= 0 or chunks <= 0:
        raise BenchmarkValidationError("no extractable text or chunks in parsed source")
    if min_extracted_chars > 0 and extracted_chars < min_extracted_chars:
        raise BenchmarkValidationError(
            f"extracted_chars {extracted_chars} below minimum {min_extracted_chars}"
        )


def _strip_markdown_headings(text: str) -> str:
    return _HEADING_RE.sub("", text).strip()


def _issue(
    issue_type: str,
    path: Path,
    detail: str,
    *,
    excerpt: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": issue_type,
        "path": str(path),
        "detail": detail,
    }
    if excerpt:
        payload["excerpt"] = excerpt[:160]
    return payload


_ARTIFACT_ISSUE_DETAILS = {
    "prompt_delimiter_leakage": "cleaned/TTS handoff text contains prompt delimiter-like content",
    "model_commentary_leakage": "cleaned/TTS handoff text contains model commentary-like content",
    "pdf_hyphenation_leakage": "cleaned/TTS handoff text still contains PDF line-break hyphenation",
    "pdf_page_footnote_leakage": "cleaned/TTS handoff text still contains PDF page/header/footnote residue",
    "pdf_joined_dash_leakage": "cleaned/TTS handoff text contains a joined PDF dash line-break artifact",
    "epub_social_boilerplate": "cleaned/TTS handoff text contains EPUB publisher/social boilerplate",
    "joined_word_leakage": "cleaned/TTS handoff text contains joined word extraction residue",
    "garbled_repeated_token_leakage": "cleaned/TTS handoff text contains a garbled repeated-character token",
}


def _scan_text_for_leakage(path: Path, text: str) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for artifact in scan_cleaner_handoff_artifacts(text):
        issues.append(
            _issue(
                artifact.issue_type,
                path,
                _ARTIFACT_ISSUE_DETAILS.get(
                    artifact.issue_type,
                    "cleaned/TTS handoff text contains extraction artifact residue",
                ),
                excerpt=artifact.excerpt,
            )
        )
    return issues


def _run_cleaner_gate(
    *,
    source_path: Path,
    config: PipelineConfig,
    document: Any | None = None,
) -> dict[str, Any]:
    """Score chunks after rule-based cleanup without loading cleaner or TTS models."""
    if document is None:
        document = _parse_document(source_path, config)
    chunks = _document_chunks(document, config)
    detected_issues: list[dict[str, Any]] = []
    rule_only_chunks = 0
    llm_needed_chunks = 0
    cleaned_chars = 0

    for index, chunk in enumerate(chunks, start=1):
        rule_based_text = cleaner_rules_backend.rule_based_clean(chunk)
        cleaned_chars += len(rule_based_text)
        chunk_issues = _scan_text_for_leakage(
            Path(f"cleaner_gate#chunk:{index}"),
            rule_based_text,
        )
        detected_issues.extend(chunk_issues)
        needs_llm = bool(chunk_issues) or cleaner_rules_backend.needs_llm_cleaning(rule_based_text)
        if needs_llm:
            llm_needed_chunks += 1
        else:
            rule_only_chunks += 1

    issue_types = sorted({issue["type"] for issue in detected_issues})
    return {
        "cleaner_gate_rule_only_chunks": rule_only_chunks,
        "cleaner_gate_llm_needed_chunks": llm_needed_chunks,
        "cleaner_gate_artifact_issue_count": len(detected_issues),
        "cleaner_gate_artifact_issue_types": issue_types,
        "cleaner_gate_artifact_issues": detected_issues,
        "artifact_quality": {
            "success": not detected_issues,
            "issues": detected_issues,
            "checked_files": [f"cleaner_gate#chunk:{index}" for index in range(1, len(chunks) + 1)],
            "cleaned_chars": cleaned_chars,
            "before_cleaning_chars": sum(len(chunk) for chunk in chunks),
        },
    }


def _read_cleaned_trace_texts(path: Path) -> list[str]:
    texts: list[str] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            cleaned_text = payload.get("cleaned_text")
            if isinstance(cleaned_text, str):
                texts.append(cleaned_text)
    except Exception:
        return texts
    return texts


def scan_artifact_quality(
    output_dir: Path | str,
    *,
    source_chars: int | None = None,
) -> dict[str, Any]:
    """Scan benchmark artifacts for cleaner/TTS handoff quality regressions."""
    root = Path(output_dir)
    issues: list[dict[str, Any]] = []
    checked_files: list[str] = []
    cleaned_chars_total = 0
    before_chars_total = 0

    cleaned_paths = sorted(root.rglob("chapter_*_cleaned.md"))
    for cleaned_path in cleaned_paths:
        checked_files.append(str(cleaned_path))
        cleaned_text = cleaned_path.read_text(encoding="utf-8", errors="replace")
        cleaned_body = _strip_markdown_headings(cleaned_text)
        cleaned_chars_total += len(cleaned_body)
        issues.extend(_scan_text_for_leakage(cleaned_path, cleaned_body))

        before_path = cleaned_path.with_name(
            cleaned_path.name.replace("_cleaned.md", "_before_cleaning.md")
        )
        if before_path.exists():
            before_text = before_path.read_text(encoding="utf-8", errors="replace")
            before_body = _strip_markdown_headings(before_text)
            before_chars = len(before_body)
            before_chars_total += before_chars
            if before_chars >= 1000 and len(cleaned_body) < max(200, int(before_chars * 0.5)):
                issues.append(
                    _issue(
                        "suspiciously_short_output",
                        cleaned_path,
                        f"cleaned output has {len(cleaned_body)} chars for {before_chars} input chars",
                    )
                )

    trace_paths = sorted(root.rglob("chapter_*_cleaning_trace.jsonl"))
    if not cleaned_paths and not trace_paths:
        issues.append(
            _issue(
                "missing_handoff_artifacts",
                root,
                "no cleaned text or cleaning trace artifacts were available to inspect",
            )
        )

    for trace_path in trace_paths:
        checked_files.append(str(trace_path))
        for index, cleaned_text in enumerate(_read_cleaned_trace_texts(trace_path), start=1):
            issues.extend(
                _scan_text_for_leakage(
                    Path(f"{trace_path}#cleaned_text:{index}"),
                    cleaned_text,
                )
            )

    if (
        source_chars is not None
        and source_chars >= 1000
        and cleaned_paths
        and cleaned_chars_total < max(200, int(source_chars * 0.5))
    ):
        issues.append(
            _issue(
                "suspiciously_short_output",
                root,
                f"all cleaned artifacts total {cleaned_chars_total} chars for {source_chars} source chars",
            )
        )

    repeated_commentary = [
        issue
        for issue in issues
        if issue["type"] in {"model_commentary_leakage", "prompt_delimiter_leakage"}
    ]
    if len(repeated_commentary) >= 3:
        issues.append(
            _issue(
                "repeated_boilerplate",
                root,
                f"{len(repeated_commentary)} leakage indicators found across cleaned artifacts",
            )
        )

    return {
        "success": not issues,
        "issues": issues,
        "checked_files": checked_files,
        "cleaned_chars": cleaned_chars_total,
        "before_cleaning_chars": before_chars_total,
    }


def _install_tts_chunk_profiler(pipeline: Any, profiler: TTSChunkProfiler) -> None:
    stages = getattr(pipeline, "stages", None)
    processor = getattr(stages, "chapter_processor", None) if stages is not None else None
    target = processor if processor is not None else pipeline
    ensure_attr = "ensure_chapter_tts_engine"
    original_ensure = getattr(target, ensure_attr, None)
    if not callable(original_ensure):
        ensure_attr = "_ensure_chapter_tts_engine"
        original_ensure = getattr(target, ensure_attr, None)

    if callable(original_ensure):

        def profiled_ensure(*args: Any, **kwargs: Any) -> Any:
            result = original_ensure(*args, **kwargs)
            profiler.wrap_engine(getattr(pipeline, "_tts_engine", None))
            return result

        setattr(target, ensure_attr, profiled_ensure)

    profiler.wrap_engine(getattr(pipeline, "_tts_engine", None))


def _create_timed_pipeline(
    config: PipelineConfig,
    timings: StageTimings,
) -> Any:
    pipeline = ConversionPipeline(config)
    stages = getattr(pipeline, "stages", None)
    if stages is not None:
        processor = stages.chapter_processor
        time_instance_method(stages, "ingest", "parse", timings)
        time_instance_method(stages, "package", "package", timings)
        time_instance_method(processor, "clean_chapter_content", "clean", timings)
        time_instance_method(processor, "synthesize_chapter_chunks", "tts", timings)
        time_instance_method(
            processor,
            "clean_and_synthesize_with_overlap",
            "clean_tts_overlap",
            timings,
        )
        time_instance_method(processor, "encode_chapter_audio", "encode", timings)
        time_instance_method(processor, "ensure_chapter_tts_engine", "tts_model_load", timings)
        return pipeline

    time_instance_method(pipeline, "_ingest", "parse", timings)
    time_instance_method(pipeline, "_clean_chapter_content", "clean", timings)
    time_instance_method(pipeline, "_synthesize_chapter_chunks", "tts", timings)
    time_instance_method(
        pipeline,
        "_clean_and_synthesize_with_overlap",
        "clean_tts_overlap",
        timings,
    )
    time_instance_method(pipeline, "_encode_chapter_audio", "encode", timings)
    time_instance_method(pipeline, "_package", "package", timings)
    time_instance_method(pipeline, "_ensure_chapter_tts_engine", "tts_model_load", timings)
    return pipeline


def _ensure_mlx_conversion_engine(tts_engine: str) -> None:
    available = TTSEngineFactory.available_conversion_engines(provider="mlx-audio")
    if tts_engine not in available:
        raise BenchmarkValidationError(
            f"full conversion benchmark requires an MLX conversion engine; got {tts_engine!r}"
        )


def _base_metrics(
    *,
    source_path: Path,
    output_dir: Path,
    temp_dir: Path,
    config: PipelineConfig,
    parse_only: bool,
    cleaner_gate: bool,
    profile_tts_chunks: bool = False,
) -> dict[str, Any]:
    source_suffix = source_path.suffix.lower()
    effective_pdf_backend = config.pdf_extraction_backend if source_suffix == ".pdf" else None
    return {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "source_path": str(source_path),
        "source_hash": _sha256_file(source_path),
        "source_size_bytes": source_path.stat().st_size,
        "source_type": source_suffix.removeprefix("."),
        "source_classification": None,
        "source_classification_confidence": None,
        "ocr_required": False,
        "mode": "cleaner_gate" if cleaner_gate else "parse_only" if parse_only else "full",
        "pdf_extraction_backend": effective_pdf_backend,
        "cleaner_backend": config.cleaner_backend,
        "cleaner_model_name": config.cleaner_model_name,
        "cleaner_skip_llm_when_not_needed": config.cleaner_skip_llm_when_not_needed,
        "tts_engine": config.tts_engine,
        "tts_provider": config.tts_runtime.provider,
        "tts_device": config.tts_runtime.device,
        "tts_model_name": config.tts_model_name,
        "tts_quantization": config.tts_quantization,
        "voice": config.voice,
        "chunk_size": config.chunk_size,
        "chapter_start": getattr(config, "chapter_start", 1),
        "chapter_limit": config.chapter_limit,
        "emit_chapter_mp3_artifacts": config.emit_chapter_mp3_artifacts,
        "enable_chapter_aac_packaging": config.enable_chapter_aac_packaging,
        "enable_background_chapter_aac_encoding": config.enable_background_chapter_aac_encoding,
        "enable_chunk_overlap": config.enable_chunk_overlap,
        "enable_tts_file_batch_results": config.enable_tts_file_batch_results,
        "parallel_requested": bool(config.enable_parallel),
        "requested_parallel_chapters": int(config.max_parallel_chapters),
        "effective_parallel_chapters": int(config.max_parallel_chapters),
        "parallel_worker_mode": str(getattr(config, "parallel_worker_mode", "auto")),
        "memory_safe_mode": False,
        "safe_mode_reason": None,
        "warmup_memory_delta_bytes": None,
        "estimated_per_worker_memory_bytes": None,
        "selected_worker_count_reason": (
            "parallel requested without full pipeline calibration"
            if config.enable_parallel
            else "requested sequential mode"
        ),
        "title": None,
        "author": None,
        "parse_seconds": 0.0,
        "chunk_seconds": 0.0,
        "parse_chars_per_second": 0.0,
        "extracted_chars": 0,
        "pages": None,
        "chapters": 0,
        "chapter_titles": [],
        "chunks": 0,
        "skipped_non_prose_sections": [],
        "clean_seconds_total": 0.0,
        "clean_seconds_per_chunk": None,
        "tts_model_load_seconds_total": 0.0,
        "tts_seconds_total": 0.0,
        "tts_seconds_per_chunk": None,
        "tts_chunk_profile": _tts_chunk_profile_summary(
            enabled=profile_tts_chunks,
            path=output_dir / "tts_chunk_profile.jsonl",
        ),
        "clean_tts_overlap_seconds_total": 0.0,
        "encode_seconds_total": 0.0,
        "package_seconds_total": 0.0,
        "pipeline_parse_seconds": 0.0,
        "wall_time_seconds": 0.0,
        "processing_time_seconds": 0.0,
        "total_duration_ms": 0,
        "output_path": None,
        "output_dir": str(output_dir),
        "temp_dir": str(temp_dir),
        "metrics_path": str(output_dir / "benchmark_summary.json"),
        "artifact_quality": {
            "success": True,
            "issues": [],
            "checked_files": [],
            "cleaned_chars": 0,
            "before_cleaning_chars": 0,
        },
        "cleaner_gate_rule_only_chunks": 0,
        "cleaner_gate_llm_needed_chunks": 0,
        "cleaner_gate_artifact_issue_count": 0,
        "cleaner_gate_artifact_issue_types": [],
        "cleaner_gate_artifact_issues": [],
        "chapter_errors": [],
        "success": True,
        "error": None,
        "errors": [],
    }


def _apply_source_preflight_metrics(metrics: dict[str, Any], source_path: Path) -> None:
    if source_path.suffix.lower() != ".pdf":
        return

    try:
        analysis = analyze_pdf_text_layer(source_path)
    except Exception:
        metrics["source_classification"] = "unknown"
        metrics["source_classification_confidence"] = 0.0
        return

    metrics["source_classification"] = analysis.classification
    metrics["source_classification_confidence"] = analysis.confidence
    metrics["ocr_required"] = analysis.requires_ocr
    metrics["pages"] = analysis.total_pages
    if analysis.requires_ocr:
        raise OCRRequiredPDFError(
            file_path=source_path,
            confidence=analysis.confidence,
            sampled_pages=analysis.sampled_pages,
            text_chars=analysis.text_chars,
        )


def _write_metrics(metrics: dict[str, Any], output_dir: Path) -> None:
    metrics_path = output_dir / "benchmark_summary.json"
    metrics["metrics_path"] = str(metrics_path)
    metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True), encoding="utf-8")


def _apply_full_result_metrics(
    metrics: dict[str, Any],
    result: Any,
    timings: StageTimings,
    pipeline: Any,
) -> None:
    for chapter in getattr(result, "chapters", []):
        stage_timings = getattr(chapter, "stage_timings", None)
        if not isinstance(stage_timings, dict):
            continue
        for key in (
            "clean",
            "tts",
            "clean_tts_overlap",
            "encode",
            "tts_model_load",
        ):
            value = stage_timings.get(key)
            if isinstance(value, (int, float)) and value > 0:
                timings.add(key, float(value))

    chunks = int(metrics.get("chunks") or 0)
    clean_total = timings.get("clean")
    tts_total = timings.get("tts")
    overlap_total = timings.get("clean_tts_overlap")
    if overlap_total > 0 and clean_total == 0 and tts_total == 0:
        clean_total = overlap_total
        tts_total = overlap_total

    safe_mode_reason = getattr(
        getattr(getattr(pipeline, "config", None), "memory_policy", None),
        "safe_mode_reason",
        None,
    )
    memory_policy = getattr(getattr(pipeline, "config", None), "memory_policy", None)
    warmup_memory_delta_bytes = getattr(
        memory_policy,
        "measured_warmup_delta_bytes",
        None,
    )
    estimated_per_worker_memory_bytes = getattr(
        memory_policy,
        "estimated_per_worker_memory_bytes",
        None,
    )
    selected_worker_count_reason = getattr(
        memory_policy,
        "selected_worker_count_reason",
        None,
    )
    effective_parallel_chapters = int(
        getattr(
            pipeline,
            "_selected_parallel_chapters",
            getattr(getattr(pipeline, "config", None), "max_parallel_chapters", 1),
        )
        or 1
    )
    pipeline_selected_reason = getattr(
        pipeline,
        "_selected_parallel_worker_count_reason",
        None,
    )

    metrics.update(
        {
            "success": bool(getattr(result, "success", False)),
            "error": getattr(result, "error", None),
            "output_path": str(result.output_path) if getattr(result, "output_path", None) else None,
            "total_duration_ms": int(getattr(result, "total_duration_ms", 0) or 0),
            "processing_time_seconds": _round_seconds(
                float(getattr(result, "processing_time_seconds", 0.0) or 0.0)
            ),
            "clean_seconds_total": _round_seconds(clean_total),
            "clean_seconds_per_chunk": timings.per_chunk(clean_total, chunks),
            "tts_model_load_seconds_total": timings.rounded("tts_model_load"),
            "tts_seconds_total": _round_seconds(tts_total),
            "tts_seconds_per_chunk": timings.per_chunk(tts_total, chunks),
            "clean_tts_overlap_seconds_total": timings.rounded("clean_tts_overlap"),
            "encode_seconds_total": timings.rounded("encode"),
            "package_seconds_total": timings.rounded("package"),
            "pipeline_parse_seconds": timings.rounded("parse"),
            "effective_parallel_chapters": effective_parallel_chapters,
            "memory_safe_mode": bool(safe_mode_reason),
            "safe_mode_reason": str(safe_mode_reason) if safe_mode_reason else None,
            "warmup_memory_delta_bytes": warmup_memory_delta_bytes,
            "estimated_per_worker_memory_bytes": estimated_per_worker_memory_bytes,
            "selected_worker_count_reason": (
                str(pipeline_selected_reason or selected_worker_count_reason)
                if (pipeline_selected_reason or selected_worker_count_reason)
                else metrics.get("selected_worker_count_reason")
            ),
            "chapter_errors": [
                chapter.error
                for chapter in getattr(result, "chapters", [])
                if getattr(chapter, "error", None)
            ],
        }
    )
    errors = [
        str(error)
        for error in [metrics.get("error"), *metrics.get("chapter_errors", [])]
        if error
    ]
    metrics["errors"] = errors


def run_benchmark(
    *,
    source_path: Path,
    output_dir: Path,
    temp_dir: Path,
    parse_only: bool = True,
    tts_engine: str | None = None,
    tts_model_name: str | None = None,
    tts_quantization: str | None = None,
    cleaner_backend: str | None = None,
    cleaner_model_name: str | None = None,
    cleaner_cuda_device: str | None = None,
    cleaner_cuda_quantization: str | None = None,
    cleaner_skip_llm_when_not_needed: bool = True,
    cleaner_gate: bool = False,
    pdf_extraction_backend: str = "fitz",
    voice: str | None = None,
    chunk_size: int | None = None,
    chapter_limit: int | None = None,
    chapter_start: int | None = None,
    emit_chapter_mp3_artifacts: bool = False,
    enable_chapter_aac_packaging: bool = False,
    enable_background_chapter_aac_encoding: bool = True,
    enable_chunk_overlap: bool | None = False,
    enable_tts_file_batch_results: bool = True,
    min_extracted_chars: int = 1,
    enable_parallel: bool = True,
    max_parallel_chapters: int = 2,
    parallel_worker_mode: str = "auto",
    force_parallel_workers: bool = False,
    worker_selection_mode: str = "warmup",
    profile_tts_chunks: bool = False,
) -> dict[str, Any]:
    """Run a parse-only or full conversion benchmark and emit JSON-safe metrics."""
    benchmark_start = time.perf_counter()
    source_path = Path(source_path).expanduser().resolve()
    output_dir = Path(output_dir)
    temp_dir = Path(temp_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)

    config = _build_config(
        output_dir=output_dir,
        temp_dir=temp_dir,
        tts_engine=tts_engine,
        tts_model_name=tts_model_name,
        tts_quantization=tts_quantization,
        cleaner_backend=cleaner_backend,
        cleaner_model_name=cleaner_model_name,
        cleaner_cuda_device=cleaner_cuda_device,
        cleaner_cuda_quantization=cleaner_cuda_quantization,
        cleaner_skip_llm_when_not_needed=cleaner_skip_llm_when_not_needed,
        pdf_extraction_backend=pdf_extraction_backend,
        voice=voice,
        chunk_size=chunk_size,
        chapter_start=chapter_start,
        chapter_limit=chapter_limit,
        emit_chapter_mp3_artifacts=emit_chapter_mp3_artifacts,
        enable_chapter_aac_packaging=enable_chapter_aac_packaging,
        enable_background_chapter_aac_encoding=enable_background_chapter_aac_encoding,
        enable_chunk_overlap=enable_chunk_overlap,
        enable_tts_file_batch_results=enable_tts_file_batch_results,
        enable_parallel=enable_parallel,
        max_parallel_chapters=max_parallel_chapters,
        parallel_worker_mode=parallel_worker_mode,
    )
    if force_parallel_workers:
        config.memory_policy = _BenchmarkForcedMemoryPolicy()
    elif worker_selection_mode == "static" or parallel_worker_mode == "manual":
        config.memory_policy = SafeMemoryPolicy(
            memory_sampler=sample_memory_snapshot,
            estimated_worker_bytes=4 * 1024**3,
            enable_warmup_calibration=False,
        )
    metrics = _base_metrics(
        source_path=source_path,
        output_dir=output_dir,
        temp_dir=temp_dir,
        config=config,
        parse_only=parse_only,
        cleaner_gate=cleaner_gate,
        profile_tts_chunks=profile_tts_chunks,
    )
    tts_chunk_profiler = (
        TTSChunkProfiler(output_dir / "tts_chunk_profile.jsonl")
        if profile_tts_chunks
        else None
    )

    try:
        _apply_source_preflight_metrics(metrics, source_path)
        source_metrics, parsed_document = _parse_source_for_metrics(source_path, config)
        metrics.update(source_metrics)
        _validate_extraction(metrics, min_extracted_chars=min_extracted_chars)

        if cleaner_gate:
            gate_metrics = _run_cleaner_gate(
                source_path=source_path,
                config=config,
                document=parsed_document,
            )
            metrics.update(gate_metrics)
            if not gate_metrics["artifact_quality"]["success"]:
                message = (
                    "cleaner gate check failed: "
                    + "; ".join(gate_metrics["cleaner_gate_artifact_issue_types"][:5])
                )
                metrics["success"] = False
                metrics["error"] = metrics.get("error") or message
                metrics["errors"] = [*metrics.get("errors", []), message]
        elif not parse_only:
            _ensure_mlx_conversion_engine(config.tts_engine)
            timings = StageTimings()
            pipeline = _create_timed_pipeline(config, timings)
            if tts_chunk_profiler is not None:
                _install_tts_chunk_profiler(pipeline, tts_chunk_profiler)
            result = pipeline.convert(source_path)
            _apply_full_result_metrics(metrics, result, timings, pipeline)
            quality = scan_artifact_quality(
                output_dir,
                source_chars=int(metrics.get("extracted_chars") or 0),
            )
            metrics["artifact_quality"] = quality
            if not quality["success"]:
                message = (
                    "artifact quality check failed: "
                    + "; ".join(issue["type"] for issue in quality["issues"][:5])
                )
                metrics["success"] = False
                metrics["error"] = metrics.get("error") or message
                metrics["errors"] = [*metrics.get("errors", []), message]
    except Exception as exc:
        metrics["success"] = False
        metrics["error"] = str(exc)
        metrics["errors"] = [str(exc)]
    except BaseException as exc:
        message = "interrupted" if isinstance(exc, KeyboardInterrupt) else str(exc)
        metrics["success"] = False
        metrics["error"] = message
        metrics["errors"] = [message]
        raise
    finally:
        if tts_chunk_profiler is not None:
            tts_chunk_profiler.write()
            metrics["tts_chunk_profile"] = tts_chunk_profiler.summary()
        metrics["wall_time_seconds"] = _round_seconds(time.perf_counter() - benchmark_start)
        if not metrics.get("processing_time_seconds"):
            metrics["processing_time_seconds"] = metrics["wall_time_seconds"]
        _write_metrics(metrics, output_dir)

    return metrics


def run_worker_selection_comparison(
    *,
    source_path: Path,
    output_dir: Path,
    temp_dir: Path,
    max_parallel_chapters: int = 2,
    chapter_limit: int | None = None,
    chapter_start: int | None = None,
    **benchmark_kwargs: Any,
) -> dict[str, Any]:
    """Run bounded full benchmarks for sequential, static, and warmup selection."""
    output_dir = Path(output_dir)
    temp_dir = Path(temp_dir)
    benchmark_kwargs.pop("parallel_worker_mode", None)
    cases = [
        {
            "name": "sequential_baseline",
            "enable_parallel": False,
            "max_parallel_chapters": 1,
            "worker_selection_mode": "sequential",
        },
        {
            "name": "static_conservative",
            "enable_parallel": True,
            "max_parallel_chapters": max_parallel_chapters,
            "worker_selection_mode": "static",
        },
        {
            "name": "warmup_estimated",
            "enable_parallel": True,
            "max_parallel_chapters": max_parallel_chapters,
            "worker_selection_mode": "warmup",
        },
    ]
    results: list[dict[str, Any]] = []
    for case in cases:
        case_output_dir = output_dir / case["name"]
        case_temp_dir = temp_dir / case["name"]
        metrics = run_benchmark(
            source_path=source_path,
            output_dir=case_output_dir,
            temp_dir=case_temp_dir,
            parse_only=False,
            chapter_limit=chapter_limit,
            chapter_start=chapter_start,
            enable_parallel=bool(case["enable_parallel"]),
            max_parallel_chapters=int(case["max_parallel_chapters"]),
            worker_selection_mode=str(case["worker_selection_mode"]),
            parallel_worker_mode="auto",
            **benchmark_kwargs,
        )
        results.append(
            {
                "name": case["name"],
                "metrics_path": str(case_output_dir / "benchmark_summary.json"),
                "success": metrics.get("success"),
                "wall_time_seconds": metrics.get("wall_time_seconds"),
                "clean_seconds_total": metrics.get("clean_seconds_total"),
                "tts_seconds_total": metrics.get("tts_seconds_total"),
                "encode_seconds_total": metrics.get("encode_seconds_total"),
                "package_seconds_total": metrics.get("package_seconds_total"),
                "worker_count_used": metrics.get("effective_parallel_chapters"),
                "memory_estimate_used": metrics.get(
                    "estimated_per_worker_memory_bytes"
                ),
                "artifact_quality_result": metrics.get("artifact_quality", {}).get(
                    "success"
                ),
            }
        )

    summary = {
        "source_path": str(Path(source_path).expanduser().resolve()),
        "chapter_limit": chapter_limit,
        "chapter_start": chapter_start or 1,
        "requested_parallel_chapters": max_parallel_chapters,
        "cases": results,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "worker_selection_comparison.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    defaults = PipelineConfig()
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("source", help="PDF, EPUB, Markdown, or text source to benchmark")
    parser.add_argument("--output-dir", type=Path, default=Path("output/nagel_benchmark"))
    parser.add_argument("--temp-dir", type=Path, default=Path("temp/nagel_benchmark"))
    parser.add_argument("--full", action="store_true", help="Run full conversion")
    parser.add_argument("--parse-only", action="store_true", help="Run parse/chunk benchmark only")
    parser.add_argument("--cleaner-gate", action="store_true", help="Run parse/chunk plus rule-based cleaner gate only")
    parser.add_argument("--pdf-backend", default=defaults.pdf_extraction_backend, choices=["fitz", "pymupdf4llm", "auto"])
    parser.add_argument("--tts-engine", default=defaults.tts_engine)
    parser.add_argument("--tts-model", default=None)
    parser.add_argument("--tts-quantization", default=defaults.tts_quantization)
    parser.add_argument(
        "--cleaner-backend",
        default=defaults.cleaner_backend,
        choices=cleaner_backend_choices(),
        help="Cleaner backend: mlx (Apple Silicon), cuda (NVIDIA GPU), or api (remote)",
    )
    parser.add_argument("--cleaner-model", default=None)
    parser.add_argument(
        "--cleaner-cuda-device",
        default=defaults.cleaner_cuda_device,
        help="CUDA device for cleaner when --cleaner-backend=cuda (also CLEANER_CUDA_DEVICE)",
    )
    parser.add_argument(
        "--cleaner-cuda-quantization",
        default=defaults.cleaner_cuda_quantization,
        choices=cleaner_cuda_quantization_choices(),
        help="CUDA quant mode when --cleaner-backend=cuda (also CLEANER_CUDA_QUANTIZATION)",
    )
    parser.add_argument(
        "--cleaner-skip-llm-when-not-needed",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--voice", default=defaults.voice)
    parser.add_argument("--chunk-size", type=int, default=defaults.chunk_size)
    parser.add_argument("--chapter-start", type=int, default=1)
    parser.add_argument("--chapter-limit", type=int, default=None)
    parser.add_argument("--emit-chapter-mp3-artifacts", action="store_true")
    parser.add_argument("--enable-chapter-aac-packaging", action="store_true")
    parser.add_argument(
        "--enable-background-chapter-aac-encoding",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--enable-chunk-overlap",
        action=argparse.BooleanOptionalAction,
        default=None,
    )
    parser.add_argument(
        "--enable-tts-file-batch-results",
        action=argparse.BooleanOptionalAction,
        default=defaults.enable_tts_file_batch_results,
    )
    parser.add_argument("--min-extracted-chars", type=int, default=1)
    parser.add_argument("--disable-parallel", action="store_true")
    parser.add_argument("--max-parallel-chapters", type=int, default=defaults.max_parallel_chapters)
    parser.add_argument(
        "--parallel-worker-mode",
        choices=["auto", "manual"],
        default=defaults.parallel_worker_mode,
        help="Use auto adaptive worker selection or manual requested worker count.",
    )
    parser.add_argument(
        "--force-parallel-workers",
        action="store_true",
        help="Benchmark-only: bypass memory worker clamps to test requested worker counts.",
    )
    parser.add_argument(
        "--profile-tts-chunks",
        action="store_true",
        help="Benchmark-only: write per-synthesis-call TTS timing rows.",
    )
    parser.add_argument("--compare-worker-selection", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _apply_cleaner_env(
        cleaner_backend=args.cleaner_backend,
        cleaner_cuda_device=args.cleaner_cuda_device,
        cleaner_cuda_quantization=args.cleaner_cuda_quantization,
    )
    parse_only = args.cleaner_gate or not args.full or args.parse_only
    if args.compare_worker_selection:
        summary = run_worker_selection_comparison(
            source_path=Path(args.source),
            output_dir=args.output_dir,
            temp_dir=args.temp_dir,
            tts_engine=args.tts_engine,
            tts_model_name=args.tts_model,
            tts_quantization=args.tts_quantization,
            cleaner_backend=args.cleaner_backend,
            cleaner_model_name=args.cleaner_model,
            cleaner_cuda_device=args.cleaner_cuda_device,
            cleaner_cuda_quantization=args.cleaner_cuda_quantization,
            cleaner_skip_llm_when_not_needed=args.cleaner_skip_llm_when_not_needed,
            pdf_extraction_backend=args.pdf_backend,
            voice=args.voice,
            chunk_size=args.chunk_size,
            chapter_start=args.chapter_start,
            chapter_limit=args.chapter_limit,
            emit_chapter_mp3_artifacts=args.emit_chapter_mp3_artifacts,
            enable_chapter_aac_packaging=args.enable_chapter_aac_packaging,
            enable_background_chapter_aac_encoding=args.enable_background_chapter_aac_encoding,
            enable_chunk_overlap=args.enable_chunk_overlap,
            enable_tts_file_batch_results=args.enable_tts_file_batch_results,
            min_extracted_chars=args.min_extracted_chars,
            max_parallel_chapters=args.max_parallel_chapters,
            parallel_worker_mode=args.parallel_worker_mode,
            force_parallel_workers=args.force_parallel_workers,
            profile_tts_chunks=args.profile_tts_chunks,
        )
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0 if all(case.get("success") for case in summary["cases"]) else 1

    metrics = run_benchmark(
        source_path=Path(args.source),
        output_dir=args.output_dir,
        temp_dir=args.temp_dir,
        parse_only=parse_only,
        tts_engine=args.tts_engine,
        tts_model_name=args.tts_model,
        tts_quantization=args.tts_quantization,
        cleaner_backend=args.cleaner_backend,
        cleaner_model_name=args.cleaner_model,
        cleaner_cuda_device=args.cleaner_cuda_device,
        cleaner_cuda_quantization=args.cleaner_cuda_quantization,
        cleaner_skip_llm_when_not_needed=args.cleaner_skip_llm_when_not_needed,
        cleaner_gate=args.cleaner_gate,
        pdf_extraction_backend=args.pdf_backend,
        voice=args.voice,
        chunk_size=args.chunk_size,
        chapter_start=args.chapter_start,
        chapter_limit=args.chapter_limit,
        emit_chapter_mp3_artifacts=args.emit_chapter_mp3_artifacts,
        enable_chapter_aac_packaging=args.enable_chapter_aac_packaging,
        enable_background_chapter_aac_encoding=args.enable_background_chapter_aac_encoding,
        enable_chunk_overlap=args.enable_chunk_overlap,
        enable_tts_file_batch_results=args.enable_tts_file_batch_results,
        min_extracted_chars=args.min_extracted_chars,
        enable_parallel=not args.disable_parallel,
        max_parallel_chapters=args.max_parallel_chapters,
        parallel_worker_mode=args.parallel_worker_mode,
        force_parallel_workers=args.force_parallel_workers,
        profile_tts_chunks=args.profile_tts_chunks,
    )
    print(json.dumps(metrics, indent=2, sort_keys=True))
    return 0 if metrics.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
