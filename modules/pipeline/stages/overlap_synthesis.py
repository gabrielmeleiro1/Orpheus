"""Cleaner/TTS overlap orchestration for the synthesis stage."""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol

import numpy as np

from modules.pipeline.models import PipelineStage
from modules.pipeline.progress import chapter_progress_message
from modules.pipeline.runtime import PipelineRuntimeProtocol


class OverlapStageHooks(Protocol):
    """Stage-owned cleaning operations needed by overlap synthesis."""

    def log_tts_stage_start(self, *, is_cached: bool) -> None: ...

    def save_pre_clean_markdown(
        self,
        *,
        chapter: Any,
        output_dir: Path,
        original_chunks: list[str],
    ) -> None: ...

    def get_text_cleaner_for_chapter(self) -> Any: ...

    def maybe_capture_cleaner_stats(self, cleaner: Any) -> dict[str, int] | None: ...

    def clean_chunk_safely(
        self,
        *,
        cleaner: Any,
        chunk: str,
        llm_cleaning_enabled: bool,
    ) -> tuple[str, bool, dict[str, Any] | None]: ...

    def log_cleaner_stats_delta(
        self,
        stats_start: dict[str, int] | None,
        stats_end: dict[str, int] | None,
    ) -> None: ...

    def save_cleaning_artifacts(
        self,
        *,
        chapter: Any,
        output_dir: Path,
        cleaned_chunks: list[str],
        llm_input_chunks: list[str],
        cleaning_trace_rows: list[dict[str, Any]],
    ) -> None: ...


@dataclass(frozen=True)
class OverlapWorkItem:
    """Raw cleaner output produced ahead of synthesis."""

    chunk_idx: int
    original_chunk: str
    cleaned_chunk: str
    clean_trace: Any


@dataclass(frozen=True)
class OverlapProducerError:
    """Error raised by the cleaner producer and delivered to the consumer."""

    error: BaseException


@dataclass(frozen=True)
class OverlapChunkRecord:
    """Normalized overlap metadata consumed by synthesis/debug output."""

    chunk_idx: int
    cleaned_chunk: str
    llm_input_text: str
    trace_row: dict[str, Any] | None


def _build_overlap_trace_row(
    *,
    chunk_idx: int,
    total_chunks: int,
    original_chunk: str,
    cleaned_chunk: str,
    clean_trace: dict[str, Any],
) -> dict[str, Any]:
    rule_based_text = clean_trace.get("rule_based_text", original_chunk)
    return {
        "chunk_index": chunk_idx + 1,
        "total_chunks": total_chunks,
        "mode": clean_trace.get("mode"),
        "llm_error": clean_trace.get("llm_error"),
        "original_text": clean_trace.get("original_text", original_chunk),
        "rule_based_text": rule_based_text,
        "cleaned_text": clean_trace.get("cleaned_text", cleaned_chunk),
    }


def _build_overlap_chunk_record(
    *,
    item: OverlapWorkItem,
    total_chunks: int,
    include_debug_row: bool,
) -> OverlapChunkRecord:
    llm_input_text = item.original_chunk
    trace_row = None
    clean_trace = item.clean_trace
    if clean_trace is not None:
        llm_input_text = clean_trace.get("rule_based_text", item.original_chunk)
        if include_debug_row:
            trace_row = _build_overlap_trace_row(
                chunk_idx=item.chunk_idx,
                total_chunks=total_chunks,
                original_chunk=item.original_chunk,
                cleaned_chunk=item.cleaned_chunk,
                clean_trace=clean_trace,
            )
    return OverlapChunkRecord(
        chunk_idx=item.chunk_idx,
        cleaned_chunk=item.cleaned_chunk,
        llm_input_text=llm_input_text,
        trace_row=trace_row,
    )


def _apply_overlap_chunk_record(
    *,
    record: OverlapChunkRecord,
    cleaned_chunks: list[str],
    llm_input_chunks: list[str],
    cleaning_trace_rows: list[dict[str, Any]],
) -> None:
    cleaned_chunks[record.chunk_idx] = record.cleaned_chunk
    llm_input_chunks[record.chunk_idx] = record.llm_input_text
    if record.trace_row is not None:
        cleaning_trace_rows.append(record.trace_row)


