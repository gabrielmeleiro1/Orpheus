"""Package-backed Kokoro runtime adapter for CPU/CUDA hosts."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Iterator

import numpy as np

from modules.tts.runtimes.errors import TTSRuntimeUnavailable
from modules.tts.runtimes.kokoro_base import KokoroRuntimeConfig, KokoroRuntimeFactory
from modules.tts.strategies.base import (
    SynthesisItem,
    SynthesisProgressCallback,
    SynthesisResult,
)


class PackageKokoroRuntime:
    """Adapter around the upstream ``kokoro.KPipeline`` package."""

    def __init__(self, config: KokoroRuntimeConfig) -> None:
        self._config = config
        self.provider = config.runtime_spec.provider
        self.sample_rate = config.sample_rate
        self._pipeline = None
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

    def load(self) -> None:
        if self._pipeline is not None:
            return

        try:
            from kokoro import KModel, KPipeline
        except ImportError as exc:
            raise TTSRuntimeUnavailable(
                "Package Kokoro runtime is unavailable. Install kokoro>=0.9.4."
            ) from exc

        kwargs = self._pipeline_kwargs()
        try:
            local_model_dir = self._local_model_dir()
            if local_model_dir is not None:
                model_path = local_model_dir / "kokoro-v1_0.pth"
                config_path = local_model_dir / "config.json"
                kwargs["model"] = KModel(
                    repo_id="hexgrad/Kokoro-82M",
                    config=str(config_path),
                    model=str(model_path),
                )
                kwargs.pop("repo_id", None)
            self._pipeline = KPipeline(**kwargs)
        except TypeError:
            kwargs.pop("repo_id", None)
            kwargs.pop("device", None)
            self._pipeline = KPipeline(**kwargs)
        except Exception as exc:
            raise TTSRuntimeUnavailable(f"Package Kokoro runtime failed to load: {exc}") from exc

    def _local_model_dir(self) -> Path | None:
        model_name = self._config.runtime_spec.resolved_model()
        if model_name is None:
            return None
        candidate = Path(model_name)
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

    def unload(self, *, keep_in_cache: bool = True) -> None:
        _ = keep_in_cache
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
            import soundfile as sf

            output_path.parent.mkdir(parents=True, exist_ok=True)
            sf.write(str(output_path), audio, self.sample_rate)

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
            started_at = time.perf_counter()
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
                    duration_ms=int((time.perf_counter() - started_at) * 1000),
                    audio_path=item.output_path,
                )
            except Exception as exc:
                yield SynthesisResult(
                    audio=None,
                    index=item.index,
                    error=str(exc),
                    duration_ms=int((time.perf_counter() - started_at) * 1000),
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
