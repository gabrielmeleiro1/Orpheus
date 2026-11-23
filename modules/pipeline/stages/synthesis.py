"""Pipeline synthesis-stage helpers."""

from __future__ import annotations

import inspect
import wave
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterator, Optional, Protocol

import numpy as np

from modules.pipeline.models import PipelineStage
from modules.pipeline.progress import chapter_progress_message
from modules.pipeline.runtime import PipelineRuntimeProtocol

_DEFAULT_MAX_IN_MEMORY_CHAPTER_SAMPLES = 30_000_000  # ~120MB float32 audio buffer
_WAV_COPY_FRAMES = 65_536


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


@dataclass(frozen=True)
class ChapterAudioFrame:
    """Audio written around a chapter body (announcement and pauses).

    A framed chapter is always published as a WAV at ``output_path``: the
    lead-in is written before the first body sample and the tail after the
    last, so the body is streamed to disk once.
    """

    output_path: Path
    lead_in: tuple[np.ndarray, ...]
    tail: tuple[np.ndarray, ...]


class ChapterAudioAccumulator:
    """Accumulate chapter audio in memory until it must spill to WAV."""

    def __init__(
        self,
        *,
        output_path: Path,
        sample_rate: int,
        max_in_memory_samples: int,
        frame: ChapterAudioFrame | None = None,
    ) -> None:
        self.output_path = output_path if frame is None else frame.output_path
        self.sample_rate = sample_rate
        self.max_in_memory_samples = max_in_memory_samples
        self._frame = frame
        self._segments: list[np.ndarray] = []
        self._sample_count = 0
        self._writer: Any = None
        self._pcm16_square_sum = 0.0
        self._pcm16_sample_count = 0
        # Streamed body samples of a framed chapter are summed in fixed-size
        # blocks counted from the body start, so the RMS does not depend on
        # how the body arrived in chunks (matching a block-wise WAV scan).
        self._pending_body_pcm16 = np.empty(0, dtype=np.int16)

    @property
    def is_streaming(self) -> bool:
        return self._writer is not None

    def pcm16_rms(self) -> float | None:
        """Return the PCM16 RMS of streamed audio, matching a post-hoc WAV scan."""
        if self._pcm16_sample_count == 0:
            return None
        return float(
            np.sqrt(self._pcm16_square_sum / self._pcm16_sample_count) / 32768.0
        )

    def _track_pcm16(self, pcm16: np.ndarray) -> None:
        if pcm16.size == 0:
            return
        self._pcm16_square_sum += float(np.sum(np.square(pcm16, dtype=np.float64)))
        self._pcm16_sample_count += int(pcm16.size)

    def _track_body_pcm16(self, pcm16: np.ndarray) -> None:
        if self._frame is None:
            self._track_pcm16(pcm16)
            return
        if pcm16.size == 0:
            return
        if self._pending_body_pcm16.size:
            pcm16 = np.concatenate([self._pending_body_pcm16, pcm16])
        whole = pcm16.size - pcm16.size % _WAV_COPY_FRAMES
        for start in range(0, whole, _WAV_COPY_FRAMES):
            self._track_pcm16(pcm16[start:start + _WAV_COPY_FRAMES])
        self._pending_body_pcm16 = pcm16[whole:].copy()

    def _write_frame_part(self, segments: tuple[np.ndarray, ...]) -> None:
        for segment in segments:
            self._track_pcm16(_write_audio_segment(self._writer, segment))

    def add_segment(self, audio: np.ndarray) -> None:
        audio_array = np.asarray(audio, dtype=np.float32)
        if not np.isfinite(audio_array).all():
            raise ValueError("Audio segment contains non-finite samples")
        if self.is_streaming:
            self._track_body_pcm16(_write_audio_segment(self._writer, audio_array))
            return

        projected_sample_count = self._sample_count + int(len(audio_array))
        if projected_sample_count <= self.max_in_memory_samples:
            self._segments.append(audio_array)
            self._sample_count = projected_sample_count
            return

        self.spill_to_disk()
        self._track_body_pcm16(_write_audio_segment(self._writer, audio_array))

    def add_file(self, audio_path: Path) -> None:
        if not self.is_streaming:
            self.spill_to_disk()
        for pcm16 in _write_wav_file_segment(self._writer, audio_path, self.sample_rate):
            self._track_body_pcm16(pcm16)

    def spill_to_disk(self) -> None:
        if self.is_streaming:
            return

        self._writer = _open_chapter_wav_writer(self.output_path, self.sample_rate)
        if self._frame is not None:
            self._write_frame_part(self._frame.lead_in)
        for buffered in self._segments:
            self._track_body_pcm16(_write_audio_segment(self._writer, buffered))
        self._segments.clear()
        self._sample_count = 0

    def finalize(self) -> Optional[Path | np.ndarray]:
        if self.is_streaming:
            if self._frame is not None:
                self._track_pcm16(self._pending_body_pcm16)
                self._pending_body_pcm16 = np.empty(0, dtype=np.int16)
                self._write_frame_part(self._frame.tail)
            self._close_writer()
            return self.output_path
        if not self._segments:
            return None
        if len(self._segments) == 1:
            body = self._segments[0]
        else:
            body = np.concatenate(self._segments)
        if self._frame is None:
            return body
        # A body that fit in memory is written as one segment, after the
        # concatenation that may raise MemoryError and before any byte lands.
        self._writer = _open_chapter_wav_writer(self.output_path, self.sample_rate)
        self._write_frame_part(self._frame.lead_in)
        self._track_pcm16(_write_audio_segment(self._writer, body))
        self._segments.clear()
        self._sample_count = 0
        self._write_frame_part(self._frame.tail)
        self._close_writer()
        return self.output_path

    def _close_writer(self) -> None:
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.close()

    def close(self) -> None:
        """Best-effort cleanup; successful finalization requires a strict close."""
        try:
            self._close_writer()
        except Exception:
            pass


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


