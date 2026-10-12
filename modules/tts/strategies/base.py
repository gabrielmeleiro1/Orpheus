"""
TTS Strategy Base Interface
============================
Abstract base class for Text-to-Speech engine strategies.
Enables pluggable TTS engines (Kokoro and future providers).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator, Literal, Optional

if TYPE_CHECKING:
    import numpy as np


BatchingMode = Literal["none", "threaded", "native"]
WorkerAutoMode = Literal["sequential", "memory_safe", "cpu_cores"]
SynthesisProgressCallback = Callable[[int, int], None]


@dataclass(frozen=True)
class TTSVoice:
    """Represents a TTS voice option."""

    id: str
    name: str
    language: str
    gender: Literal["male", "female", "neutral"]
    description: str
    is_multilingual: bool = False
    locale: Optional[str] = None
    style_tags: tuple[str, ...] = ()
    sample_text: Optional[str] = None


@dataclass(frozen=True)
class SynthesisItem:
    """A single synthesis request item for grouped chunk processing."""

    text: str
    voice: str
    speed: float = 1.0
    index: int = 0
    output_path: Optional[Path] = None


@dataclass
class SynthesisResult:
    """Result payload for one synthesis request item."""

    audio: Optional[np.ndarray]
    index: int
    error: Optional[str] = None
    duration_ms: int = 0
    audio_path: Optional[Path] = None


@dataclass(frozen=True)
class TTSCapabilities:
    """Normalized runtime capability contract across TTS engines."""

    supports_batching: bool
    supports_streaming: bool
    supports_native_batching: bool = False
    batching_mode: BatchingMode = "none"
    supported_quantizations: tuple[str, ...] = ("bf16", "8bit", "6bit", "4bit")
    supported_languages: tuple[str, ...] = ()
    supports_voice_cloning: bool = False
    supports_engine_options: bool = False
    is_local: bool = True
    provider: str = "local"
    supports_conversion: bool = True
    worker_auto_mode: WorkerAutoMode = "memory_safe"
    process_safe: bool = False


@dataclass
class TTSConfig:
    """Base configuration for TTS engines."""

    sample_rate: int = 24000
    default_speed: float = 1.0
    max_chunk_tokens: int = 700
    quantization: Literal["4bit", "6bit", "8bit", "bf16"] = "bf16"
    batch_size: int = 4
    use_batching: bool = True
    runtime_spec: Optional[Any] = None
    runtime_options: Optional[Any] = None


class TTSStrategy(ABC):
    """
    Abstract base class for TTS engine strategies.

    Implementations:
        - KokoroTTSStrategy: Kokoro-82M via provider-specific runtime adapters
        - OpenAITTSStrategy: OpenAI TTS API (future)
    """

    def __init__(self, config: Optional[TTSConfig] = None):
        """
        Initialize the TTS strategy.

        Args:
            config: TTS configuration (uses defaults if None)
        """
        self.config = config or TTSConfig()
        self._is_loaded = False

    # ==================== Properties ====================

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Strategy name identifier.

        Returns:
            Short name like 'kokoro', etc.
        """

    @property
    @abstractmethod
    def display_name(self) -> str:
        """
        Human-readable display name.

        Returns:
            Name like 'Kokoro 82M', etc.
        """

    @property
    @abstractmethod
    def version(self) -> str:
        """
        Engine version string.

        Returns:
            Version like '1.0.0'
        """

    @property
    def is_loaded(self) -> bool:
        """Check if the model is currently loaded."""
        return self._is_loaded

    @property
    @abstractmethod
    def sample_rate(self) -> int:
        """
        Audio sample rate in Hz.

        Returns:
            Sample rate like 24000, 22050, etc.
        """

    @property
    @abstractmethod
    def supported_voices(self) -> list[TTSVoice]:
        """
        List of supported voice options.

        Returns:
            List of TTSVoice dataclasses
        """

    @property
    @abstractmethod
    def supports_batching(self) -> bool:
        """
        Legacy compatibility flag for grouped multi-item synthesis support.

        Returns:
            True if the engine exposes a grouped multi-item synthesis path
        """

    @property
    @abstractmethod
    def supports_streaming(self) -> bool:
        """
        Whether this engine supports streaming generation.

        Returns:
            True if streaming is supported
        """

    @property
    def capabilities(self) -> TTSCapabilities:
        """Capability contract consumed by API/UI surfaces."""
        languages = sorted({voice.language for voice in self.supported_voices if voice.language})
        return TTSCapabilities(
            supports_batching=self.supports_batching,
            supports_native_batching=False,
            batching_mode="threaded" if self.supports_batching else "none",
            supports_streaming=self.supports_streaming,
            supported_languages=tuple(languages),
            supports_engine_options=bool(self.describe_engine_options()),
        )

    def describe_engine_options(self) -> dict[str, object]:
        """
        Engine-specific options schema for additive API payload support.

        Engines can override this to expose additional configuration knobs.
        """
        return {}

    def to_engine_info(self) -> dict[str, object]:
        """Canonical engine metadata payload."""
        return {
            "id": self.name,
            "name": self.display_name,
            "display_name": self.display_name,
            "version": self.version,
            "sample_rate": self.sample_rate,
            "capabilities": asdict(self.capabilities),
            "supported_quantizations": list(self.capabilities.supported_quantizations),
            "options_schema": self.describe_engine_options(),
            "voices": [
                {
                    "id": voice.id,
                    "name": voice.name,
                    "language": voice.language,
                    "locale": voice.locale or voice.language,
                    "gender": voice.gender,
                    "description": voice.description,
                    "style_tags": list(voice.style_tags),
                    "sample_text": voice.sample_text,
                    "is_multilingual": bool(voice.is_multilingual),
                }
                for voice in self.supported_voices
            ],
        }

    # ==================== Lifecycle ====================

    @abstractmethod
    def load(self) -> None:
        """
        Load the TTS model into memory.

        Raises:
            ImportError: If required dependencies not installed
            RuntimeError: If model fails to load
        """

    @abstractmethod
    def unload(self, keep_in_cache: bool = True) -> None:
        """
        Unload the model to free memory.
        Safe to call even if not loaded.
        """

    def warmup(
        self,
        voice: Optional[str] = None,
        speed: float = 1.0,
        probe_text: str = "warmup check",
    ) -> None:
        """
        Optional runtime warm-up hook for strategies with expensive first synthesis.
        """
        _ = voice, speed, probe_text

    # ==================== Synthesis ====================

    @abstractmethod
    def synthesize(
        self,
        text: str,
        voice: str,
        speed: float = 1.0,
        output_path: Optional[Path] = None,
    ) -> np.ndarray:
        """
        Synthesize speech from text.

        Args:
            text: Text to synthesize
            voice: Voice ID (must be in supported_voices)
            speed: Speech speed multiplier (0.5-2.0)
            output_path: Optional path to save audio directly

        Returns:
            Audio as numpy array (float32, mono)

        Raises:
            ValueError: If voice not supported
            RuntimeError: If synthesis fails
        """

    def synthesize_batch(
        self,
        items: list[SynthesisItem],
        progress_callback: Optional[SynthesisProgressCallback] = None,
    ) -> list[SynthesisResult]:
        """
        Synthesize multiple items through a grouped multi-item path.

        Default implementation processes items sequentially.
        Engines may override this with grouped scheduling or native batch inference.

        Args:
            items: Ordered synthesis items
            progress_callback: Optional callback with (completed, total)

        Returns:
            List of synthesis results preserving input order
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
        Incrementally synthesize multiple items.

        The default implementation processes items sequentially and yields each
        result as soon as it is available. `synthesize_batch()` remains the
        list-returning compatibility wrapper.

        Args:
            items: Ordered synthesis items
            progress_callback: Optional callback with (completed, total)

        Yields:
            Synthesis results as they are produced
        """
        if not items:
            return

        total = len(items)
        for completed, item in enumerate(items, start=1):
            try:
                import numpy as np

                audio = np.asarray(
                    self.synthesize(
                        item.text,
                        item.voice,
                        item.speed,
                    ),
                    dtype=np.float32,
                )
                duration_ms = int(len(audio) / self.sample_rate * 1000) if len(audio) else 0
                result = SynthesisResult(
                    audio=audio,
                    index=item.index,
                    error=None,
                    duration_ms=duration_ms,
                )
            except Exception as exc:
                result = SynthesisResult(
                    audio=None,
                    index=item.index,
                    error=str(exc),
                    duration_ms=0,
                )

            if progress_callback is not None:
                progress_callback(completed, total)
            yield result

    # ==================== Utilities ====================

    def validate_voice(self, voice: str) -> bool:
        """
        Check if a voice ID or comma-separated blend is supported.

        Args:
            voice: Voice ID or blend to validate

        Returns:
            True if voice is supported
        """
        if "," not in voice:
            return any(v.id == voice for v in self.supported_voices)
        supported = {v.id for v in self.supported_voices}
        components = [component.strip() for component in voice.split(",")]
        return all(component and component in supported for component in components)

    def _clamp_speed(self, speed: float) -> float:
        """
        Clamp speed to valid range.

        Args:
            speed: Requested speed

        Returns:
            Speed clamped to 0.5-2.0 range
        """
        return max(0.5, min(2.0, speed))
