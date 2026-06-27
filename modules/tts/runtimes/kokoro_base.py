"""Provider-neutral Kokoro runtime contracts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator, Protocol

from modules.tts.runtime_spec import TTSRuntimeSpec
from modules.tts.runtimes.errors import TTSRuntimeUnavailable
from modules.tts.strategies.base import (
    SynthesisItem,
    SynthesisProgressCallback,
    SynthesisResult,
)

if TYPE_CHECKING:
    import numpy as np


@dataclass(frozen=True)
class KokoroRuntimeConfig:
    """Resolved configuration passed from the Kokoro strategy to runtimes."""

    runtime_spec: TTSRuntimeSpec
    model_name: str
    quantization: str
    sample_rate: int
    default_voice: str = "am_adam"
    max_chunk_tokens: int = 700
    batch_size: int = 4
    use_batching: bool = True
    enable_streaming: bool = True
    stream_buffer_size: int = 8192
    runtime_options: dict[str, object] | None = None

    @classmethod
    def from_strategy_config(cls, config: Any) -> "KokoroRuntimeConfig":
        """Build a runtime config from a Kokoro strategy config object."""
        runtime_spec = config.runtime_spec or TTSRuntimeSpec.from_legacy(
            "kokoro",
            config.quantization,
            model=config.model_name,
            options=(
                config.runtime_options
                if isinstance(config.runtime_options, dict)
                else None
            ),
        )
        if isinstance(config.runtime_options, dict) and config.runtime_options:
            runtime_options = dict(config.runtime_options)
        elif runtime_spec.runtime_options:
            runtime_options = dict(runtime_spec.runtime_options)
        else:
            runtime_options = None
        return cls(
            runtime_spec=runtime_spec,
            model_name=config.model_name,
            quantization=config.quantization,
            sample_rate=config.sample_rate,
            default_voice=getattr(config, "default_voice", None) or "am_adam",
            max_chunk_tokens=config.max_chunk_tokens,
            batch_size=config.batch_size,
            use_batching=config.use_batching,
            enable_streaming=config.enable_streaming,
            stream_buffer_size=config.stream_buffer_size,
            runtime_options=runtime_options,
        )


class KokoroRuntime(Protocol):
    """Provider-neutral runtime surface used by the Kokoro strategy."""

    provider: str
    sample_rate: int
    is_loaded: bool

    @property
    def chunks_processed(self) -> int: ...

    @property
    def average_chunk_time_ms(self) -> float: ...

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
        progress_callback: SynthesisProgressCallback | None = None,
    ) -> Iterator[SynthesisResult]: ...

    def get_metrics(self) -> dict[str, object]: ...


RuntimeBuilder = Callable[[KokoroRuntimeConfig], KokoroRuntime]


class KokoroRuntimeFactory:
    """Registry-backed factory for concrete Kokoro runtimes."""

    _registry: dict[tuple[str, str], RuntimeBuilder] = {}

    @classmethod
    def register(
        cls,
        provider: str,
        engine: str,
        builder: RuntimeBuilder,
    ) -> None:
        cls._registry[(provider.strip().lower(), engine.strip().lower())] = builder

    @classmethod
    def registered_keys(cls) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(cls._registry))

    def create_runtime(self, config: KokoroRuntimeConfig) -> KokoroRuntime:
        runtime_spec = config.runtime_spec
        key = (
            runtime_spec.provider.strip().lower(),
            runtime_spec.engine.strip().lower(),
        )
        try:
            builder = self._registry[key]
        except KeyError as exc:
            raise TTSRuntimeUnavailable(
                f"Kokoro runtime (provider={runtime_spec.provider!r}, "
                f"engine={runtime_spec.engine!r}) is not supported"
            ) from exc
        return builder(config)
