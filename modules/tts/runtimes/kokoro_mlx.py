"""MLX-backed Kokoro runtime adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import numpy as np

from modules.tts.engine_config import TTSConfig as EngineConfig
from modules.tts.runtimes.errors import TTSRuntimeUnavailable
from modules.tts.runtimes.kokoro_base import KokoroRuntimeConfig, KokoroRuntimeFactory
from modules.tts.strategies.base import (
    SynthesisItem,
    SynthesisProgressCallback,
    SynthesisResult,
)


class MLXKokoroRuntime:
    """Provider-neutral adapter around the legacy MLX TTSEngine facade."""

    provider = "mlx"

    def __init__(self, config: KokoroRuntimeConfig) -> None:
        self._engine = self._create_engine(config)

    @staticmethod
    def _create_engine(config: KokoroRuntimeConfig):  # noqa: ANN205
        try:
            from modules.tts.engine import TTSEngine
        except ImportError as exc:
            raise TTSRuntimeUnavailable(
                "MLX Kokoro runtime is unavailable because the MLX TTS engine "
                "could not be imported"
            ) from exc

        identity = config.runtime_spec.engine_config_kwargs()
        engine_config = EngineConfig(
            model_name=identity["model_name"],
            quantization=identity["quantization"],
            sample_rate=config.sample_rate,
            default_voice=config.default_voice,
            max_chunk_tokens=config.max_chunk_tokens,
            batch_size=config.batch_size,
            use_batching=config.use_batching,
            enable_streaming=config.enable_streaming,
            stream_buffer_size=config.stream_buffer_size,
            runtime_cache_key=identity["runtime_cache_key"],
        )
        try:
            return TTSEngine(engine_config)
        except ImportError as exc:
            raise TTSRuntimeUnavailable(
                "MLX Kokoro runtime is unavailable because MLX dependencies "
                "could not be loaded"
            ) from exc

    @property
    def sample_rate(self) -> int:
        return int(getattr(self._engine.config, "sample_rate", 24000))

    @property
    def is_loaded(self) -> bool:
        return bool(getattr(self._engine, "_model_loaded", False))

    @property
    def chunks_processed(self) -> int:
        return int(getattr(self._engine, "_total_chunks_processed", 0))

    @property
    def average_chunk_time_ms(self) -> float:
        return float(getattr(self._engine, "average_chunk_time_ms", 0.0))

    def load(self) -> None:
        self._engine.load_model()

    def unload(self, *, keep_in_cache: bool = True) -> None:
        self._engine.unload_model(keep_in_cache=keep_in_cache)

    def warmup(
        self,
        *,
        voice: str | None = None,
        speed: float = 1.0,
        probe_text: str = "warmup check",
    ) -> None:
        self._engine.warmup_model(
            voice=voice,
            speed=speed,
            probe_text=probe_text,
        )

    def synthesize(
        self,
        text: str,
        voice: str,
        speed: float = 1.0,
        output_path: Path | None = None,
    ) -> np.ndarray:
        return self._engine.synthesize(
            text=text,
            voice=voice,
            speed=speed,
            output_path=output_path,
        )

    def iter_synthesize_batch(
        self,
        items: list[SynthesisItem],
        progress_callback: SynthesisProgressCallback | None = None,
    ) -> Iterator[SynthesisResult]:
        yield from self._engine.iter_synthesize_batch(
            items,
            progress_callback=progress_callback,
        )

    def get_metrics(self) -> dict[str, object]:
        return self._engine.get_metrics()


def register_default_kokoro_runtimes() -> None:
    """Register built-in Kokoro runtime implementations."""
    KokoroRuntimeFactory.register("mlx", "kokoro", MLXKokoroRuntime)