def _write_audio_segment(writer: Any, audio: np.ndarray) -> np.ndarray:
    """Write one mono segment to a streamed WAV writer and return its PCM16 samples."""
    audio_array = np.asarray(audio, dtype=np.float32).reshape(-1)
    if not np.isfinite(audio_array).all():
        raise ValueError("Audio segment contains non-finite samples")
    pcm = np.clip(audio_array, -1.0, 1.0)
    pcm16 = (pcm * 32767.0).astype(np.int16, copy=False)
    writer.writeframes(pcm16.tobytes())
    return pcm16


def _write_wav_file_segment(
    writer: Any, audio_path: Path, sample_rate: int
) -> Iterator[np.ndarray]:
    """Append one mono PCM WAV file to an already-open chapter writer in bounded blocks."""
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
        while True:
            raw = reader.readframes(_WAV_COPY_FRAMES)
            if not raw:
                break
            writer.writeframes(raw)
            yield np.frombuffer(raw, dtype=np.int16)


def _wav_file_sample_count(audio_path: Path) -> int:
    try:
        with wave.open(str(audio_path), "rb") as reader:
            return int(reader.getnframes())
    except Exception:
        return 0


def _batch_result_has_audio(batch_result: Any) -> bool:
    if getattr(batch_result, "audio", None) is not None and len(batch_result.audio) > 0:
        return bool(np.isfinite(batch_result.audio).all())
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
            runtime.log_verbose(f"[TTS] Empty or non-finite audio on chunk {i + 1}; skipping", "warning")
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
                f"[TTS] Empty or non-finite audio on chunk {result_index + 1}; failing chapter",
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

    if batch_result.error:
        runtime.log_verbose(
            f"[TTS] Error on chunk {result_index + 1}: {batch_result.error}",
            "error",
        )
        valid = False

    if not _batch_result_has_audio(batch_result):
        runtime.log_verbose(
            f"[TTS] Empty or non-finite audio on chunk {result_index + 1}; failing chapter",
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
    if batch_result.audio is not None and len(batch_result.audio) > 0:
        # Prefer in-memory audio; a chunk file, when also present, is only a
        # byproduct and re-reading it would duplicate work.
        accumulator.add_segment(batch_result.audio)
        batch_result.audio = None
        if isinstance(audio_path, Path):
            try:
                audio_path.unlink()
            except Exception:
                pass
            batch_result.audio_path = None
        return
    if isinstance(audio_path, Path):
        accumulator.add_file(audio_path)
        try:
            audio_path.unlink()
        except Exception:
            pass
        batch_result.audio_path = None
        batch_result.audio = None


def _discard_pending_results(pending_results: dict[int, Any]) -> None:
    """Drop buffered chunk audio that will never be consumed."""
    for batch_result in pending_results.values():
        batch_result.audio = None
        audio_path = getattr(batch_result, "audio_path", None)
        if isinstance(audio_path, Path):
            audio_path.unlink(missing_ok=True)
            batch_result.audio_path = None
    pending_results.clear()


def _flush_ordered_incremental_results(
    accumulator: ChapterAudioAccumulator,
    pending_results: dict[int, Any],
    *,
    next_index: int,
) -> tuple[int, int]:
    """Consume buffered incremental results in chunk order when contiguous."""
    consumed_count = 0
    while next_index in pending_results:
        batch_result = pending_results.pop(next_index)
        _consume_batch_result_audio(accumulator, batch_result)
        next_index += 1
        consumed_count += 1
    return next_index, consumed_count


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
    # Register the directory so end-of-run cleanup removes it. Without this the
    # per-chunk WAVs survive whenever batch validation fails, and the tracked
    # set is non-empty enough to suppress the temp-root sweep that would
    # otherwise have caught them.
    runtime.track_temp_cleanup_path(chapter_temp_dir)
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
    frame: ChapterAudioFrame | None = None,
) -> ChapterAudioAccumulator:
    accumulator = ChapterAudioAccumulator(
        output_path=chapter_wav,
        sample_rate=sample_rate,
        max_in_memory_samples=_max_in_memory_chapter_samples(runtime),
        frame=frame,
    )
    runtime.track_temp_cleanup_path(accumulator.output_path)
    if total_samples > accumulator.max_in_memory_samples:
        runtime.log_verbose("[TTS] Streaming chapter audio directly to WAV", "info")
        accumulator.spill_to_disk()
    return accumulator


