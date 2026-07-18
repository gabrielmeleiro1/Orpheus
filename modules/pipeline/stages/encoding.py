"""Pipeline encoding-stage helpers."""

from __future__ import annotations

from dataclasses import dataclass
import inspect
import tempfile
import wave
from pathlib import Path
from typing import Any, Callable

import numpy as np

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


def _coerce_encoding_context(context: EncodingStageContext | Any) -> EncodingStageContext:
    if isinstance(context, EncodingStageContext):
        return context

    stage_enum = type(context._stage)
    sample_rate = int(getattr(getattr(context, "_tts_engine", None), "sample_rate", 24000) or 24000)
    return EncodingStageContext(
        config=context.config,
        sample_rate=sample_rate,
        encoding_stage=stage_enum.ENCODING,
        set_stage=lambda stage: setattr(context, "_stage", stage),
        notify_progress=context._notify_progress,
        log_verbose=context._log_verbose,
        cancel_requested=getattr(context, "check_cancelled", None),
    )


def _context_cancel_requested(context: EncodingStageContext) -> Callable[[], bool] | None:
    if context.cancel_requested is not None:
        return context.cancel_requested
    owner = getattr(context.set_stage, "__self__", None)
    predicate = getattr(owner, "check_cancelled", None)
    return predicate if callable(predicate) else None


def _call_encoder(method: Any, *args: Any, cancel_requested: Callable[[], bool] | None, **kwargs: Any) -> Any:
    """Call an encoder while keeping compatibility with legacy test/double methods."""
    if cancel_requested is None:
        return method(*args, **kwargs)
    candidate = getattr(method, "side_effect", None) or method
    try:
        signature = inspect.signature(candidate)
        accepts_cancel = "cancel_requested" in signature.parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
    except (TypeError, ValueError):
        accepts_cancel = True
    if not accepts_cancel:
        return method(*args, **kwargs)
    try:
        return method(*args, cancel_requested=cancel_requested, **kwargs)
    except TypeError as exc:
        if "cancel_requested" not in str(exc):
            raise
        return method(*args, **kwargs)


def _measure_source_duration_ms(chapter_wav: Path | np.ndarray, *, sample_rate: int) -> int:
    """Measure source audio duration without probing the encoded MP3."""
    if isinstance(chapter_wav, Path):
        with wave.open(str(chapter_wav), "rb") as wav_file:
            frames = wav_file.getnframes()
            rate = wav_file.getframerate()
        if rate <= 0:
            return 0
        return int(frames / rate * 1000)

    audio = np.asarray(chapter_wav, dtype=np.float32).reshape(-1)
    if audio.size == 0 or sample_rate <= 0:
        return 0
    return int(len(audio) / sample_rate * 1000)


def _normalize_audio_array(audio: np.ndarray, *, target_dBFS: float) -> np.ndarray:
    """Normalize mono float audio in numpy space."""
    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    if samples.size == 0:
        return samples

    rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float64)))))
    if rms <= 0.0:
        return samples

    current_dBFS = 20.0 * np.log10(rms)
    gain_db = target_dBFS - current_dBFS
    if abs(gain_db) < 1e-6:
        return samples

    gain = float(np.power(10.0, gain_db / 20.0))
    return np.clip(samples * gain, -1.0, 1.0).astype(np.float32, copy=False)


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
            float_samples = samples.astype(np.float64)
            total_square += float(np.sum(np.square(float_samples)))
            total_samples += int(samples.size)

    if total_samples == 0:
        return 0.0
    return float(np.sqrt(total_square / total_samples) / 32768.0)


def _normalization_filter_from_rms(rms: float, *, target_dBFS: float) -> str | None:
    if rms <= 0.0:
        return None

    current_dBFS = 20.0 * np.log10(rms)
    gain_db = float(target_dBFS - current_dBFS)
    if abs(gain_db) < 1e-6:
        return None
    return f"volume={gain_db:.6f}dB"


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


def write_wav_from_array(audio: np.ndarray, output_path: Path, *, sample_rate: int) -> Path:
    """Persist mono float audio as a PCM_16 WAV file."""
    pcm = np.clip(np.asarray(audio, dtype=np.float32).reshape(-1), -1.0, 1.0)
    pcm16 = (pcm * 32767.0).astype(np.int16, copy=False)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm16.tobytes())
    return output_path


