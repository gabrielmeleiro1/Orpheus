"""
TTS Engine Module
=================
Kokoro-82M TTS engine via mlx-audio framework.
"""

import time
import threading
import logging
import wave
from pathlib import Path
from typing import Callable, Iterator, Optional, TypeVar
import numpy as np

from modules.contracts.limits import MAX_SPEED, MIN_SPEED
from modules.errors import TTSInstallationError, TTSModelError, SynthesisError, VRAMOverflowError
from modules.tts.strategies.base import (
    SynthesisItem,
    SynthesisResult,
    has_no_speakable_text,
)
from .engine_config import TTSConfig
from .cleaner_errors import is_memory_pressure_error
from . import engine_output, engine_runtime
from . import mlx_compat as mlx_compat_backend
from . import model_cache as model_cache_backend
from .quantization import normalize_quantization

logger = logging.getLogger(__name__)

T = TypeVar("T")

# MLX runtime - loaded lazily to avoid long startup imports in TUI mode.
generate_audio = mlx_compat_backend.generate_audio

# Duration of the silence emitted for a chunk with nothing to speak.
BLANK_TEXT_SILENCE_MS = 50


def _inject_generate_audio_model_arg(
    kwargs: dict,
    model_path: str,
    loaded_model: object | None = None,
    generate_fn: Optional[Callable] = None,
) -> None:
    mlx_compat_backend.inject_generate_audio_model_arg(
        kwargs,
        model_path,
        loaded_model=loaded_model,
        generate_fn=generate_fn or generate_audio,
    )


def _inject_generate_audio_output_args(
    kwargs: dict,
    output_dir: str,
    file_prefix: str,
    generate_fn: Optional[Callable] = None,
) -> None:
    mlx_compat_backend.inject_generate_audio_output_args(
        kwargs,
        output_dir=output_dir,
        file_prefix=file_prefix,
        generate_fn=generate_fn or generate_audio,
    )


def _ensure_mlx_runtime() -> None:
    """Import MLX/MLX-Audio on demand."""
    global generate_audio
    _mx, generate_audio = mlx_compat_backend.ensure_mlx_runtime()


def _ensure_tqdm_thread_lock() -> None:
    """
    Force tqdm to use a thread lock to avoid multiprocessing lock initialization.

    This prevents crashes like:
      ValueError: bad value(s) in fds_to_keep
    originating from tqdm's multiprocessing lock setup path.
    """
    mlx_compat_backend.ensure_tqdm_thread_lock()


BatchItem = SynthesisItem
BatchResult = SynthesisResult


def _wav_duration_ms(path: Path, fallback_sample_rate: int) -> int:
    """Return WAV duration in milliseconds without loading audio samples."""
    try:
        with wave.open(str(path), "rb") as wav_file:
            sample_rate = wav_file.getframerate() or fallback_sample_rate
            return int(wav_file.getnframes() / max(1, sample_rate) * 1000)
    except Exception:
        return 0


