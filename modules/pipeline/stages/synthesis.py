"""Pipeline synthesis-stage helpers."""

from __future__ import annotations

import inspect
import wave
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional, Protocol

import numpy as np

from modules.pipeline.models import PipelineStage
from modules.pipeline.runtime import PipelineRuntimeProtocol

_DEFAULT_MAX_IN_MEMORY_CHAPTER_SAMPLES = 30_000_000  # ~120MB float32 audio buffer


class SynthesisStageHooks(Protocol):
    """Stage-owned operations needed by synthesis orchestration."""

    def log_tts_stage_start(self, *, is_cached: bool) -> None: ...

    def retry_batch_results_serially(
        self,
        *,
        batch_items: list[Any],
        batch_results: list[Any],
        total_chunks: int,
        on_batch_progress: Any,
    ) -> list[Any]: ...


@dataclass(frozen=True)
class ValidBatchSynthesis:
    """Successful synthesis results plus their total sample count."""

    results: list[Any]
    total_samples: int


class ChapterAudioAccumulator:
    """Accumulate chapter audio in memory until it must spill to WAV."""

    def __init__(
        self,
        *,
        output_path: Path,
        sample_rate: int,
        max_in_memory_samples: int,
    ) -> None:
        self.output_path = output_path
        self.sample_rate = sample_rate
        self.max_in_memory_samples = max_in_memory_samples
        self._segments: list[np.ndarray] = []
        self._sample_count = 0
        self._writer: Any = None

    @property
    def is_streaming(self) -> bool:
        return self._writer is not None

    def add_segment(self, audio: np.ndarray) -> None:
        audio_array = np.asarray(audio, dtype=np.float32)
        if self.is_streaming:
            _write_audio_segment(self._writer, audio_array)
            return

        projected_sample_count = self._sample_count + int(len(audio_array))
        if projected_sample_count <= self.max_in_memory_samples:
            self._segments.append(audio_array)
            self._sample_count = projected_sample_count
            return

        self.spill_to_disk()
        _write_audio_segment(self._writer, audio_array)

    def add_file(self, audio_path: Path) -> None:
        if not self.is_streaming:
            self.spill_to_disk()
        _write_wav_file_segment(self._writer, audio_path, self.sample_rate)

    def spill_to_disk(self) -> None:
        if self.is_streaming:
            return

        self._writer = _open_chapter_wav_writer(self.output_path, self.sample_rate)
        for buffered in self._segments:
            _write_audio_segment(self._writer, buffered)
        self._segments.clear()
        self._sample_count = 0

    def finalize(self) -> Optional[Path | np.ndarray]:
        if self.is_streaming:
            self.close()
            return self.output_path
        if not self._segments:
            return None
        if len(self._segments) == 1:
            return self._segments[0]
        return np.concatenate(self._segments)

    def close(self) -> None:
        if self._writer is None:
            return
        try:
            self._writer.close()
        except Exception:
            pass
        finally:
            self._writer = None


def _max_in_memory_chapter_samples(runtime: PipelineRuntimeProtocol) -> int:
    configured = getattr(runtime.config, "max_in_memory_chapter_samples", None)
    if configured is None:
        return _DEFAULT_MAX_IN_MEMORY_CHAPTER_SAMPLES
    return max(1, int(configured))


def _pipeline_sample_rate(runtime: PipelineRuntimeProtocol) -> int:
    """Return the active TTS runtime sample rate for chapter artifacts."""
    return runtime.tts_sample_rate()


