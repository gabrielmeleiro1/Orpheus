"""
Audio Module
============
Audio processing, encoding, and M4B packaging.
"""

from __future__ import annotations

from importlib import import_module

_EXPORT_MAP = {
    "AudioProcessor": ("modules.audio.processor", "AudioProcessor"),
    "AudioEncoder": ("modules.audio.encoder", "AudioEncoder"),
    "M4BPackager": ("modules.audio.packager", "M4BPackager"),
    "DocumentAudioPackager": ("modules.audio.document_packager", "DocumentAudioPackager"),
    "AudiobookMetadata": ("modules.audio.packager", "AudiobookMetadata"),
    "ChapterMarker": ("modules.audio.packager", "ChapterMarker"),
    "BufferPool": ("modules.audio.buffer_pool", "BufferPool"),
    "PooledBuffer": ("modules.audio.buffer_pool", "PooledBuffer"),
    "get_buffer_pool": ("modules.audio.buffer_pool", "get_buffer_pool"),
    "BufferStats": ("modules.audio.buffer_pool", "BufferStats"),
}

try:
    import_module("modules.audio.buffer_pool")
    BUFFER_POOL_AVAILABLE = True
except ImportError:
    BUFFER_POOL_AVAILABLE = False

__all__ = [
    "AudioProcessor",
    "AudioEncoder",
    "M4BPackager",
    "DocumentAudioPackager",
    "AudiobookMetadata",
    "ChapterMarker",
]

if BUFFER_POOL_AVAILABLE:
    __all__.extend([
        "BufferPool",
        "PooledBuffer",
        "get_buffer_pool",
        "BufferStats",
    ])


def __getattr__(name: str):
    if name == "BUFFER_POOL_AVAILABLE":
        return BUFFER_POOL_AVAILABLE

    try:
        module_name, attr_name = _EXPORT_MAP[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    module = import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
