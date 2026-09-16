"""Package-backed Kokoro runtime adapter for CPU/CUDA hosts."""

from __future__ import annotations

import logging
import os
import time
import wave
from pathlib import Path
from typing import Iterator

import numpy as np

from modules.tts import model_cache
from modules.tts.model_snapshots import installed_model_path
from modules.tts.runtimes.errors import TTSRuntimeUnavailable
from modules.tts.runtimes.kokoro_base import KokoroRuntimeConfig, KokoroRuntimeFactory
from modules.tts.strategies.base import (
    SynthesisItem,
    SynthesisProgressCallback,
    SynthesisResult,
)

logger = logging.getLogger(__name__)

_TORCH_COMPILE_ENV_VAR = "AUDIOBOOK_TTS_TORCH_COMPILE"
_BLANK_TEXT_SILENCE_MS = 50


def _torch_compile_requested(runtime_options: dict[str, object] | None) -> bool:
    # An explicit runtime option wins in both directions; the env var only
    # applies when the option is absent.
    if runtime_options and "torch_compile" in runtime_options:
        return str(runtime_options["torch_compile"]).strip().lower() in ("1", "true", "yes")
    return os.environ.get(_TORCH_COMPILE_ENV_VAR, "").strip().lower() in ("1", "true", "yes")


