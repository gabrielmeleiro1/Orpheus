"""
Audio Module
============
Audio encoding and M4B packaging.
"""

from __future__ import annotations

from importlib import import_module

_EXPORT_MAP = {
    "DocumentAudioPackager": ("modules.audio.document_packager", "DocumentAudioPackager"),
}

__all__ = [
    "DocumentAudioPackager",
]


def __getattr__(name: str):
    try:
        module_name, attr_name = _EXPORT_MAP[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    module = import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