class TTSEngine:
    """
    Kokoro-82M TTS engine wrapper.

    Uses mlx-audio for GPU-accelerated inference on Apple Silicon.
    """

    supports_incremental_batch_results = True
    supports_file_batch_results = True

    def __init__(self, config: Optional[TTSConfig] = None):
        """
        Initialize the TTS engine.

        Args:
            config: TTS configuration (uses defaults if None)
        """
        _ensure_mlx_runtime()
        _ensure_tqdm_thread_lock()

        self.config = config or TTSConfig()
        self._model_loaded = False
        self._model = None
        self._last_cached_model = None
        self._voice_snapshot: Optional[tuple[str, str]] = None

        # Determine model path based on quantization
        self._canonical_quantization = normalize_quantization(self.config.quantization)
        self._model_path = self._get_model_path()
        self._canonical_model_path = self._model_path
        self._model_cache_key = engine_runtime.get_model_cache_key(self.config)
        self._canonical_cache_key = self._model_cache_key

        # Track synthesis statistics
        self._total_chunks_processed = 0
        self._total_time_ms = 0
        self._total_chars_processed = 0
        self._engine_start_time = time.time()
        self._first_audio_time: Optional[float] = None
        self._retry_count = 0
        self._warmup_complete = False
        self._last_warmup_ms: Optional[float] = None

    def _get_model_path(self) -> str:
        """Get the model path based on quantization setting."""
        return engine_runtime.get_model_path(self.config)

    @property
    def is_loaded(self) -> bool:
        """Check if model is currently loaded."""
        return self._model_loaded

    @property
    def sample_rate(self) -> int:
        """Get the audio sample rate."""
        return self.config.sample_rate

    @property
    def _model_lock(self) -> threading.RLock:
        """Serialize access to the underlying model.

        engine_runtime hands one cached model to every engine sharing a cache
        key, and the model keeps mutable per-call state, so the lock has to be
        keyed by that model rather than owned by the instance. Falls back to the
        model path before the cache key is assigned.
        """
        key = getattr(self, "_model_cache_key", None) or self._model_path
        return model_cache_backend.get_model_lock(str(key))

    def _blank_text_silence(self) -> "np.ndarray":
        """Audio for a chunk with nothing to speak.

        Chunks such as a horizontal rule or a bare citation marker survive
        chunking but clean to "" or to bare symbols that produce no phonemes.
        The pipeline requires one audio result per chunk and treats an empty
        one as a chapter failure, so emit a short silence -- which is also what
        a scene break should sound like.
        """
        frames = max(1, int(self.config.sample_rate * BLANK_TEXT_SILENCE_MS / 1000))
        return np.zeros(frames, dtype=np.float32)

    def _write_blank_text_silence(self, output_path: Path | str) -> Path:
        """Write the blank-chunk silence when a caller requested an output path."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        silence = self._blank_text_silence()
        with wave.open(str(output_path), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(self.config.sample_rate)
            writer.writeframes((silence * 32767).astype(np.int16).tobytes())
        return output_path

    @property
    def average_chunk_time_ms(self) -> float:
        """Get average time per chunk in milliseconds."""
        if self._total_chunks_processed == 0:
            return 0.0
        return self._total_time_ms / self._total_chunks_processed

    def load_model(self, quantization: Optional[str] = None) -> None:
        """
        Load the TTS model into memory.

        Args:
            quantization: Override quantization setting ('4bit', '8bit', 'full')
        """
        engine_runtime.load_model(self, quantization=quantization)

    def warmup_model(
        self,
        voice: Optional[str] = None,
        speed: float = 1.0,
        probe_text: str = "warmup check",
    ) -> None:
        """Prime the first synthesis path without changing public load semantics."""
        engine_runtime.warmup_model(
            self,
            voice=voice,
            speed=speed,
            probe_text=probe_text,
        )

    def _update_synthesis_stats(self, start_time: float, char_count: int) -> None:
        """Update aggregate synthesis timing counters."""
        self._total_chunks_processed += 1
        self._total_time_ms += (time.time() - start_time) * 1000
        self._total_chars_processed += max(0, char_count)
        if self._first_audio_time is None:
            self._first_audio_time = time.time()

    def _synthesize_to_output_path(
        self,
        text: str,
        voice: str,
        speed: float,
        output_path: Path,
    ) -> np.ndarray:
        """Synthesize directly to a destination path and return audio samples."""
        return engine_output.synthesize_to_output_path(
            self,
            text=text,
            voice=voice,
            speed=speed,
            output_path=output_path,
            generate_fn=generate_audio,
            inject_model_arg=_inject_generate_audio_model_arg,
            inject_output_args=_inject_generate_audio_output_args,
        )

    def _synthesize_to_output_file(
        self,
        text: str,
        voice: str,
        speed: float,
        output_path: Path,
    ) -> Path:
        """Synthesize directly to a destination WAV path without reading samples."""
        return engine_output.synthesize_to_output_file(
            self,
            text=text,
            voice=voice,
            speed=speed,
            output_path=output_path,
            generate_fn=generate_audio,
            inject_model_arg=_inject_generate_audio_model_arg,
            inject_output_args=_inject_generate_audio_output_args,
        )

    def _synthesize_to_temp_audio(
        self, text: str, voice: str, speed: float
    ) -> np.ndarray:
        """Synthesize via temp WAV files and return concatenated audio."""
        return engine_output.synthesize_to_temp_audio(
            self,
            text=text,
            voice=voice,
            speed=speed,
            generate_fn=generate_audio,
            inject_model_arg=_inject_generate_audio_model_arg,
            inject_output_args=_inject_generate_audio_output_args,
        )

    @staticmethod
    def _is_network_error(error_str: str) -> bool:
        """Return True for download/network-related synthesis failures."""
        return any(
            token in error_str
            for token in [
                "download",
                "connection",
                "network",
                "timeout",
                "http",
                "repository",
            ]
        )

    @staticmethod
    def _is_model_loading_error(error_str: str) -> bool:
        """Return True for model-load/runtime signature failures."""
        return any(
            token in error_str for token in ["model", "load", "weight", "checkpoint"]
        )

    @staticmethod
    def _is_memory_pressure_error(error_str: str) -> bool:
        """Return True for VRAM/oom style failures."""
        return is_memory_pressure_error(error_str)

    @staticmethod
    def _clamp_speed(speed: float) -> float:
        """Clamp speech speed into runtime-supported range."""
        return max(MIN_SPEED, min(MAX_SPEED, speed))

    @staticmethod
    def _retry_wait_seconds(attempt: int) -> int:
        """Compute exponential-style wait between retry attempts."""
        return (attempt + 1) * 2

    def _retry_or_exhausted(
        self,
        *,
        message_prefix: str,
        attempt: int,
        max_retries: int,
    ) -> bool:
        """
        Sleep and retry if attempts remain.

        Returns:
            True when caller should retry; False when retries are exhausted.
        """
        if attempt >= max_retries - 1:
            return False
        self._retry_count += 1
        wait_time = self._retry_wait_seconds(attempt)
        logger.warning(
            "%s, retrying in %ss... (attempt %s/%s)",
            message_prefix,
            wait_time,
            attempt + 1,
            max_retries,
        )
        time.sleep(wait_time)
        return True

    def _synthesize_once(
        self,
        *,
        text: str,
        voice: str,
        speed: float,
        output_path: Optional[Path | str],
    ) -> np.ndarray:
        """Execute one synthesis attempt for either file or temp output mode."""
        if output_path is not None:
            return self._synthesize_to_output_path(
                text=text,
                voice=voice,
                speed=speed,
                output_path=Path(output_path),
            )
        return self._synthesize_to_temp_audio(text=text, voice=voice, speed=speed)

    def _synthesize_with_retries(
        self,
        synthesize_once: Callable[[], T],
        *,
        max_retries: int,
    ) -> T:
        """Run one synthesis operation with shared retry/error classification."""
        last_error = None
        for attempt in range(max_retries):
            try:
                return synthesize_once()

            except MemoryError:
                raise VRAMOverflowError(model_name=self._model_path)

            except (TTSInstallationError, SynthesisError):
                raise

            except (OSError, IOError) as e:
                error_str = str(e).lower()
                if self._is_network_error(error_str):
                    last_error = e
                    if self._retry_or_exhausted(
                        message_prefix="Model download failed",
                        attempt=attempt,
                        max_retries=max_retries,
                    ):
                        continue
                    raise TTSModelError(
                        message=f"Failed to download model after {max_retries} attempts: {str(e)}",
                        model_name=self._model_path,
                    )
                raise SynthesisError(message=str(e))

            except Exception as e:
                error_str = str(e).lower()
                # Allocation failures often mention the model being loaded, so
                # they must be recognized before the broad model-load match.
                if self._is_memory_pressure_error(error_str):
                    raise VRAMOverflowError(model_name=self._model_path)
                if self._is_model_loading_error(error_str):
                    if self._is_network_error(error_str):
                        last_error = e
                        if self._retry_or_exhausted(
                            message_prefix="Model load failed",
                            attempt=attempt,
                            max_retries=max_retries,
                        ):
                            continue
                    raise TTSModelError(
                        message=f"Model loading failed: {str(e)}",
                        model_name=self._model_path,
                    )
                raise SynthesisError(message=str(e))

        if last_error:
            raise TTSModelError(
                message=f"Failed after {max_retries} attempts: {str(last_error)}",
                model_name=self._model_path,
            )
        raise SynthesisError(message="Synthesis failed without an error")

    def synthesize(
        self,
        text: str,
        voice: Optional[str] = None,
        speed: float = 1.0,
        max_retries: int = 3,
        output_path: Optional[Path | str] = None,
    ) -> np.ndarray:
        """
        Synthesize speech from text.

        Args:
            text: Text to synthesize
            voice: Voice ID (e.g., 'am_adam', 'af_bella')
            speed: Speech speed multiplier (MIN_SPEED-MAX_SPEED)
            max_retries: Number of retries for model download failures
            output_path: Optional path to save audio directly (enables streaming)

        Returns:
            Audio as numpy array (float32, mono, 24kHz)

        Raises:
            TTSModelError: If model fails to load/download
            SynthesisError: If speech synthesis fails
            VRAMOverflowError: If not enough VRAM
        """
        start_time = time.time()

        if has_no_speakable_text(text):
            silence = self._blank_text_silence()
            if output_path is not None:
                self._write_blank_text_silence(output_path)
            return silence

        voice = voice or self.config.default_voice

        # Clamp speed to valid range
        speed = self._clamp_speed(speed)

        audio = self._synthesize_with_retries(
            lambda: self._synthesize_once(
                text=text,
                voice=voice,
                speed=speed,
                output_path=output_path,
            ),
            max_retries=max_retries,
        )
        self._update_synthesis_stats(start_time, len(text))
        return audio

    def synthesize_to_output_file(
        self,
        text: str,
        output_path: Path | str,
        voice: Optional[str] = None,
        speed: float = 1.0,
        max_retries: int = 3,
    ) -> Path:
        """
        Synthesize speech directly to a WAV path without returning audio samples.

        This is useful for long Kokoro runs where the pipeline can append WAV
        frames into the chapter accumulator and avoid an immediate readback into
        a NumPy array for every chunk.
        """
        start_time = time.time()
        output_path = Path(output_path)

        if has_no_speakable_text(text):
            return self._write_blank_text_silence(output_path)

        voice = voice or self.config.default_voice
        speed = self._clamp_speed(speed)

        written_path = self._synthesize_with_retries(
            lambda: self._synthesize_to_output_file(
                text=text,
                voice=voice,
                speed=speed,
                output_path=output_path,
            ),
            max_retries=max_retries,
        )
        self._update_synthesis_stats(start_time, len(text))
        return written_path

    def iter_synthesize_batch(
        self,
        items: list[BatchItem],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> Iterator[BatchResult]:
        """
        Incrementally synthesize multiple text chunks, one at a time.

        This is the streaming form of `synthesize_batch()`: each result is yielded
        as soon as its synthesis finishes.
        """
        total = len(items)
        for completed, item in enumerate(items, start=1):
            try:
                result = self._synthesize_single(item)
            except Exception as e:
                result = BatchResult(audio=None, index=item.index, error=str(e))

            if progress_callback:
                progress_callback(completed, total)
            yield result

    def _synthesize_single(self, item: BatchItem) -> BatchResult:
        """Synthesize a single batch item."""
        try:
            if item.output_path is not None:
                audio_path = self.synthesize_to_output_file(
                    text=item.text,
                    output_path=item.output_path,
                    voice=item.voice,
                    speed=item.speed,
                )
                duration_ms = _wav_duration_ms(audio_path, self.config.sample_rate)
                return BatchResult(
                    audio=None,
                    index=item.index,
                    error=None,
                    duration_ms=duration_ms,
                    audio_path=audio_path,
                )
            audio = self.synthesize(text=item.text, voice=item.voice, speed=item.speed)
            duration_ms = int(len(audio) / self.config.sample_rate * 1000)
            return BatchResult(
                audio=audio, index=item.index, error=None, duration_ms=duration_ms
            )
        except Exception as e:
            return BatchResult(
                audio=None, index=item.index, error=str(e), duration_ms=0
            )

    def unload_model(self, keep_in_cache: bool = True) -> None:
        """
        Unload the model and free memory.

        Important for VRAM management on 16GB unified memory.

        Args:
            keep_in_cache: If True, model stays in global cache for reuse
        """
        engine_runtime.unload_model(self, keep_in_cache=keep_in_cache)

    def get_metrics(self) -> dict[str, float | int | None]:
        """Return runtime performance counters for observability."""
        return engine_runtime.get_metrics(self)

    def __enter__(self):
        """Context manager entry."""
        self.load_model()
        return self

    def __exit__(self, _exc_type, _exc_val, _exc_tb):
        """Context manager exit - ensures cleanup."""
        self.unload_model(keep_in_cache=False)
        return False
