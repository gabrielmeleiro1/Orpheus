"""Provider runtime implementations for TTS engines."""

from modules.tts.runtimes.errors import (
    TTSRuntimeInitializationError,
    TTSRuntimeMemoryError,
    TTSRuntimeUnavailable,
)
from modules.tts.runtimes.kokoro_base import (
    KokoroRuntime,
    KokoroRuntimeConfig,
    KokoroRuntimeFactory,
)

# Import for registry side effect. TTSEngine import is deferred until a concrete
# MLX runtime instance is created.
from modules.tts.runtimes.kokoro_mlx import MLXKokoroRuntime

__all__ = [
    "KokoroRuntime",
    "KokoroRuntimeConfig",
    "KokoroRuntimeFactory",
    "MLXKokoroRuntime",
    "TTSRuntimeInitializationError",
    "TTSRuntimeMemoryError",
    "TTSRuntimeUnavailable",
]
