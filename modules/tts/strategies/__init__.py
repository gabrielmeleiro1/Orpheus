"""
TTS Strategies Module
=====================
Pluggable TTS engine implementations.

Available Strategies:
    - KokoroTTSStrategy: Kokoro-82M via mlx-audio (current default)

Usage:
    from modules.tts.strategies import KokoroTTSStrategy, TTSVoice
    from modules.tts.factory import TTSEngineFactory

    engine = TTSEngineFactory.create("kokoro")
"""

from __future__ import annotations

from modules.tts.strategies.base import (
    SynthesisItem,
    SynthesisResult,
    TTSCapabilities,
    TTSConfig,
    TTSStrategy,
    TTSVoice,
)


def __getattr__(name: str):
    if name in {"KokoroConfig", "KokoroTTSStrategy"}:
        from modules.tts.strategies.kokoro import KokoroConfig, KokoroTTSStrategy

        return {
            "KokoroConfig": KokoroConfig,
            "KokoroTTSStrategy": KokoroTTSStrategy,
        }[name]

    raise AttributeError(name)


__all__ = [
    "TTSStrategy",
    "TTSVoice",
    "SynthesisItem",
    "SynthesisResult",
    "TTSCapabilities",
    "TTSConfig",
    "KokoroTTSStrategy",
    "KokoroConfig",
]