def _finalize_chapter_audio(
    runtime: PipelineRuntimeProtocol,
    accumulator: ChapterAudioAccumulator,
    chapter_result: Any | None = None,
) -> Optional[Path | np.ndarray]:
    was_streaming = accumulator.is_streaming
    try:
        return accumulator.finalize()
    except MemoryError as exc:
        # A framed chapter opens its WAV during finalize; once bytes are
        # written, a retry would duplicate them.
        if was_streaming or accumulator.is_streaming:
            raise
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
        if chapter_result is not None:
            chapter_result.pcm_rms = accumulator.pcm16_rms()


def build_batch_items(runtime: PipelineRuntimeProtocol, *, chunks: list[str], source: str = ""):
    """Build TTS batch items from cleaned chunks."""
    from modules.tts.strategies.base import SynthesisItem
    from modules.tts.speech_typography import normalize_speech_typography

    return [
        SynthesisItem(
            text=normalize_speech_typography(chunk, source),
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
            message=chapter_progress_message(
            chapter_idx, total_chapters, f"synthesizing chunk {chunk_idx + 1}/{total_chunks}",
        ),
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

    return on_batch_progress


def _synthesize_batch_results_to_chapter_audio(
    runtime: PipelineRuntimeProtocol,
    *,
    chapter: Any,
    batch_results: list[Any],
    total_chunks: int,
    chapter_result: Any | None = None,
    frame: ChapterAudioFrame | None = None,
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
    sample_rate = _pipeline_sample_rate(runtime)
    accumulator: ChapterAudioAccumulator | None = None
    try:
        accumulator = _create_batch_audio_accumulator(
            runtime,
            chapter_wav=chapter_wav,
            sample_rate=sample_rate,
            total_samples=valid_batch.total_samples,
            frame=frame,
        )
        for batch_result in valid_batch.results:
            _consume_batch_result_audio(accumulator, batch_result)

        return _finalize_chapter_audio(runtime, accumulator, chapter_result)
    except Exception:
        if accumulator is not None:
            accumulator.close()
        raise


def _synthesize_chapter_chunks_incrementally(
    runtime: PipelineRuntimeProtocol,
    hooks: SynthesisStageHooks,
    *,
    chapter: Any,
    batch_items: list[Any],
    on_batch_progress: Any,
    total_chunks: int,
    iter_synthesize_batch: Any,
    chapter_result: Any | None = None,
    frame: ChapterAudioFrame | None = None,
) -> Optional[Path | np.ndarray]:
    """Consume iterator-returned TTS results without retaining the full result list."""
    chapter_wav = (
        runtime.config.temp_dir / f"chapter_{chapter.number:02d}.wav"
        if frame is None
        else frame.output_path
    )
    # A cancelled or failed chapter never reaches narration/encoding, where
    # completed artifacts are otherwise registered for end-of-run cleanup.
    runtime.track_temp_cleanup_path(chapter_wav)
    sample_rate = _pipeline_sample_rate(runtime)
    accumulator: ChapterAudioAccumulator | None = None
    batch_iterator: Any = None
    try:
        accumulator = ChapterAudioAccumulator(
            output_path=chapter_wav,
            sample_rate=sample_rate,
            max_in_memory_samples=_max_in_memory_chapter_samples(runtime),
            frame=frame,
        )
        seen_indexes: set[int] = set()
        valid = True
        result_count = 0
        valid_count = 0
        streamed_results: list[Any] = []
        pending_results: dict[int, Any] = {}
        next_index_to_consume = 0

        batch_iterator = iter(iter_synthesize_batch(
            items=batch_items,
            progress_callback=on_batch_progress,
        ))
        while True:
            if runtime.check_cancelled():
                accumulator.close()
                return None
            try:
                batch_result = next(batch_iterator)
            except StopIteration:
                break
            if runtime.check_cancelled():
                batch_result.audio = None
                accumulator.close()
                return None
            position = result_count
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
                pending_results[batch_result.index] = batch_result
                next_index_to_consume, consumed_count = _flush_ordered_incremental_results(
                    accumulator,
                    pending_results,
                    next_index=next_index_to_consume,
                )
                valid_count += consumed_count
            else:
                batch_result.audio = None
                valid = False
            if not valid and any(
                not getattr(result, "error", None) for result in streamed_results
            ):
                # The serial retry below only runs when every chunk errored, so
                # this chapter can no longer succeed; stop instead of buffering
                # every later chunk behind the failed one.
                _discard_pending_results(pending_results)
                runtime.log_verbose(
                    "[TTS] Chapter synthesis failed; skipping its remaining chunks",
                    "error",
                )
                accumulator.close()
                return None

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
                chapter_result=chapter_result,
                frame=frame,
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
        return _finalize_chapter_audio(runtime, accumulator, chapter_result)
    except Exception:
        if accumulator is not None:
            accumulator.close()
        raise
    finally:
        close = getattr(batch_iterator, "close", None)
        if callable(close):
            close()


def synthesize_chapter_chunks(
    runtime: PipelineRuntimeProtocol,
    hooks: SynthesisStageHooks,
    chapter: Any,
    chapter_idx: int,
    total_chapters: int,
    chunks: list[str],
    chapter_result: Any | None = None,
    frame: ChapterAudioFrame | None = None,
) -> Optional[Path | np.ndarray]:
    """Run TTS stage for chapter chunks and return chapter audio payload.

    With a ``frame``, successful audio is always the framed WAV at
    ``frame.output_path``.
    """
    _start_tts_stage(runtime, hooks)
    total_chunks = len(chunks)

    batch_items = _with_batch_output_paths(
        runtime,
        chapter=chapter,
        batch_items=build_batch_items(runtime, chunks=chunks, source=str(getattr(chapter, "content", "") or "")),
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
            chapter_result=chapter_result,
            frame=frame,
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
        chapter_result=chapter_result,
        frame=frame,
    )
