"""Configuration types for the legacy Kokoro TTS engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass
class TTSConfig:
    """Configuration for TTS engine."""

    model_name: str = "mlx-community/Kokoro-82M"
    quantization: Literal["4bit", "6bit", "8bit", "bf16"] = "bf16"
    sample_rate: int = 24000
    default_voice: str = "am_adam"
    max_chunk_tokens: int = 700
    batch_size: int = 4
    use_batching: bool = True
    enable_streaming: bool = True
    stream_buffer_size: int = 8192
    runtime_cache_key: str | None = None