def _notify_overlap_synthesis_progress(
    runtime: PipelineRuntimeProtocol,
    *,
    chapter_idx: int,
    total_chapters: int,
    chunk_idx: int,
    total_chunks: int,
) -> None:
    runtime.notify_progress(
        chapter_idx=chapter_idx,
        total_chapters=total_chapters,
        chunk_idx=chunk_idx,
        total_chunks=total_chunks,
        message=chapter_progress_message(
            chapter_idx, total_chapters, f"synthesizing chunk {chunk_idx + 1}/{total_chunks}",
        ),
    )


def _synthesize_overlap_chunk_audio(
    runtime: PipelineRuntimeProtocol,
    *,
    chunk_idx: int,
    cleaned_chunk: str,
    source: str = "",
) -> Optional[np.ndarray]:
    from modules.tts.speech_normalizer import SpeechNormalizationConfig, normalize_for_speech
    from modules.tts.speech_typography import normalize_speech_typography

    speech_text = normalize_for_speech(
        normalize_speech_typography(cleaned_chunk, source),
        SpeechNormalizationConfig(
            enabled=bool(getattr(runtime.config, "speech_normalization", False))
        ),
    )
    try:
        audio = runtime.synthesize_speech(
            text=speech_text,
            voice=runtime.config.voice,
            speed=runtime.config.speed,
        )
    except Exception as exc:
        runtime.log_verbose(f"[TTS] Error on chunk {chunk_idx + 1}: {exc}", "error")
        return None
    return np.asarray(audio, dtype=np.float32)


