"""Pipeline encoding-stage helpers."""

from __future__ import annotations

from dataclasses import dataclass
import wave
from pathlib import Path
from typing import Any, Callable

import numpy as np

from modules.contracts.recovery_inventory import chapter_audio_path
from modules.pipeline.progress import chapter_progress_message

_WAV_RMS_CHUNK_FRAMES = 65536


@dataclass(frozen=True)
class EncodingStageContext:
    """Narrow pipeline surface required by the encoding stage."""

    config: Any
    sample_rate: int
    encoding_stage: Any
    set_stage: Callable[[Any], None]
    notify_progress: Callable[..., None]
    log_verbose: Callable[[str, str], None]
    cancel_requested: Callable[[], bool] | None = None


def _measure_source_duration_ms(chapter_wav: Path) -> int:
    """Measure source audio duration without probing the encoded MP3."""
    with wave.open(str(chapter_wav), "rb") as wav_file:
        frames = wav_file.getnframes()
        rate = wav_file.getframerate()
    if rate <= 0:
        return 0
    return int(frames / rate * 1000)


def _measure_pcm16_wav_rms(wav_path: Path, *, chunk_frames: int = _WAV_RMS_CHUNK_FRAMES) -> float:
    """Measure mono PCM16 WAV RMS using bounded readframes chunks."""
    total_square = 0.0
    total_samples = 0

    with wave.open(str(wav_path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()

        if channels != 1:
            raise ValueError(f"Expected mono WAV input, got {channels} channels")
        if sample_width != 2:
            raise ValueError(f"Expected 16-bit WAV input, got {sample_width * 8}-bit")

        while True:
            raw = wav_file.readframes(chunk_frames)
            if not raw:
                break
            samples = np.frombuffer(raw, dtype=np.int16)
            if samples.size == 0:
                continue
            total_square += float(np.sum(np.square(samples, dtype=np.float64)))
            total_samples += int(samples.size)

    if total_samples == 0:
        return 0.0
    return float(np.sqrt(total_square / total_samples) / 32768.0)


# Raising speech to the loudness target pushes its peaks past full scale, and
# AAC adds inter-sample overshoot on top. A lookahead limiter holds sample
# peaks at -2 dBFS so the decoded audio stays below about -1 dBTP; it acts only
# on peaks, leaving the loudness target and the voice unchanged.
PEAK_LIMIT_DBFS = -2.0
_PEAK_LIMITER = (
    f"alimiter=limit={10 ** (PEAK_LIMIT_DBFS / 20):.4f}:attack=5:release=50"
    ":level=disabled:latency=enabled"
)


def _normalization_filter_from_rms(rms: float, *, target_dBFS: float) -> str | None:
    if rms <= 0.0:
        return None

    current_dBFS = 20.0 * np.log10(rms)
    gain_db = float(target_dBFS - current_dBFS)
    if abs(gain_db) < 1e-6:
        return _PEAK_LIMITER
    return f"volume={gain_db:.6f}dB,{_PEAK_LIMITER}"


def _normalization_filter_for_wav(
    wav_path: Path,
    *,
    target_dBFS: float,
    precomputed_rms: float | None = None,
) -> str | None:
    if precomputed_rms is not None:
        return _normalization_filter_from_rms(precomputed_rms, target_dBFS=target_dBFS)
    return _normalization_filter_from_rms(
        _measure_pcm16_wav_rms(wav_path),
        target_dBFS=target_dBFS,
    )


def encode_chapter_audio(
    context: EncodingStageContext,
    chapter: Any,
    chapter_idx: int,
    total_chapters: int,
    output_dir: Path,
    chapter_wav: Path,
    *,
    precomputed_rms: float | None = None,
) -> tuple[Path, int]:
    """Encode chapter WAV to MP3 and return (mp3_path, duration_ms)."""
    from modules.audio.encoder import AudioEncoder

    cancel_requested = context.cancel_requested
    context.set_stage(context.encoding_stage)
    context.notify_progress(
        chapter_idx=chapter_idx,
        total_chapters=total_chapters,
        message=chapter_progress_message(chapter_idx, total_chapters, "encoding"),
    )
    context.log_verbose("[ENCODER] Encoding to MP3...", "process")
    source_duration_ms = _measure_source_duration_ms(chapter_wav)

    audio_filter: str | None = None
    if context.config.normalize_volume:
        context.log_verbose("[ENCODER] Normalizing audio volume...", "info")
        audio_filter = _normalization_filter_for_wav(
            chapter_wav,
            target_dBFS=context.config.target_dbfs,
            precomputed_rms=precomputed_rms,
        )

    # Recovery inventories record this same name before synthesis starts.
    chapter_mp3 = chapter_audio_path(chapter, output_dir, ".mp3")

    AudioEncoder(sample_rate=int(context.sample_rate or 24000)).wav_to_mp3(
        chapter_wav,
        chapter_mp3,
        bitrate=context.config.mp3_bitrate,
        audio_filter=audio_filter,
        cancel_requested=cancel_requested,
    )
    return chapter_mp3, source_duration_ms


def encode_chapter_aac(
    context: EncodingStageContext,
    chapter: Any,
    chapter_wav: Path,
    *,
    precomputed_rms: float | None = None,
    output_path: Path | None = None,
) -> Path:
    """Encode chapter WAV to AAC/M4A for stream-copy packaging.

    When ``output_path`` is provided, the AAC lands there (durable chapters dir
    for AAC-only conversions). Otherwise it is written under ``temp_dir``.
    """
    from modules.audio.encoder import AudioEncoder

    audio_filter: str | None = None
    if context.config.normalize_volume:
        audio_filter = _normalization_filter_for_wav(
            chapter_wav,
            target_dBFS=context.config.target_dbfs,
            precomputed_rms=precomputed_rms,
        )

    if output_path is not None:
        chapter_aac = Path(output_path)
        chapter_aac.parent.mkdir(parents=True, exist_ok=True)
    else:
        chapter_aac = context.config.temp_dir / f"chapter_{chapter.number:02d}.m4a"
    encoder = AudioEncoder(
        bitrate=context.config.mp3_bitrate,
        sample_rate=int(context.sample_rate or 24000),
    )
    encoder.wav_to_aac(
        chapter_wav,
        chapter_aac,
        bitrate=context.config.mp3_bitrate,
        audio_filter=audio_filter,
        cancel_requested=context.cancel_requested,
    )
    return chapter_aac
