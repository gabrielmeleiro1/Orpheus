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
from modules.tts.runtimes.kokoro_mlx import MLXKokoroRuntime, register_default_kokoro_runtimes
from modules.tts.runtimes.kokoro_package import (
    PackageKokoroRuntime,
    register_package_kokoro_runtimes,
)

register_default_kokoro_runtimes()
register_package_kokoro_runtimes()

__all__ = [
    "KokoroRuntime",
    "KokoroRuntimeConfig",
    "KokoroRuntimeFactory",
    "MLXKokoroRuntime",
    "PackageKokoroRuntime",
    "TTSRuntimeInitializationError",
    "TTSRuntimeMemoryError",
    "TTSRuntimeUnavailable",
    "register_default_kokoro_runtimes",
    "register_package_kokoro_runtimes",
]
