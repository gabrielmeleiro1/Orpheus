"""Runtime provider adapters for concrete TTS backends."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Protocol

from modules.tts.strategies.base import SynthesisItem, SynthesisResult

from modules.tts.engine_config import TTSConfig as EngineConfig
from modules.tts.runtimes.errors import TTSRuntimeUnavailable
from modules.tts.runtime_spec import TTSRuntimeSpec

if TYPE_CHECKING:
    import numpy as np


class KokoroRuntime(Protocol):
    """Provider-neutral runtime surface used by the Kokoro strategy."""

    provider: str
    sample_rate: int
    is_loaded: bool

    @property
    def chunks_processed(self) -> int: ...

    def load(self) -> None: ...

    def unload(self, *, keep_in_cache: bool = True) -> None: ...

    def warmup(
        self,
        *,
        voice: str | None = None,
        speed: float = 1.0,
        probe_text: str = "warmup check",
    ) -> None: ...

    def synthesize(
        self,
        text: str,
        voice: str,
        speed: float = 1.0,
        output_path: Path | None = None,
    ) -> np.ndarray: ...

    def iter_synthesize_batch(
        self,
        items: list[SynthesisItem],
        progress_callback=None,  # noqa: ANN001
    ) -> Iterator[SynthesisResult]: ...

    @property
    def average_chunk_time_ms(self) -> float: ...

    def get_metrics(self) -> dict[str, object]: ...


class KokoroRuntimeProvider(Protocol):
    """Factory for a provider-specific Kokoro runtime."""

    def create_runtime(self, config) -> KokoroRuntime: ...  # noqa: ANN001


class MLXKokoroRuntime:
    """Provider-neutral adapter around the legacy MLX TTSEngine facade."""

    provider = "mlx"

    def __init__(self, engine) -> None:  # noqa: ANN001
        self._engine = engine

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
        progress_callback=None,  # noqa: ANN001
    ) -> Iterator[SynthesisResult]:
        yield from self._engine.iter_synthesize_batch(
            items,
            progress_callback=progress_callback,
        )

    def get_metrics(self) -> dict[str, object]:
        return self._engine.get_metrics()


class MLXKokoroRuntimeProvider:
    """MLX-backed Kokoro runtime provider."""

    def create_runtime(self, config) -> KokoroRuntime:  # noqa: ANN001
        from modules.tts.engine import TTSEngine

        runtime_spec = config.runtime_spec or TTSRuntimeSpec.from_legacy(
            "kokoro",
            config.quantization,
            model=config.model_name,
            provider="mlx",
            options=config.runtime_options if isinstance(config.runtime_options, dict) else None,
        )
        identity = runtime_spec.engine_config_kwargs()
        engine_config = EngineConfig(
            model_name=identity["model_name"],
            quantization=identity["quantization"],
            sample_rate=config.sample_rate,
            default_voice="am_adam",
            max_chunk_tokens=config.max_chunk_tokens,
            batch_size=config.batch_size,
            use_batching=config.use_batching,
            enable_streaming=config.enable_streaming,
            stream_buffer_size=config.stream_buffer_size,
            runtime_cache_key=identity["runtime_cache_key"],
        )
        return MLXKokoroRuntime(TTSEngine(engine_config))


def create_kokoro_runtime_provider(runtime_spec: TTSRuntimeSpec) -> KokoroRuntimeProvider:
    """Resolve a Kokoro runtime provider from canonical identity."""
    if runtime_spec.provider == "mlx":
        return MLXKokoroRuntimeProvider()
    raise TTSRuntimeUnavailable(
        f"Kokoro provider '{runtime_spec.provider}' is not supported"
    )