class PackageKokoroRuntime:
    """Adapter around the upstream ``kokoro.KPipeline`` package."""

    supports_file_batch_results = False

    def __init__(self, config: KokoroRuntimeConfig) -> None:
        self._config = config
        self.provider = config.runtime_spec.provider
        self.sample_rate = config.sample_rate
        self._pipeline = None
        self._last_cached_pipeline = None
        self._chunks_processed = 0
        self._total_chunk_time_ms = 0.0

    @property
    def is_loaded(self) -> bool:
        return self._pipeline is not None

    @property
    def chunks_processed(self) -> int:
        return self._chunks_processed

    @property
    def average_chunk_time_ms(self) -> float:
        if self._chunks_processed <= 0:
            return 0.0
        return self._total_chunk_time_ms / self._chunks_processed

    def _cache_key(self) -> str:
        compiled = _torch_compile_requested(self._config.runtime_options)
        return (
            f"kokoro-package:{self._pipeline_kwargs()!r}:"
            f"{self._config.runtime_spec.cache_key()!r}:compile={compiled}"
        )

    def load(self) -> None:
        if self._pipeline is not None:
            return

        cached_pipeline = model_cache.get_cached_model(self._cache_key())
        if cached_pipeline is not None:
            self._pipeline = cached_pipeline
            self._last_cached_pipeline = cached_pipeline
            logger.info("Package Kokoro runtime loaded from cache")
            return

        try:
            from kokoro import KPipeline
        except ImportError as exc:
            raise TTSRuntimeUnavailable(
                "Package Kokoro runtime is unavailable. Install kokoro>=0.9.4."
            ) from exc

        kwargs = self._pipeline_kwargs()
        try:
            local_model_dir = self._local_model_dir()
            if local_model_dir is not None:
                from kokoro import KModel

                model_path = local_model_dir / "kokoro-v1_0.pth"
                config_path = local_model_dir / "config.json"
                kwargs["model"] = KModel(
                    repo_id="hexgrad/Kokoro-82M",
                    config=str(config_path),
                    model=str(model_path),
                ).to(self._config.runtime_spec.device or self.provider).eval()
                kwargs.pop("repo_id", None)
            self._pipeline = KPipeline(**kwargs)
        except TypeError:
            dropped_kwargs = [
                key for key in ("repo_id", "device") if key in kwargs
            ]
            for key in dropped_kwargs:
                kwargs.pop(key, None)
            if dropped_kwargs:
                logger.warning(
                    "Installed kokoro package does not accept KPipeline kwargs %s; "
                    "retrying without them",
                    ", ".join(dropped_kwargs),
                )
            self._pipeline = KPipeline(**kwargs)
        except Exception as exc:
            raise TTSRuntimeUnavailable(f"Package Kokoro runtime failed to load: {exc}") from exc

        self._maybe_compile_pipeline_model()
        model_cache.set_cached_model(self._cache_key(), self._pipeline)
        self._last_cached_pipeline = self._pipeline

    def _maybe_compile_pipeline_model(self) -> None:
        """Optionally torch.compile the vocoder decoder (opt-in, CUDA-oriented).

        Compilation trades a one-time warmup cost (tens of seconds, amortized by
        the inductor on-disk cache) for ~1.4x faster synthesis, so it only pays
        off for longer conversions. Only the decoder is compiled: prosody and
        durations stay eager and unchanged, while the compiled vocoder draws a
        different (statistically equivalent) noise realization for aspiration.

        torch.compile() itself is lazy — backend failures (no triton, unusable
        inductor) only surface at the first forward — so a probe synthesis runs
        before the pipeline is published, and any failure restores the eager
        decoder.
        """
        if not _torch_compile_requested(self._config.runtime_options):
            return
        model = getattr(self._pipeline, "model", None)
        if model is None:
            return
        eager_decoder = model.decoder
        try:
            import torch

            model.decoder = torch.compile(model.decoder, dynamic=True)
            probe_voice = self._voice_arg(self._config.default_voice)
            for _ in self._pipeline("compile check", voice=probe_voice):
                pass
            logger.info("Package Kokoro runtime enabled torch.compile for the decoder")
        except Exception as exc:
            model.decoder = eager_decoder
            logger.warning("torch.compile unavailable; using eager synthesis: %s", exc)

    def _local_model_dir(self) -> Path | None:
        model_name = self._config.runtime_spec.resolved_model()
        if model_name is None:
            return None
        candidate = Path(installed_model_path(model_name))
        if candidate.is_dir():
            return candidate
        return None

    def _pipeline_kwargs(self) -> dict[str, object]:
        language_code = "a"
        if self._config.default_voice.startswith("b"):
            language_code = "b"
        kwargs: dict[str, object] = {
            "lang_code": language_code,
            "repo_id": self._config.runtime_spec.resolved_model(),
        }
        if self._config.runtime_spec.device:
            kwargs["device"] = self._config.runtime_spec.device
        return kwargs

    def _voice_arg(self, voice: str) -> str:
        local_model_dir = self._local_model_dir()
        if local_model_dir is None or voice.endswith(".pt"):
            return voice
        voice_path = local_model_dir / "voices" / f"{voice}.pt"
        if voice_path.is_file():
            return str(voice_path)
        return voice

    def _write_output_audio(self, output_path: Path, audio: np.ndarray) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import soundfile as sf
        except ModuleNotFoundError:
            pcm = np.clip(np.asarray(audio, dtype=np.float32).reshape(-1), -1.0, 1.0)
            pcm16 = (pcm * 32767.0).astype(np.int16, copy=False)
            with wave.open(str(output_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(self.sample_rate)
                wav_file.writeframes(pcm16.tobytes())
            return

        sf.write(str(output_path), audio, self.sample_rate)

    def unload(self, *, keep_in_cache: bool = True) -> None:
        pipeline = self._pipeline
        if pipeline is None:
            pipeline = self._last_cached_pipeline
        if not keep_in_cache and pipeline is not None:
            model_cache.discard_cached_model_if_same(
                self._cache_key(),
                pipeline,
            )
            self._last_cached_pipeline = None
        elif keep_in_cache and pipeline is not None:
            self._last_cached_pipeline = pipeline
        self._pipeline = None

    def warmup(
        self,
        *,
        voice: str | None = None,
        speed: float = 1.0,
        probe_text: str = "warmup check",
    ) -> None:
        self.synthesize(probe_text, voice or self._config.default_voice, speed)

    def synthesize(
        self,
        text: str,
        voice: str,
        speed: float = 1.0,
        output_path: Path | None = None,
    ) -> np.ndarray:
        if self._pipeline is None:
            raise RuntimeError("Model not loaded. Call load() first.")

        started_at = time.perf_counter()
        if not text.strip():
            # Keep blank chunks structurally valid for the pipeline. The MLX
            # adapter gets this behavior from TTSEngine; package-backed
            # runtimes need to provide the same short scene-break silence.
            frames = max(1, int(self.sample_rate * _BLANK_TEXT_SILENCE_MS / 1000))
            audio = np.zeros(frames, dtype=np.float32)
            if output_path is not None:
                self._write_output_audio(output_path, audio)
            self._chunks_processed += 1
            self._total_chunk_time_ms += (time.perf_counter() - started_at) * 1000
            return audio

        # The pipeline is shared with every other runtime holding the same cache
        # key (e.g. a voice preview raised during a conversion). KPipeline keeps
        # mutable per-call state, so calls into it must not interleave.
        with model_cache.get_model_lock(self._cache_key()):
            audio_chunks = [
                np.asarray(audio, dtype=np.float32)
                for _graphemes, _phonemes, audio in self._pipeline(
                    text,
                    voice=self._voice_arg(voice),
                    speed=speed,
                )
            ]
        if audio_chunks:
            audio = np.concatenate(audio_chunks)
        else:
            audio = np.array([], dtype=np.float32)

        if output_path is not None:
            self._write_output_audio(output_path, audio)

        self._chunks_processed += 1
        self._total_chunk_time_ms += (time.perf_counter() - started_at) * 1000
        return audio

    def iter_synthesize_batch(
        self,
        items: list[SynthesisItem],
        progress_callback: SynthesisProgressCallback | None = None,
    ) -> Iterator[SynthesisResult]:
        total = len(items)
        for offset, item in enumerate(items, start=1):
            try:
                audio = self.synthesize(
                    item.text,
                    item.voice,
                    item.speed,
                    item.output_path,
                )
                yield SynthesisResult(
                    audio=audio,
                    index=item.index,
                    duration_ms=int(len(audio) / self.sample_rate * 1000),
                    audio_path=item.output_path,
                )
            except Exception as exc:
                yield SynthesisResult(
                    audio=None,
                    index=item.index,
                    error=str(exc),
                    duration_ms=0,
                    audio_path=item.output_path,
                )
            if progress_callback is not None:
                progress_callback(offset, total)

    def get_metrics(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "chunks_processed": self.chunks_processed,
            "average_chunk_time_ms": self.average_chunk_time_ms,
        }


def register_package_kokoro_runtimes() -> None:
    """Register upstream package Kokoro runtime implementations."""
    KokoroRuntimeFactory.register("cpu", "kokoro", PackageKokoroRuntime)
    KokoroRuntimeFactory.register("cuda", "kokoro", PackageKokoroRuntime)