def _write_temp_wav_from_array(audio: np.ndarray, *, sample_rate: int) -> Path:
    """Persist a temporary WAV for FFmpeg fallbacks."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        temp_wav = Path(tmp.name)

    try:
        write_wav_from_array(audio, temp_wav, sample_rate=sample_rate)
    except Exception:
        try:
            temp_wav.unlink()
        except Exception:
            pass
        raise

    return temp_wav


def _wav_to_mp3(
    encoder: Any,
    input_path: Path,
    output_path: Path,
    *,
    bitrate: str,
    audio_filter: str | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> Path:
    kwargs = {"bitrate": bitrate}
    if audio_filter is not None:
        kwargs["audio_filter"] = audio_filter
    return _call_encoder(
        encoder.wav_to_mp3,
        input_path,
        output_path,
        cancel_requested=cancel_requested,
        **kwargs,
    )


def encode_chapter_audio(
    context: EncodingStageContext | Any,
    chapter: Any,
    chapter_idx: int,
    total_chapters: int,
    output_dir: Path,
    chapter_wav: Path | np.ndarray,
    *,
    precomputed_rms: float | None = None,
) -> tuple[Path, int]:
    """Encode chapter WAV to MP3 and return (mp3_path, duration_ms)."""
    from modules.audio.encoder import AudioEncoder

    context = _coerce_encoding_context(context)
    cancel_requested = _context_cancel_requested(context)
    context.set_stage(context.encoding_stage)
    context.notify_progress(
        chapter_idx=chapter_idx,
        total_chapters=total_chapters,
        message=f"Encoding: {chapter.title}",
    )
    context.log_verbose("[ENCODER] Encoding to MP3...", "process")
    sample_rate = int(context.sample_rate or 24000)

    source_duration_ms = _measure_source_duration_ms(
        chapter_wav,
        sample_rate=sample_rate,
    )

    audio_array: np.ndarray | None = None
    audio_filter: str | None = None
    if context.config.normalize_volume:
        context.log_verbose("[ENCODER] Normalizing audio volume...", "info")

    if isinstance(chapter_wav, np.ndarray):
        audio_array = np.asarray(chapter_wav, dtype=np.float32).reshape(-1)
    elif context.config.normalize_volume:
        audio_filter = _normalization_filter_for_wav(
            chapter_wav,
            target_dBFS=context.config.target_dbfs,
            precomputed_rms=precomputed_rms,
        )

    # Normalize volume without writing a normalized WAV back to disk.
    if context.config.normalize_volume and audio_array is not None:
        audio_array = _normalize_audio_array(
            audio_array,
            target_dBFS=context.config.target_dbfs,
        )

    # Encode to MP3.
    safe_title = "".join(
        c if c.isalnum() or c in " -_" else "_"
        for c in chapter.title
    )[:50]
    chapter_mp3 = output_dir / f"{chapter.number:02d}_{safe_title}.mp3"

    encoder = AudioEncoder()
    array_to_mp3 = getattr(encoder, "array_to_mp3", None)
    if audio_array is not None and callable(array_to_mp3):
        try:
            _call_encoder(
                array_to_mp3,
                audio_array,
                chapter_mp3,
                cancel_requested=cancel_requested,
                sample_rate=sample_rate,
                bitrate=context.config.mp3_bitrate,
            )
        except Exception as exc:
            context.log_verbose(
                f"[ENCODER] array_to_mp3 failed ({exc}); falling back to wav_to_mp3",
                "warning",
            )
            temp_wav = _write_temp_wav_from_array(audio_array, sample_rate=sample_rate)
            try:
                _wav_to_mp3(
                    encoder,
                    temp_wav,
                    chapter_mp3,
                    bitrate=context.config.mp3_bitrate,
                    cancel_requested=cancel_requested,
                )
            finally:
                try:
                    temp_wav.unlink()
                except Exception:
                    pass
    else:
        _wav_to_mp3(
            encoder,
            chapter_wav,
            chapter_mp3,
            bitrate=context.config.mp3_bitrate,
            audio_filter=audio_filter,
            cancel_requested=cancel_requested,
        )
    return chapter_mp3, source_duration_ms


def encode_chapter_aac(
    context: EncodingStageContext | Any,
    chapter: Any,
    chapter_wav: Path | np.ndarray,
    *,
    precomputed_rms: float | None = None,
    output_path: Path | None = None,
) -> Path:
    """Encode chapter WAV/audio payload to AAC/M4A for stream-copy packaging.

    When ``output_path`` is provided, the AAC lands there (durable chapters dir
    for AAC-only conversions). Otherwise it is written under ``temp_dir``.
    """
    from modules.audio.encoder import AudioEncoder

    context = _coerce_encoding_context(context)
    cancel_requested = _context_cancel_requested(context)
    sample_rate = int(context.sample_rate or 24000)

    audio_array: np.ndarray | None = None
    audio_filter: str | None = None
    if isinstance(chapter_wav, np.ndarray):
        audio_array = np.asarray(chapter_wav, dtype=np.float32).reshape(-1)
    elif context.config.normalize_volume:
        audio_filter = _normalization_filter_for_wav(
            chapter_wav,
            target_dBFS=context.config.target_dbfs,
            precomputed_rms=precomputed_rms,
        )

    if context.config.normalize_volume and audio_array is not None:
        audio_array = _normalize_audio_array(
            audio_array,
            target_dBFS=context.config.target_dbfs,
        )

    if output_path is not None:
        chapter_aac = Path(output_path)
        chapter_aac.parent.mkdir(parents=True, exist_ok=True)
    else:
        chapter_aac = context.config.temp_dir / f"chapter_{chapter.number:02d}.m4a"
    encoder = AudioEncoder(
        bitrate=context.config.mp3_bitrate,
        sample_rate=sample_rate,
    )
    array_to_aac = getattr(encoder, "array_to_aac", None)
    if audio_array is not None and callable(array_to_aac):
        _call_encoder(
            array_to_aac,
            audio_array,
            chapter_aac,
            cancel_requested=cancel_requested,
            sample_rate=sample_rate,
            bitrate=context.config.mp3_bitrate,
        )
    else:
        _call_encoder(
            encoder.wav_to_aac,
            chapter_wav,
            chapter_aac,
            cancel_requested=cancel_requested,
            bitrate=context.config.mp3_bitrate,
            audio_filter=audio_filter,
        )
    return chapter_aac
