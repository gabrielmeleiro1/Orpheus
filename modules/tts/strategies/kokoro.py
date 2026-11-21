"""
Kokoro TTS Strategy
===================
TTSStrategy implementation for Kokoro-82M runtimes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

import numpy as np

from modules.tts.runtime_spec import TTSRuntimeSpec
from modules.tts.runtimes import KokoroRuntime, KokoroRuntimeConfig, KokoroRuntimeFactory
from modules.tts.strategies.base import (
    SynthesisItem,
    SynthesisProgressCallback,
    SynthesisResult,
    TTSCapabilities,
    TTSConfig,
    TTSStrategy,
    TTSVoice,
)
from modules.tts.voices import KOKORO_VOICE_CATALOG


@dataclass
class KokoroConfig(TTSConfig):
    """Configuration specific to Kokoro TTS."""

    model_name: str = "mlx-community/Kokoro-82M"
    default_voice: str = "am_adam"
    enable_streaming: bool = True
    stream_buffer_size: int = 8192
    runtime_options: Optional[dict[str, object]] = None


class KokoroTTSStrategy(TTSStrategy):
    """
    Kokoro-82M TTS strategy using provider-specific runtime adapters.

    The current grouped multi-item path uses scheduler-driven chunk submission
    around per-item synthesis, not native runtime batch inference.
    """

    supports_incremental_batch_results = True

    def __init__(
        self,
        config: Optional[KokoroConfig] = None,
        *,
        runtime_factory: Optional[KokoroRuntimeFactory] = None,
    ):
        """
        Initialize Kokoro TTS strategy.

        Args:
            config: Kokoro-specific configuration
        """
        resolved_config = config or KokoroConfig()
        super().__init__(resolved_config)
        self._runtime: Optional[KokoroRuntime] = None
        self._runtime_factory = runtime_factory
        self._config = resolved_config

    # ==================== Properties ====================

    @property
    def name(self) -> str:
        return "kokoro"

    @property
    def display_name(self) -> str:
        return "Kokoro 82M"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def sample_rate(self) -> int:
        return self.config.sample_rate

    @property
    def supported_voices(self) -> list[TTSVoice]:
        return [
            TTSVoice(
                id=str(entry["id"]),
                name=str(entry["name"]),
                language=str(entry["language"]),
                locale=str(entry.get("locale") or entry["language"]),
                gender=str(entry["gender"]),
                description=str(entry["description"]),
                style_tags=tuple(entry.get("style_tags", [])),
                sample_text=entry.get("sample_text") if isinstance(entry.get("sample_text"), str) else None,
                is_multilingual=bool(entry.get("is_multilingual", False)),
            )
            for entry in KOKORO_VOICE_CATALOG
        ]

    @property
    def supports_file_batch_results(self) -> bool:
        """Whether the active runtime writes chunk files natively.

        Runtimes that only return in-memory audio (the CPU/CUDA package
        adapter) leave this False so the pipeline never requests a pure
        write+readback round-trip on the synthesis hot path.
        """
        if self._runtime is not None:
            return bool(getattr(self._runtime, "supports_file_batch_results", False))
        # Before load, fall back to the provider the runtime would resolve to,
        # matching the capabilities property.
        return self._provider() == "mlx"

    @property
    def supports_batching(self) -> bool:
        return True

    @property
    def supports_streaming(self) -> bool:
        return self._config.enable_streaming

    def _provider(self) -> str:
        runtime_spec = self._config.runtime_spec
        if runtime_spec is not None:
            return runtime_spec.provider
        return TTSRuntimeSpec.default_provider("kokoro")

    @property
    def capabilities(self) -> TTSCapabilities:
        provider = self._provider()
        return TTSCapabilities(
            supports_batching=True,
            supports_streaming=self._config.enable_streaming,
            supports_native_batching=False,
            batching_mode="threaded",
            supported_quantizations=("bf16",),
            supported_languages=("en-US", "en-GB"),
            supports_voice_cloning=False,
            supports_engine_options=True,
            is_local=True,
            provider=provider,
            supports_conversion=True,
            worker_auto_mode="sequential",
            process_safe=False,
        )

    def describe_engine_options(self) -> dict[str, object]:
        default_quantization = "bf16"
        return {
            "quantization": {
                "type": "string",
                "enum": ["bf16"],
                "default": default_quantization,
            },
            "enable_streaming": {
                "type": "boolean",
                "default": self._config.enable_streaming,
            },
        }

    # ==================== Lifecycle ====================

    def load(self) -> None:
        """
        Load the Kokoro TTS model.

        Raises:
            ImportError: If mlx-audio not installed
        """
        if self._is_loaded and self._runtime is not None:
            return

        runtime_config = KokoroRuntimeConfig.from_strategy_config(self._config)
        if self._config.runtime_spec is None:
            self._config.runtime_spec = runtime_config.runtime_spec
        runtime_factory = self._runtime_factory or KokoroRuntimeFactory()
        self._runtime = runtime_factory.create_runtime(runtime_config)
        self._runtime.load()
        self._is_loaded = True

    def unload(self, keep_in_cache: bool = True) -> None:
        """Unload the model to free memory."""
        if self._runtime is not None:
            self._runtime.unload(keep_in_cache=keep_in_cache)
            self._runtime = None
        self._is_loaded = False

    def warmup(
        self,
        voice: Optional[str] = None,
        speed: float = 1.0,
        probe_text: str = "warmup check",
    ) -> None:
        if not self._is_loaded or self._runtime is None:
            raise RuntimeError("Model not loaded. Call load() first.")
        self._runtime.warmup(
            voice=voice,
            speed=speed,
            probe_text=probe_text,
        )

    # ==================== Synthesis ====================

    def synthesize(
        self,
        text: str,
        voice: str,
        speed: float = 1.0,
        output_path: Optional[Path] = None,
    ) -> np.ndarray:
        """
        Synthesize speech using Kokoro.

        Args:
            text: Text to synthesize
            voice: Voice ID or blend of supported voice IDs
            speed: Speech speed multiplier (MIN_SPEED-MAX_SPEED)
            output_path: Optional path to save audio

        Returns:
            Audio as numpy array (float32, mono, 24kHz)

        Raises:
            ValueError: If voice not supported
            RuntimeError: If synthesis fails
        """
        if not self._is_loaded or self._runtime is None:
            raise RuntimeError("Model not loaded. Call load() first.")

        if not self.validate_voice(voice):
            raise ValueError(
                f"Voice '{voice}' not supported. "
                f"Available: {[v.id for v in self.supported_voices]}"
            )

        speed = self._clamp_speed(speed)
        if "," in voice:
            voice = ",".join(component.strip() for component in voice.split(","))

        return self._runtime.synthesize(
            text=text,
            voice=voice,
            speed=speed,
            output_path=output_path,
        )

    def synthesize_batch(
        self,
        items: list[SynthesisItem],
        progress_callback: Optional[SynthesisProgressCallback] = None,
    ) -> list[SynthesisResult]:
        """
        Synthesize multiple texts via grouped multi-item scheduling.

        The underlying engine still synthesizes each item individually; this
        does not call a native batched MLX inference API.

        Args:
            items: List of synthesis items to synthesize
            progress_callback: Optional callback with (completed, total)

        Returns:
            Ordered synthesis results
        """
        return list(
            self.iter_synthesize_batch(
                items=items,
                progress_callback=progress_callback,
            )
        )

    def iter_synthesize_batch(
        self,
        items: list[SynthesisItem],
        progress_callback: Optional[SynthesisProgressCallback] = None,
    ) -> Iterator[SynthesisResult]:
        """
        Incrementally synthesize multiple texts via grouped multi-item scheduling.
        """
        if not self._is_loaded or self._runtime is None:
            raise RuntimeError("Model not loaded. Call load() first.")

        for item in items:
            if not self.validate_voice(item.voice):
                raise ValueError(f"Voice '{item.voice}' not supported.")

        normalized_items = [
            SynthesisItem(
                text=item.text,
                voice=(
                    ",".join(component.strip() for component in item.voice.split(","))
                    if "," in item.voice else item.voice
                ),
                speed=self._clamp_speed(item.speed),
                index=item.index,
                output_path=item.output_path,
            )
            for item in items
        ]
        yield from self._runtime.iter_synthesize_batch(
            normalized_items,
            progress_callback=progress_callback,
        )