class OverlapSynthesisRunner:
    """Run cleaner production and TTS consumption without unbounded queue waits."""

    def __init__(
        self,
        runtime: PipelineRuntimeProtocol,
        hooks: OverlapStageHooks,
        *,
        chapter: Any,
        chapter_idx: int,
        total_chapters: int,
        chunks: list[str],
        output_dir: Path,
        queue_poll_seconds: float = 0.1,
        chapter_result: Any | None = None,
    ) -> None:
        self.runtime = runtime
        self.hooks = hooks
        self.chapter = chapter
        self.chapter_idx = chapter_idx
        self.total_chapters = total_chapters
        self.chunks = chunks
        self.output_dir = output_dir
        self.chapter_result = chapter_result
        self.total_chunks = len(chunks)
        self.queue_poll_seconds = max(0.01, float(queue_poll_seconds))
        self.work_queue: queue.Queue[Any] = queue.Queue(maxsize=4)
        self.sentinel = object()
        self.cancelled_sentinel = object()
        self.cancel_event = threading.Event()

    def run(self) -> tuple[Optional[Path | np.ndarray], bool]:
        if self.total_chunks == 0:
            return None, False

        from .synthesis import (
            ChapterAudioAccumulator,
            _finalize_chapter_audio,
            _max_in_memory_chapter_samples,
            _pipeline_sample_rate,
            _start_tts_stage,
        )

        self.runtime.set_stage(PipelineStage.CLEANING)
        self.hooks.save_pre_clean_markdown(
            chapter=self.chapter,
            output_dir=self.output_dir,
            original_chunks=self.chunks.copy(),
        )

        cleaner = self.hooks.get_text_cleaner_for_chapter()
        cleaner_stats_start = self.hooks.maybe_capture_cleaner_stats(cleaner)

        cleaned_chunks: list[str] = [""] * self.total_chunks
        llm_input_chunks: list[str] = [""] * self.total_chunks
        cleaning_trace_rows: list[dict[str, Any]] = []

        producer_thread = self._start_cleaner_producer(cleaner)
        _start_tts_stage(self.runtime, self.hooks)
        chapter_wav = self.runtime.config.temp_dir / f"chapter_{self.chapter.number:02d}.wav"
        self.runtime.track_temp_cleanup_path(chapter_wav)
        accumulator = ChapterAudioAccumulator(
            output_path=chapter_wav,
            sample_rate=_pipeline_sample_rate(self.runtime),
            max_in_memory_samples=_max_in_memory_chapter_samples(self.runtime),
        )
        was_cancelled = False
        producer_error: BaseException | None = None
        synthesis_failed = False
        synthesized_chunks = 0

        try:
            while True:
                item = self._next_queue_item()
                if item is self.cancelled_sentinel:
                    was_cancelled = True
                    break
                if item is self.sentinel:
                    break
                if isinstance(item, OverlapProducerError):
                    producer_error = item.error
                    break

                record = _build_overlap_chunk_record(
                    item=item,
                    total_chunks=self.total_chunks,
                    include_debug_row=self.runtime.config.save_cleaning_debug,
                )
                _apply_overlap_chunk_record(
                    record=record,
                    cleaned_chunks=cleaned_chunks,
                    llm_input_chunks=llm_input_chunks,
                    cleaning_trace_rows=cleaning_trace_rows,
                )
                if synthesis_failed:
                    # The chapter is already incomplete and will be discarded;
                    # keep draining cleaned text for artifacts but spend no
                    # more TTS time on it.
                    continue
                _notify_overlap_synthesis_progress(
                    self.runtime,
                    chapter_idx=self.chapter_idx,
                    total_chapters=self.total_chapters,
                    chunk_idx=record.chunk_idx,
                    total_chunks=self.total_chunks,
                )

                audio_array = _synthesize_overlap_chunk_audio(
                    self.runtime,
                    chunk_idx=record.chunk_idx,
                    cleaned_chunk=record.cleaned_chunk,
                    source=str(getattr(self.chapter, "content", "") or ""),
                )
                if audio_array is None or len(audio_array) == 0:
                    synthesis_failed = True
                    continue

                accumulator.add_segment(audio_array)
                synthesized_chunks += 1
                self.runtime.add_processed_chars(len(record.cleaned_chunk))
        except BaseException:
            accumulator.close()
            raise
        finally:
            self.cancel_event.set()
            producer_thread.join(timeout=0.05 if was_cancelled else 1.0)

        if producer_error is not None:
            accumulator.close()
            raise RuntimeError(f"Overlap cleaner producer failed: {producer_error}") from producer_error

        if was_cancelled:
            accumulator.close()
            return None, True

        cleaner_stats_end = self.hooks.maybe_capture_cleaner_stats(cleaner)
        self.hooks.log_cleaner_stats_delta(cleaner_stats_start, cleaner_stats_end)
        self.hooks.save_cleaning_artifacts(
            chapter=self.chapter,
            output_dir=self.output_dir,
            cleaned_chunks=cleaned_chunks,
            llm_input_chunks=llm_input_chunks,
            cleaning_trace_rows=cleaning_trace_rows,
        )

        if synthesis_failed or synthesized_chunks != self.total_chunks:
            accumulator.close()
            return None, False

        return _finalize_chapter_audio(self.runtime, accumulator, self.chapter_result), False

    def _next_queue_item(self) -> Any:
        while True:
            if self.runtime.check_cancelled():
                self.cancel_event.set()
                return self.cancelled_sentinel
            try:
                return self.work_queue.get(timeout=self.queue_poll_seconds)
            except queue.Empty:
                continue

    def _start_cleaner_producer(self, cleaner: Any) -> threading.Thread:
        producer_thread = threading.Thread(
            target=self._produce_cleaned_chunks,
            args=(cleaner,),
            daemon=True,
        )
        register = getattr(self.runtime, "register_background_worker", None)
        if callable(register):
            register(producer_thread)
        producer_thread.start()
        return producer_thread

    def _put_queue_item(self, item: Any) -> None:
        while not self.cancel_event.is_set():
            try:
                self.work_queue.put(item, timeout=self.queue_poll_seconds)
                return
            except queue.Full:
                continue

    def _produce_cleaned_chunks(self, cleaner: Any) -> None:
        try:
            llm_cleaning_enabled = True
            for idx, chunk in enumerate(self.chunks):
                if self.cancel_event.is_set():
                    break
                cleaned_chunk, llm_cleaning_enabled, clean_trace = (
                    self.hooks.clean_chunk_safely(
                        cleaner=cleaner,
                        chunk=chunk,
                        llm_cleaning_enabled=llm_cleaning_enabled,
                    )
                )
                self._put_queue_item(
                    OverlapWorkItem(
                        chunk_idx=idx,
                        original_chunk=chunk,
                        cleaned_chunk=cleaned_chunk,
                        clean_trace=clean_trace,
                    )
                )
        except Exception as exc:
            self._put_queue_item(OverlapProducerError(exc))
        finally:
            self._put_queue_item(self.sentinel)
            unregister = getattr(self.runtime, "unregister_background_worker", None)
            if callable(unregister):
                unregister(threading.current_thread())