def _open_chapter_wav_writer(output_path: Path, sample_rate: int = 24000):
    """Open a mono PCM_16 WAV writer for streamed chapter audio."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    writer = wave.open(str(output_path), "wb")
    writer.setnchannels(1)
    writer.setsampwidth(2)
    writer.setframerate(sample_rate)
    return writer


def _write_audio_segment(writer: Any, audio: np.ndarray) -> None:
    """Write one mono segment to a streamed WAV writer."""
    pcm = np.clip(np.asarray(audio, dtype=np.float32).reshape(-1), -1.0, 1.0)
    pcm16 = (pcm * 32767.0).astype(np.int16, copy=False)
    writer.writeframes(pcm16.tobytes())


def _write_wav_file_segment(writer: Any, audio_path: Path, sample_rate: int) -> None:
    """Append one mono PCM WAV file to an already-open chapter writer."""
    with wave.open(str(audio_path), "rb") as reader:
        if reader.getnchannels() != 1:
            raise ValueError(f"Expected mono WAV chunk, got {reader.getnchannels()} channels")
        if reader.getframerate() != sample_rate:
            raise ValueError(
                f"Expected {sample_rate} Hz WAV chunk, got {reader.getframerate()} Hz"
            )
        if reader.getsampwidth() != 2:
            raise ValueError(
                f"Expected 16-bit PCM WAV chunk, got {reader.getsampwidth() * 8}-bit"
            )
        writer.writeframes(reader.readframes(reader.getnframes()))


def _wav_file_sample_count(audio_path: Path) -> int:
    try:
        with wave.open(str(audio_path), "rb") as reader:
            return int(reader.getnframes())
    except Exception:
        return 0


def _batch_result_has_audio(batch_result: Any) -> bool:
    if getattr(batch_result, "audio", None) is not None and len(batch_result.audio) > 0:
        return True
    audio_path = getattr(batch_result, "audio_path", None)
    return (
        isinstance(audio_path, Path)
        and audio_path.exists()
        and _wav_file_sample_count(audio_path) > 0
    )


def _batch_result_sample_count(batch_result: Any) -> int:
    if getattr(batch_result, "audio", None) is not None:
        return int(len(batch_result.audio))
    audio_path = getattr(batch_result, "audio_path", None)
    if isinstance(audio_path, Path):
        return _wav_file_sample_count(audio_path)
    return 0


def _start_tts_stage(
    runtime: PipelineRuntimeProtocol,
    hooks: SynthesisStageHooks,
) -> None:
    runtime.set_stage(PipelineStage.SYNTHESIZING)
    hooks.log_tts_stage_start(is_cached=runtime.is_tts_engine_loaded())


def _collect_valid_batch_results(
    runtime: PipelineRuntimeProtocol,
    batch_results: list[Any],
) -> ValidBatchSynthesis:
    valid_results: list[Any] = []
    total_samples = 0
    for i, batch_result in enumerate(batch_results):
        if batch_result.error:
            runtime.log_verbose(f"[TTS] Error on chunk {i + 1}: {batch_result.error}", "error")
            continue
        if not _batch_result_has_audio(batch_result):
            runtime.log_verbose(f"[TTS] Empty audio on chunk {i + 1}; skipping", "warning")
            continue
        valid_results.append(batch_result)
        total_samples += _batch_result_sample_count(batch_result)
    return ValidBatchSynthesis(results=valid_results, total_samples=total_samples)


def _validate_complete_batch_results(
    runtime: PipelineRuntimeProtocol,
    *,
    batch_results: list[Any],
    expected_count: int,
) -> bool:
    """Return True when final synthesis output has exactly one audio result per chunk."""
    valid = True
    seen_indexes: set[int] = set()

    if len(batch_results) != expected_count:
        runtime.log_verbose(
            "[TTS] Batch synthesis structural mismatch: "
            f"expected {expected_count} results, got {len(batch_results)}",
            "error",
        )
        valid = False

    for position, batch_result in enumerate(batch_results):
        result_index = getattr(batch_result, "index", None)
        if not isinstance(result_index, int):
            runtime.log_verbose(
                f"[TTS] Batch result {position + 1} is missing a valid chunk index",
                "error",
            )
            valid = False
            continue

        if result_index < 0 or result_index >= expected_count:
            runtime.log_verbose(
                f"[TTS] Batch result {position + 1} has out-of-range chunk index "
                f"{result_index}",
                "error",
            )
            valid = False
            continue

        if result_index in seen_indexes:
            runtime.log_verbose(
                f"[TTS] Duplicate batch result for chunk {result_index + 1}",
                "error",
            )
            valid = False
        seen_indexes.add(result_index)

        if batch_result.error:
            runtime.log_verbose(
                f"[TTS] Error on chunk {result_index + 1}: {batch_result.error}",
                "error",
            )
            valid = False

        if not _batch_result_has_audio(batch_result):
            runtime.log_verbose(
                f"[TTS] Empty audio on chunk {result_index + 1}; failing chapter",
                "error",
            )
            valid = False
            continue

    missing_indexes = sorted(set(range(expected_count)) - seen_indexes)
    if missing_indexes:
        missing_chunks = ", ".join(str(index + 1) for index in missing_indexes)
        runtime.log_verbose(
            f"[TTS] Missing batch result for chunk(s): {missing_chunks}",
            "error",
        )
        valid = False

    if not valid:
        runtime.log_verbose(
            "[TTS] Chapter synthesis failed because final batch output was incomplete",
            "error",
        )

    return valid


def _get_incremental_batch_synthesizer(tts_engine: Any):
    """Return a real iterator batch method, avoiding dynamic mock attributes."""
    if not bool(getattr(tts_engine, "supports_incremental_batch_results", False)):
        return None

    try:
        inspect.getattr_static(tts_engine, "iter_synthesize_batch")
    except AttributeError:
        return None

    iter_synthesize_batch = getattr(tts_engine, "iter_synthesize_batch", None)
    if not callable(iter_synthesize_batch):
        return None
    return iter_synthesize_batch


def _validate_incremental_batch_result(
    runtime: PipelineRuntimeProtocol,
    *,
    batch_result: Any,
    position: int,
    expected_count: int,
    seen_indexes: set[int],
) -> bool:
    """Validate one streamed batch result before its audio is consumed."""
    result_index = getattr(batch_result, "index", None)
    if not isinstance(result_index, int):
        runtime.log_verbose(
            f"[TTS] Batch result {position + 1} is missing a valid chunk index",
            "error",
        )
        return False

    valid = True
    if result_index < 0 or result_index >= expected_count:
        runtime.log_verbose(
            f"[TTS] Batch result {position + 1} has out-of-range chunk index "
            f"{result_index}",
            "error",
        )
        valid = False
    elif result_index in seen_indexes:
        runtime.log_verbose(
            f"[TTS] Duplicate batch result for chunk {result_index + 1}",
            "error",
        )
        valid = False
    else:
        seen_indexes.add(result_index)

    if result_index != position:
        runtime.log_verbose(
            f"[TTS] Incremental batch result {position + 1} arrived out of order",
            "error",
        )
        valid = False

    if batch_result.error:
        runtime.log_verbose(
            f"[TTS] Error on chunk {result_index + 1}: {batch_result.error}",
            "error",
        )
        valid = False

    if not _batch_result_has_audio(batch_result):
        runtime.log_verbose(
            f"[TTS] Empty audio on chunk {result_index + 1}; failing chapter",
            "error",
        )
        return False

    return valid


def _finalize_incremental_batch_validation(
    runtime: PipelineRuntimeProtocol,
    *,
    result_count: int,
    expected_count: int,
    seen_indexes: set[int],
    valid: bool,
) -> bool:
    """Validate streamed batch structure once the iterator is exhausted."""
    if result_count != expected_count:
        runtime.log_verbose(
            "[TTS] Batch synthesis structural mismatch: "
            f"expected {expected_count} results, got {result_count}",
            "error",
        )
        valid = False

    missing_indexes = sorted(set(range(expected_count)) - seen_indexes)
    if missing_indexes:
        missing_chunks = ", ".join(str(index + 1) for index in missing_indexes)
        runtime.log_verbose(
            f"[TTS] Missing batch result for chunk(s): {missing_chunks}",
            "error",
        )
        valid = False

    if not valid:
        runtime.log_verbose(
            "[TTS] Chapter synthesis failed because final batch output was incomplete",
            "error",
        )

    return valid


def _consume_batch_result_audio(accumulator: ChapterAudioAccumulator, batch_result: Any) -> None:
    audio_path = getattr(batch_result, "audio_path", None)
    if isinstance(audio_path, Path):
        accumulator.add_file(audio_path)
        try:
            audio_path.unlink()
        except Exception:
            pass
        batch_result.audio_path = None
        return
    if batch_result.audio is not None:
        accumulator.add_segment(batch_result.audio)
        batch_result.audio = None


def _should_use_file_batch_results(
    runtime: PipelineRuntimeProtocol,
    batch_items: list[Any],
) -> bool:
    if bool(getattr(runtime.config, "enable_tts_file_batch_results", False)):
        return True

    threshold = int(
        getattr(
            runtime.config,
            "tts_file_batch_results_auto_threshold_chunks",
            64,
        )
        or 0
    )
    return threshold > 0 and len(batch_items) >= threshold


def _with_batch_output_paths(
    runtime: PipelineRuntimeProtocol,
    *,
    chapter: Any,
    batch_items: list[Any],
) -> list[Any]:
    if not _should_use_file_batch_results(runtime, batch_items):
        return batch_items
    if not runtime.supports_file_batch_results():
        return batch_items

    chapter_temp_dir = runtime.config.temp_dir / f"ch_{chapter.number:03d}_tts"
    chapter_temp_dir.mkdir(parents=True, exist_ok=True)
    return [
        replace(item, output_path=chapter_temp_dir / f"chunk_{item.index:04d}.wav")
        for item in batch_items
    ]


def _create_batch_audio_accumulator(
    runtime: PipelineRuntimeProtocol,
    *,
    chapter_wav: Path,
    sample_rate: int,
    total_samples: int,
) -> ChapterAudioAccumulator:
    accumulator = ChapterAudioAccumulator(
        output_path=chapter_wav,
        sample_rate=sample_rate,
        max_in_memory_samples=_max_in_memory_chapter_samples(runtime),
    )
    if total_samples > accumulator.max_in_memory_samples:
        runtime.log_verbose("[TTS] Streaming chapter audio directly to WAV", "info")
        accumulator.spill_to_disk()
    return accumulator


def _finalize_chapter_audio(
    runtime: PipelineRuntimeProtocol,
    accumulator: ChapterAudioAccumulator,
) -> Optional[Path | np.ndarray]:
    try:
        return accumulator.finalize()
    except Exception as exc:
        runtime.log_verbose(
            f"[TTS] In-memory concatenation failed ({exc}); streaming to chapter WAV",
            "warning",
        )
        if not accumulator.is_streaming:
            runtime.log_verbose("[TTS] Streaming chapter audio directly to WAV", "info")
            accumulator.spill_to_disk()
        return accumulator.finalize()
    finally:
        accumulator.close()


def build_batch_items(runtime: PipelineRuntimeProtocol, *, chunks: list[str]):
    """Build TTS batch items from cleaned chunks."""
    from modules.tts.strategies.base import SynthesisItem
    from modules.tts.speech_normalizer import SpeechNormalizationConfig, normalize_for_speech

    speech_config = SpeechNormalizationConfig(
        enabled=bool(getattr(runtime.config, "speech_normalization", False))
    )

    return [
        SynthesisItem(
            text=normalize_for_speech(chunk, speech_config),
            voice=runtime.config.voice,
            speed=runtime.config.speed,
            index=i,
        )
        for i, chunk in enumerate(chunks)
    ]


def make_batch_progress_callback(
    runtime: PipelineRuntimeProtocol,
    *,
    chapter_idx: int,
    total_chapters: int,
    total_chunks: int,
    chunks: list[str],
):
    """Create per-batch progress callback for synthesis updates."""

    def on_batch_progress(completed: int, total: int):
        _ = total
        chunk_idx = completed - 1
        if chunk_idx < 0 or chunk_idx >= len(chunks):
            return

        runtime.notify_progress(
            chapter_idx=chapter_idx,
            total_chapters=total_chapters,
            chunk_idx=chunk_idx,
            total_chunks=total_chunks,
            message=f"Synthesizing chunk {chunk_idx + 1}/{total_chunks}",
        )

        text_preview = chunks[chunk_idx][:40].replace("\n", " ")
        runtime.log_verbose(
            f"[TTS] Speaking chunk {chunk_idx + 1}/{total_chunks}: {text_preview}...",
            "info",
        )

        pct = int((chunk_idx + 1) / total_chunks * 100)
        if pct % 25 == 0:
            runtime.log_verbose(
                f"[TTS] Progress: {pct}% ({chunk_idx + 1}/{total_chunks} chunks)",
                "info",
            )

        runtime.add_processed_chars(len(chunks[chunk_idx]))

    return on_batch_progress


def save_batch_chunk_audio(
    runtime: PipelineRuntimeProtocol,
    *,
    chapter_number: int,
    batch_results: list[Any],
) -> list[Path]:
    """Write successful chunk synthesis outputs to temp WAV files."""
    chapter_temp_dir = runtime.config.temp_dir / f"ch_{chapter_number:03d}"
    chapter_temp_dir.mkdir(parents=True, exist_ok=True)

    chunk_files: list[Path] = []
    sample_rate = _pipeline_sample_rate(runtime)
    for i, batch_result in enumerate(batch_results):
        if batch_result.error:
            runtime.log_verbose(f"[TTS] Error on chunk {i + 1}: {batch_result.error}", "error")
            continue

        chunk_wav = chapter_temp_dir / f"chunk_{i:04d}.wav"
        writer = _open_chapter_wav_writer(chunk_wav, sample_rate)
        try:
            _write_audio_segment(writer, batch_result.audio)
        finally:
            try:
                writer.close()
            except Exception:
                pass
        chunk_files.append(chunk_wav)

    return chunk_files


def cleanup_chunk_files(chunk_files: list[Path]) -> None:
    """Best-effort cleanup for temporary chunk WAV files."""
    parent_dirs: set[Path] = set()
    for chunk_file in chunk_files:
        parent_dirs.add(chunk_file.parent)
        try:
            chunk_file.unlink()
        except Exception:
            pass

    for parent_dir in parent_dirs:
        try:
            parent_dir.rmdir()
        except Exception:
            pass


def _synthesize_batch_results_to_chapter_audio(
    runtime: PipelineRuntimeProtocol,
    *,
    chapter: Any,
    batch_results: list[Any],
    total_chunks: int,
) -> Optional[Path | np.ndarray]:
    """Validate list-returned batch results and assemble chapter audio."""
    if not _validate_complete_batch_results(
        runtime,
        batch_results=batch_results,
        expected_count=total_chunks,
    ):
        return None
    batch_results = sorted(batch_results, key=lambda result: result.index)

    valid_batch = _collect_valid_batch_results(runtime, batch_results)
    if not valid_batch.results:
        return None

    runtime.log_verbose(
        f"[TTS] Generation complete - {len(valid_batch.results)} audio segments",
        "success",
    )

    chapter_wav = runtime.config.temp_dir / f"chapter_{chapter.number:02d}.wav"
    accumulator = _create_batch_audio_accumulator(
        runtime,
        chapter_wav=chapter_wav,
        sample_rate=_pipeline_sample_rate(runtime),
        total_samples=valid_batch.total_samples,
    )
    for batch_result in valid_batch.results:
        _consume_batch_result_audio(accumulator, batch_result)

    return _finalize_chapter_audio(runtime, accumulator)


def _synthesize_chapter_chunks_incrementally(
    runtime: PipelineRuntimeProtocol,
    hooks: SynthesisStageHooks,
    *,
    chapter: Any,
    batch_items: list[Any],
    on_batch_progress: Any,
    total_chunks: int,
    iter_synthesize_batch: Any,
) -> Optional[Path | np.ndarray]:
    """Consume iterator-returned TTS results without retaining the full result list."""
    chapter_wav = runtime.config.temp_dir / f"chapter_{chapter.number:02d}.wav"
    accumulator = ChapterAudioAccumulator(
        output_path=chapter_wav,
        sample_rate=_pipeline_sample_rate(runtime),
        max_in_memory_samples=_max_in_memory_chapter_samples(runtime),
    )
    seen_indexes: set[int] = set()
    valid = True
    result_count = 0
    valid_count = 0
    streamed_results: list[Any] = []

    try:
        for position, batch_result in enumerate(
            iter_synthesize_batch(
                items=batch_items,
                progress_callback=on_batch_progress,
            )
        ):
            streamed_results.append(batch_result)
            result_count = position + 1
            result_valid = _validate_incremental_batch_result(
                runtime,
                batch_result=batch_result,
                position=position,
                expected_count=total_chunks,
                seen_indexes=seen_indexes,
            )
            if result_valid:
                _consume_batch_result_audio(accumulator, batch_result)
                valid_count += 1
            else:
                batch_result.audio = None
                valid = False

        if (
            valid_count == 0
            and streamed_results
            and all(getattr(result, "error", None) for result in streamed_results)
        ):
            accumulator.close()
            retry_results = hooks.retry_batch_results_serially(
                batch_items=batch_items,
                batch_results=streamed_results,
                total_chunks=total_chunks,
                on_batch_progress=on_batch_progress,
            )
            return _synthesize_batch_results_to_chapter_audio(
                runtime,
                chapter=chapter,
                batch_results=retry_results,
                total_chunks=total_chunks,
            )

        valid = _finalize_incremental_batch_validation(
            runtime,
            result_count=result_count,
            expected_count=total_chunks,
            seen_indexes=seen_indexes,
            valid=valid,
        )
        if not valid or valid_count == 0:
            accumulator.close()
            return None

        runtime.log_verbose(
            f"[TTS] Generation complete - {valid_count} audio segments",
            "success",
        )
        return _finalize_chapter_audio(runtime, accumulator)
    except Exception:
        accumulator.close()
        raise


def synthesize_chapter_chunks(
    runtime: PipelineRuntimeProtocol,
    hooks: SynthesisStageHooks,
    chapter: Any,
    chapter_idx: int,
    total_chapters: int,
    chunks: list[str],
) -> Optional[Path | np.ndarray]:
    """Run TTS stage for chapter chunks and return chapter audio payload."""
    _start_tts_stage(runtime, hooks)
    total_chunks = len(chunks)

    batch_items = _with_batch_output_paths(
        runtime,
        chapter=chapter,
        batch_items=build_batch_items(runtime, chunks=chunks),
    )
    on_batch_progress = make_batch_progress_callback(
        runtime,
        chapter_idx=chapter_idx,
        total_chapters=total_chapters,
        total_chunks=total_chunks,
        chunks=chunks,
    )

    iter_synthesize_batch = _get_incremental_batch_synthesizer(runtime.tts_engine)
    if iter_synthesize_batch is not None:
        return _synthesize_chapter_chunks_incrementally(
            runtime,
            hooks,
            chapter=chapter,
            batch_items=batch_items,
            on_batch_progress=on_batch_progress,
            total_chunks=total_chunks,
            iter_synthesize_batch=iter_synthesize_batch,
        )

    batch_results = runtime.synthesize_batch(
        items=batch_items,
        progress_callback=on_batch_progress,
    )
    batch_results = hooks.retry_batch_results_serially(
        batch_items=batch_items,
        batch_results=batch_results,
        total_chunks=total_chunks,
        on_batch_progress=on_batch_progress,
    )

    return _synthesize_batch_results_to_chapter_audio(
        runtime,
        chapter=chapter,
        batch_results=batch_results,
        total_chunks=total_chunks,
    )


def clean_and_synthesize_with_overlap(
    runtime: PipelineRuntimeProtocol,
    hooks: SynthesisStageHooks,
    chapter: Any,
    chapter_idx: int,
    total_chapters: int,
    chunks: list[str],
    output_dir: Path,
) -> tuple[Optional[Path | np.ndarray], bool]:
    """
    Overlap cleaner production and chunk synthesis with a bounded queue.

    This path is used for multi-chunk chapters when pipelining is enabled.
    """
    from .overlap_synthesis import OverlapSynthesisRunner

    return OverlapSynthesisRunner(
        runtime,
        hooks,
        chapter=chapter,
        chapter_idx=chapter_idx,
        total_chapters=total_chapters,
        chunks=chunks,
        output_dir=output_dir,
    ).run()
