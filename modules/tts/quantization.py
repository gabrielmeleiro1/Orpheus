"""Canonical TTS quantization values and alias normalization."""

from __future__ import annotations

from typing import Optional

QUANTIZATION_ALIASES: dict[str, str] = {
    "q8": "8bit",
    "q4": "4bit",
    "full": "bf16",
}

CANONICAL_QUANTIZATIONS = frozenset({"bf16", "4bit", "6bit", "8bit"})

DEFAULT_QUANTIZATION = "bf16"


def normalize_quantization(value: Optional[str]) -> str:
    """Normalize UI/API quantization aliases to engine-supported values."""
    normalized = str(value or "").strip().lower()
    return QUANTIZATION_ALIASES.get(normalized, normalized or DEFAULT_QUANTIZATION)
