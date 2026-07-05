"""
TTS Module
==========
Text-to-Speech engine using Kokoro-82M provider runtimes.

Package exports are loaded lazily so lightweight modules such as
``modules.tts.runtime_spec`` do not import ML/NumPy dependencies.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {
    # Legacy
    "TTSEngine": ("modules.tts.engine", "TTSEngine"),
    "TTSConfig": ("modules.tts.engine", "TTSConfig"),
    "list_voices": ("modules.tts.engine", "list_voices"),
    "KOKORO_VOICES": ("modules.tts.engine", "KOKORO_VOICES"),
    "TextChunker": ("modules.tts.chunker", "TextChunker"),
    "ChunkConfig": ("modules.tts.chunker", "ChunkConfig"),
    "chunk_text": ("modules.tts.chunker", "chunk_text"),
    "VRAMManager": ("modules.tts.memory", "VRAMManager"),
    "TextCleaner": ("modules.tts.cleaner", "TextCleaner"),
    "CleanerConfig": ("modules.tts.cleaner_config", "CleanerConfig"),
    # Strategy Pattern
    "TTSStrategy": ("modules.tts.strategies", "TTSStrategy"),
    "TTSVoice": ("modules.tts.strategies", "TTSVoice"),
    "TTSCapabilities": ("modules.tts.strategies", "TTSCapabilities"),
    "StrategyConfig": ("modules.tts.strategies", "TTSConfig"),
    "KokoroTTSStrategy": ("modules.tts.strategies", "KokoroTTSStrategy"),
    "KokoroConfig": ("modules.tts.strategies", "KokoroConfig"),
    "TTSEngineFactory": ("modules.tts.factory", "TTSEngineFactory"),
    "create_tts_engine": ("modules.tts.factory", "create_tts_engine"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = _EXPORTS[name]
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value
