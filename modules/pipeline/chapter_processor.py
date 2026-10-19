"""Single-chapter processing for the conversion pipeline."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
import time
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    import numpy as np

from modules.async_bridge import run_coroutine_sync as _run_coroutine_sync
from modules.contracts.recovery_inventory import chapter_audio_path

from .models import ChapterResult, PipelineStage
from .runtime import PipelineRuntime
from .progress import chapter_progress_message
from .stage_metrics import timed_stage

logger = logging.getLogger(__name__)


class ChapterProcessor:
    """Owns chunk, clean, synthesize, and encode work for one chapter."""

    def __init__(self, runtime: PipelineRuntime) -> None:
        self.runtime = runtime
        self._stage_timings: dict[str, float] | None = None

    def begin_stage_timing_capture(
        self,
        stage_timings: dict[str, float],
    ) -> dict[str, float] | None:
        previous_stage_timings = self._stage_timings
        self._stage_timings = stage_timings
        return previous_stage_timings

    def restore_stage_timing_capture(
        self,
        previous_stage_timings: dict[str, float] | None,
    ) -> None:
        self._stage_timings = previous_stage_timings

    @staticmethod
    def process_chapter_worker(
        chapter: Any,
        chapter_idx: int,
        output_dir: Path,
        total_chapters: Optional[int] = None,
        tts_engine: Any = None,
        text_cleaner: Any = None,
    ) -> ChapterResult:
        from .chapter_runners import process_chapter_worker

        return process_chapter_worker(
            chapter=chapter,
            chapter_idx=chapter_idx,
            output_dir=output_dir,
            total_chapters=total_chapters,
            tts_engine=tts_engine,
            text_cleaner=text_cleaner,
        )

    @staticmethod
    def _should_degrade_sequential_from_memory_errors(
        results: list[ChapterResult],
    ) -> bool:
        """Return True when parallel worker failures indicate memory pressure."""
        from .retry import should_degrade_sequential_from_memory_errors

        return should_degrade_sequential_from_memory_errors(results)

    @staticmethod
    def _should_retry_sequential_from_parallel_errors(
        results: list[ChapterResult],
    ) -> bool:
        """
        Return True when parallel worker failures are likely infrastructure-level.
        """
        from .retry import should_retry_sequential_from_parallel_errors

        return should_retry_sequential_from_parallel_errors(results)

    def clean_chunk_safely(
        self,
        *,
        cleaner: Any,
        chunk: str,
        llm_cleaning_enabled: bool,
    ) -> tuple[str, bool, Optional[dict[str, Any]]]:
        """
        Clean a chunk and degrade to rule-based mode on infrastructure-level LLM failures.
        """
        from .retry import clean_chunk_safely

        return clean_chunk_safely(
            runtime=self.runtime,
            cleaner=cleaner,
            chunk=chunk,
            llm_cleaning_enabled=llm_cleaning_enabled,
        )

    def retry_batch_results_serially(
        self,
        *,
        batch_items: list[Any],
        batch_results: list[Any],
        total_chunks: int,
        on_batch_progress: Callable[[int, int], None],
    ) -> list[Any]:
        """
        Retry chunk synthesis serially when grouped synthesis fails for all chunks.
        """
        from .retry import retry_batch_results_serially

        return retry_batch_results_serially(
            runtime=self.runtime,
            batch_items=batch_items,
            batch_results=batch_results,
            total_chunks=total_chunks,
            on_batch_progress=on_batch_progress,
        )

    @timed_stage("tts_model_load")
    def ensure_chapter_tts_engine(self):
        """Create or reuse the chapter TTS engine based on execution mode."""
        from .model_lifecycle import ensure_chapter_tts_engine

        return ensure_chapter_tts_engine(self.runtime)

    def chunk_chapter_content(
        self,
        *,
        chapter: Any,
        chapter_idx: int,
        total_chapters: int,
    ) -> list[str]:
        """Run chapter chunking stage and return text chunks."""
        from modules.tts.chunker import TextChunker, ChunkConfig
        from .chapter_narration import chapter_body

        self.runtime.set_stage(PipelineStage.CHUNKING)
        self.runtime.notify_progress(
            chapter_idx=chapter_idx,
            total_chapters=total_chapters,
            message=chapter_progress_message(chapter_idx, total_chapters, "chunking"),
        )

        chunk_config = ChunkConfig(max_tokens=self.runtime.config.chunk_size)
        chunker = TextChunker(config=chunk_config)
        chunks = chunker.chunk(chapter_body(chapter))
        self.runtime.log_verbose(f"[CHUNKER] Split into {len(chunks)} chunks", "info")
        return chunks

    def get_text_cleaner_for_chapter(self):
        """
        Return cleaner for chapter processing.

        In sequential mode the cleaner is cached; in parallel mode each worker uses
        a fresh cleaner instance.
        """
        from .model_lifecycle import get_text_cleaner_for_chapter

        return get_text_cleaner_for_chapter(self.runtime)

    def save_pre_clean_markdown(
        self,
        *,
        chapter: Any,
        output_dir: Path,
        original_chunks: list[str],
    ) -> None:
        """Persist pre-clean chunk text for debugging when enabled."""
        from .stages.cleaning import save_pre_clean_markdown

        save_pre_clean_markdown(
            runtime=self.runtime,
            chapter=chapter,
            output_dir=output_dir,
            original_chunks=original_chunks,
        )

    def _clean_chunks_with_trace(
        self,
        chapter_idx: int,
        total_chapters: int,
        chunks: list[str],
        cleaner: Any,
    ) -> tuple[list[str], list[str], list[dict[str, Any]], bool]:
        from .stages.cleaning import clean_chunks_with_trace

        return clean_chunks_with_trace(
            runtime=self.runtime,
            hooks=self,
            chapter_idx=chapter_idx,
            total_chapters=total_chapters,
            chunks=chunks,
            cleaner=cleaner,
        )

    @staticmethod
    def run_coroutine_sync(coro):
        """Run coroutine from sync pipeline code, even when a loop already exists."""
        return _run_coroutine_sync(coro)


    @staticmethod
    def maybe_capture_cleaner_stats(cleaner: Any) -> Optional[dict[str, int]]:
        """Capture cleaner stats snapshot if supported by cleaner runtime."""
        stats_snapshot_fn = getattr(cleaner, "stats_snapshot", None)
        if not callable(stats_snapshot_fn):
            return None
        return stats_snapshot_fn()

    def log_cleaner_stats_delta(
        self,
        stats_start: Optional[dict[str, int]],
        stats_end: Optional[dict[str, int]],
    ) -> None:
        """Log cleaner usage deltas between two stats snapshots."""
        from .stages.cleaning import log_cleaner_stats_delta

        log_cleaner_stats_delta(
            runtime=self.runtime,
            stats_start=stats_start,
            stats_end=stats_end,
        )

    def save_cleaning_artifacts(
        self,
        *,
        chapter: Any,
        output_dir: Path,
        cleaned_chunks: list[str],
        llm_input_chunks: list[str],
        cleaning_trace_rows: list[dict[str, Any]],
    ) -> None:
        """Persist cleaned output and optional debug artifacts."""
        from .stages.cleaning import save_cleaning_artifacts

        save_cleaning_artifacts(
            runtime=self.runtime,
            chapter=chapter,
            output_dir=output_dir,
            cleaned_chunks=cleaned_chunks,
            llm_input_chunks=llm_input_chunks,
            cleaning_trace_rows=cleaning_trace_rows,
        )

    @timed_stage("clean")
    def clean_chapter_content(
        self,
        *,
        chapter: Any,
        chapter_idx: int,
        total_chapters: int,
        chunks: list[str],
        output_dir: Path,
    ) -> tuple[list[str], bool]:
        """
        Run cleaner stage for all chunks.

        Returns:
            Tuple of (cleaned_chunks, was_cancelled).
        """
        self.runtime.set_stage(PipelineStage.CLEANING)

        original_chunks = chunks.copy()
        self.save_pre_clean_markdown(
            chapter=chapter,
            output_dir=output_dir,
            original_chunks=original_chunks,
        )

        text_cleaner = self.get_text_cleaner_for_chapter()

        self.runtime.log_verbose("[STATUS] LLM (Text Cleaner) running...", "process")
        cleaner_stats_start = self.maybe_capture_cleaner_stats(text_cleaner)

        cleaned_chunks, llm_input_chunks, cleaning_trace_rows, was_cancelled = (
            self._clean_chunks_with_trace(
                chapter_idx=chapter_idx,
                total_chapters=total_chapters,
                chunks=chunks,
                cleaner=text_cleaner,
            )
        )
        if was_cancelled:
            return cleaned_chunks, True

        cleaner_stats_end = self.maybe_capture_cleaner_stats(text_cleaner)
        self.log_cleaner_stats_delta(cleaner_stats_start, cleaner_stats_end)

        self.save_cleaning_artifacts(
            chapter=chapter,
            output_dir=output_dir,
            cleaned_chunks=cleaned_chunks,
            llm_input_chunks=llm_input_chunks,
            cleaning_trace_rows=cleaning_trace_rows,
        )

        return cleaned_chunks, False

    def log_tts_stage_start(self, *, is_cached: bool) -> None:
        """Emit consistent TTS stage start logs."""
        if is_cached:
            self.runtime.log_verbose(
                f"[TTS] Using cached TTS Model: {self.runtime.config.tts_model_name}-{self.runtime.config.tts_quantization}",
                "success",
            )
        else:
            self.runtime.log_verbose(
                f"[TTS] Loading TTS Model: {self.runtime.config.tts_model_name}-{self.runtime.config.tts_quantization}",
                "process",
            )
        self.runtime.log_verbose("[STATUS] TTS (Text-to-Speech) running...", "process")

        if is_cached:
            self.runtime.log_verbose(
                "[TTS] Starting multi-chunk synthesis with cached model", "success"
            )
        else:
            self.runtime.log_verbose(
                "[TTS] Model loaded - Starting multi-chunk synthesis", "success"
            )

    @timed_stage("tts")
    def synthesize_chapter_chunks(
        self,
        *,
        chapter: Any,
        chapter_idx: int,
        total_chapters: int,
        chunks: list[str],
        chapter_result: ChapterResult | None = None,
    ) -> Optional[Path | np.ndarray]:
        """Run TTS stage for chapter chunks and return chapter audio payload."""
        from .stages.synthesis import synthesize_chapter_chunks

        return synthesize_chapter_chunks(
            runtime=self.runtime,
            hooks=self,
            chapter=chapter,
            chapter_idx=chapter_idx,
            total_chapters=total_chapters,
            chunks=chunks,
            chapter_result=chapter_result,
        )

    @timed_stage("clean_tts_overlap")
    def clean_and_synthesize_with_overlap(
        self,
        *,
        chapter: Any,
        chapter_idx: int,
        total_chapters: int,
        chunks: list[str],
        output_dir: Path,
        chapter_result: ChapterResult | None = None,
    ) -> tuple[Optional[Path | np.ndarray], bool]:
        """Overlap cleaner production and TTS synthesis for a chapter."""
        from .stages.synthesis import clean_and_synthesize_with_overlap

        return clean_and_synthesize_with_overlap(
            runtime=self.runtime,
            hooks=self,
            chapter=chapter,
            chapter_idx=chapter_idx,
            total_chapters=total_chapters,
            chunks=chunks,
            output_dir=output_dir,
            chapter_result=chapter_result,
        )

    def _record_tts_throughput(self, chunks: list[str], elapsed_seconds: float) -> None:
        if elapsed_seconds <= 0:
            return
        char_count = sum(len(chunk) for chunk in chunks)
        if char_count <= 0:
            return
        self.runtime.record_tts_throughput(char_count, elapsed_seconds)

    def _record_encode_throughput(self, elapsed_seconds: float) -> None:
        if elapsed_seconds <= 0:
            return
        self.runtime.record_encode_throughput(elapsed_seconds)

    def record_parallel_stage_throughput(
        self,
        results: list[ChapterResult],
        chapters: list[Any],
    ) -> None:
        """Record throughput from completed parallel chapter results."""
        chapters_by_number = {
            int(getattr(chapter, "number", index + 1)): chapter
            for index, chapter in enumerate(chapters)
        }
        for result in results:
            if result.error:
                continue
            timings = result.stage_timings or {}
            tts_seconds = float(timings.get("tts") or timings.get("clean_tts_overlap") or 0.0)
            if tts_seconds > 0:
                chapter = chapters_by_number.get(int(result.chapter_number))
                content = str(getattr(chapter, "content", "") or "") if chapter is not None else ""
                self._record_tts_throughput([content], tts_seconds)
            encode_seconds = float(timings.get("encode") or 0.0)
            if encode_seconds > 0:
                self._record_encode_throughput(encode_seconds)

    @timed_stage("encode")
    def encode_chapter_audio(
        self,
        *,
        chapter: Any,
        chapter_idx: int,
        total_chapters: int,
        output_dir: Path,
        chapter_wav: Path | np.ndarray,
        precomputed_rms: float | None = None,
    ) -> tuple[Path, int]:
        """Encode chapter WAV/audio payload to MP3 and return path plus duration."""
        from .stages.encoding import EncodingStageContext, encode_chapter_audio

        sample_rate = self.runtime.tts_sample_rate()
        context = EncodingStageContext(
            config=self.runtime.config,
            sample_rate=sample_rate,
            encoding_stage=PipelineStage.ENCODING,
            set_stage=self.runtime.set_stage,
            notify_progress=self.runtime.notify_progress,
            log_verbose=self.runtime.log_verbose,
            cancel_requested=self.runtime.check_cancelled,
        )

        return encode_chapter_audio(
            context=context,
            chapter=chapter,
            chapter_idx=chapter_idx,
            total_chapters=total_chapters,
            output_dir=output_dir,
            chapter_wav=chapter_wav,
            precomputed_rms=precomputed_rms,
        )

    def _chapter_aac_output_path(self, chapter: Any, output_dir: Path) -> Path:
        """Choose temp vs durable AAC output based on whether MP3s are emitted.

        AAC-only conversions persist the per-chapter .m4a under the chapters
        output dir (reused as chapters.mp3_path) so repair can resolve it after
        temp cleanup. When MP3 artifacts are emitted, AAC stays packaging-only
        under temp_dir.
        """
        if self.runtime.config.emit_chapter_mp3_artifacts:
            return self.runtime.config.temp_dir / f"chapter_{chapter.number:02d}.m4a"
        return chapter_audio_path(chapter, output_dir, ".m4a")

    def _await_chapter_aac_slot(self) -> None:
        """Wait for the in-flight background AAC encode before queueing another."""
        pending = self.runtime.state.chapter_aac_pending_future
        if pending is None:
            return
        try:
            pending.result()
        except Exception:
            # Encoder failures are surfaced when packaging resolves chapter futures.
            pass
        finally:
            self.runtime.state.chapter_aac_pending_future = None

    def _ensure_chapter_aac(
        self,
        *,
        result: ChapterResult,
        chapter: Any,
        encoding_source: Path,
        output_dir: Path,
    ) -> None:
        if not getattr(self.runtime.config, "enable_chapter_aac_packaging", False):
            return
        if result.aac_path is not None and result.aac_path.exists():
            if (
                not self.runtime.config.emit_chapter_mp3_artifacts
                and result.mp3_path is None
            ):
                result.mp3_path = result.aac_path
            self._release_chapter_master(result, encoding_source)
            return

        from .stages.encoding import EncodingStageContext, encode_chapter_aac

        aac_output = self._chapter_aac_output_path(chapter, output_dir)
        durable_chapter_audio = not self.runtime.config.emit_chapter_mp3_artifacts

        def encode() -> Path:
            sample_rate = self.runtime.tts_sample_rate()
            context = EncodingStageContext(
                config=self.runtime.config,
                sample_rate=sample_rate,
                encoding_stage=PipelineStage.ENCODING,
                set_stage=self.runtime.set_stage,
                notify_progress=self.runtime.notify_progress,
                log_verbose=self.runtime.log_verbose,
                cancel_requested=self.runtime.check_cancelled,
            )
            encoded = encode_chapter_aac(
                context=context,
                chapter=chapter,
                chapter_wav=encoding_source,
                precomputed_rms=result.pcm_rms,
                output_path=aac_output,
            )
            if encoded.is_file():
                self._release_chapter_master(result, encoding_source)
            return encoded

        if bool(getattr(self.runtime.config, "enable_background_chapter_aac_encoding", True)):
            if self.runtime.check_cancelled():
                return
            self._await_chapter_aac_slot()
            executor = self.runtime.state.chapter_aac_executor
            if executor is None:
                executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chapter-aac")
                self.runtime.state.chapter_aac_executor = executor
            # Track the temp output at submit time: failed or cancelled runs never
            # reach packaging (the only other place that tracks it), which would
            # orphan the encoded .m4a in the temp root. The tracker ignores paths
            # outside temp_dir, so durable repair chapter audio stays exempt.
            self.runtime.track_temp_cleanup_path(aac_output)
            result.aac_future = executor.submit(encode)
            self.runtime.state.chapter_aac_pending_future = result.aac_future
            return

        result.aac_path = encode()
        if durable_chapter_audio:
            result.mp3_path = result.aac_path
        self.runtime.track_temp_cleanup_path(result.aac_path)

    def _release_chapter_master(self, result: ChapterResult, master: Path) -> None:
        """Release temporary PCM after every required encode has succeeded."""
        if master.resolve().is_relative_to(self.runtime.config.temp_dir.resolve()):
            master.unlink(missing_ok=True)
            result.wav_path = None

    def prepare_chapter_audio(
        self,
        *,
        chapter: Any,
        chapter_idx: int,
        total_chapters: int,
        output_dir: Path,
    ) -> tuple[ChapterResult, Optional[Path | np.ndarray], bool]:
        """
        Prepare chapter audio up to (but excluding) MP3 encoding.

        Returns:
            Tuple of (chapter_result, wav_or_audio, was_cancelled).
        """
        result = ChapterResult(
            chapter_number=chapter.number,
            chapter_title=chapter.title,
        )
        self.runtime.log_verbose(
            f"[CHAPTER {chapter.number}] Processing: {chapter.title}", "process"
        )

        if chapter_idx == 0:
            self.runtime.log_verbose(
                f"[STATUS] Starting first chapter: {chapter.title}", "process"
            )

        try:
            self.ensure_chapter_tts_engine()

            chunks = self.chunk_chapter_content(
                chapter=chapter,
                chapter_idx=chapter_idx,
                total_chapters=total_chapters,
            )

            if len(chunks) == 0:
                from .chapter_narration import add_chapter_narration, chapter_body

                if getattr(chapter, "heading_text", "") and not chapter_body(chapter).strip():
                    import numpy as np

                    narrated = add_chapter_narration(
                        self.runtime, chapter, np.empty(0, dtype=np.float32), result,
                    )
                    if narrated is None:
                        result.error = "Cancelled"
                    return result, narrated, narrated is None
                result.error = "No text content"
                self.runtime.log_verbose(f"[CHAPTER {chapter.number}] Empty chapter!", "error")
                return result, None, False

            use_chunk_overlap = self.runtime.config.enable_chunk_overlap and len(chunks) > 1
            if use_chunk_overlap:
                tts_started = time.perf_counter()
                chapter_wav, was_cancelled = self.clean_and_synthesize_with_overlap(
                    chapter=chapter,
                    chapter_idx=chapter_idx,
                    total_chapters=total_chapters,
                    chunks=chunks,
                    output_dir=output_dir,
                    chapter_result=result,
                )
                tts_seconds = time.perf_counter() - tts_started
                if was_cancelled:
                    result.error = "Cancelled"
                    return result, None, True
                if chapter_wav is None:
                    result.error = "No audio generated"
                    return result, None, False
                self._record_tts_throughput(chunks, tts_seconds)
                from .chapter_narration import add_chapter_narration

                narrated = add_chapter_narration(self.runtime, chapter, chapter_wav, result)
                if narrated is None:
                    result.error = "Cancelled"
                return result, narrated, narrated is None

            chunks, was_cancelled = self.clean_chapter_content(
                chapter=chapter,
                chapter_idx=chapter_idx,
                total_chapters=total_chapters,
                chunks=chunks,
                output_dir=output_dir,
            )
            if was_cancelled:
                result.error = "Cancelled"
                return result, None, True

            tts_started = time.perf_counter()
            chapter_wav = self.synthesize_chapter_chunks(
                chapter=chapter,
                chapter_idx=chapter_idx,
                total_chapters=total_chapters,
                chunks=chunks,
                chapter_result=result,
            )
            tts_seconds = time.perf_counter() - tts_started
            if chapter_wav is None:
                result.error = "No audio generated"
                return result, None, False
            self._record_tts_throughput(chunks, tts_seconds)
            from .chapter_narration import add_chapter_narration

            narrated = add_chapter_narration(self.runtime, chapter, chapter_wav, result)
            if narrated is None:
                result.error = "Cancelled"
            return result, narrated, narrated is None
        except Exception as exc:
            result.error = str(exc)
            self.runtime.log_verbose(
                f"[CHAPTER {chapter.number}] Failed: {result.error}",
                "error",
            )
            self.runtime.log_verbose(
                f"[CHAPTER {chapter.number}] Traceback:\n{traceback.format_exc()}",
                "error",
            )
            logger.exception(f"Chapter {chapter.number} preparation failed")
            return result, None, False

    def finalize_chapter_encoding(
        self,
        *,
        result: ChapterResult,
        chapter_wav: Path | np.ndarray,
        chapter: Any,
        chapter_idx: int,
        total_chapters: int,
        output_dir: Path,
    ) -> ChapterResult:
        """Finalize chapter by encoding prepared WAV/audio payload to MP3."""
        if isinstance(chapter_wav, Path):
            result.wav_path = chapter_wav
            encoding_source = chapter_wav
            self.runtime.track_temp_cleanup_path(chapter_wav)
        else:
            from .stages.encoding import write_wav_from_array

            sample_rate = self.runtime.tts_sample_rate()
            master_dir = self.runtime.config.temp_dir / "chapter_masters"
            result.wav_path = master_dir / f"chapter_{chapter.number:02d}.wav"
            write_wav_from_array(
                chapter_wav,
                result.wav_path,
                sample_rate=sample_rate,
            )
            self.runtime.track_temp_cleanup_path(master_dir)
            encoding_source = result.wav_path

        if not self.runtime.config.emit_chapter_mp3_artifacts:
            from .stages.encoding import _measure_source_duration_ms

            sample_rate = self.runtime.tts_sample_rate()
            encode_started = time.perf_counter()
            result.duration_ms = _measure_source_duration_ms(
                encoding_source,
                sample_rate=sample_rate,
            )
            self._ensure_chapter_aac(
                result=result,
                chapter=chapter,
                encoding_source=encoding_source,
                output_dir=output_dir,
            )
            self._record_encode_throughput(time.perf_counter() - encode_started)
            self.runtime.log_verbose(
                f"[ENCODER] Skipped chapter MP3 artifact ({result.duration_ms / 1000:.1f}s)",
                "success",
            )
            self.runtime.log_verbose(
                f"[CHAPTER {chapter.number}] TTS Complete - Audio ready", "success"
            )
            if chapter_idx == 0:
                self.runtime.log_verbose(
                    f"[STATUS] First chapter finished: {chapter.title}", "success"
                )
            return result

        encode_started = time.perf_counter()
        chapter_mp3, duration_ms = self.encode_chapter_audio(
            chapter=chapter,
            chapter_idx=chapter_idx,
            total_chapters=total_chapters,
            output_dir=output_dir,
            chapter_wav=encoding_source,
            precomputed_rms=result.pcm_rms,
        )
        self._record_encode_throughput(time.perf_counter() - encode_started)
        result.mp3_path = chapter_mp3
        result.duration_ms = duration_ms
        self._ensure_chapter_aac(
            result=result,
            chapter=chapter,
            encoding_source=encoding_source,
            output_dir=output_dir,
        )

        self.runtime.log_verbose(
            f"[ENCODER] Complete: {chapter_mp3.name} ({result.duration_ms / 1000:.1f}s)",
            "success",
        )
        self.runtime.log_verbose(
            f"[CHAPTER {chapter.number}] TTS Complete - Audio ready", "success"
        )

        if chapter_idx == 0:
            self.runtime.log_verbose(
                f"[STATUS] First chapter finished: {chapter.title}", "success"
            )

        return result

    def process_chapter(
        self,
        chapter: Any,
        chapter_idx: int,
        total_chapters: int,
        output_dir: Path,
    ) -> ChapterResult:
        """
        Process a single chapter: chunk → clean → synthesize → encode.
        """
        self.runtime.begin_active_chapter(chapter_idx, str(chapter.title))
        result: Optional[ChapterResult] = None
        result, chapter_wav, was_cancelled = self.prepare_chapter_audio(
            chapter=chapter,
            chapter_idx=chapter_idx,
            total_chapters=total_chapters,
            output_dir=output_dir,
        )
        try:
            if was_cancelled or result.error or chapter_wav is None:
                return result
            return self.finalize_chapter_encoding(
                result=result,
                chapter_wav=chapter_wav,
                chapter=chapter,
                chapter_idx=chapter_idx,
                total_chapters=total_chapters,
                output_dir=output_dir,
            )
        except Exception as exc:
            result.error = str(exc)
            self.runtime.log_verbose(
                f"[CHAPTER {chapter.number}] Failed: {result.error}",
                "error",
            )
            self.runtime.log_verbose(
                f"[CHAPTER {chapter.number}] Traceback:\n{traceback.format_exc()}",
                "error",
            )
            logger.exception(f"Chapter {chapter.number} processing failed")
            return result
        finally:
            if result is not None:
                self.runtime.finish_active_chapter(chapter_idx, failed=bool(result.error))
