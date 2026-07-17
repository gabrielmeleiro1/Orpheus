"""Tests for canonical TTS quantization normalization."""

from __future__ import annotations

import pytest

from modules.tts.quantization import (
    DEFAULT_QUANTIZATION,
    normalize_quantization,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("q8", "8bit"),
        ("Q8", "8bit"),
        (" q8 ", "8bit"),
        ("q4", "4bit"),
        ("Q4", "4bit"),
        ("full", "bf16"),
        ("FULL", "bf16"),
        ("bf16", "bf16"),
        ("8bit", "8bit"),
        ("4bit", "4bit"),
        ("6bit", "6bit"),
        (None, DEFAULT_QUANTIZATION),
        ("", DEFAULT_QUANTIZATION),
        ("   ", DEFAULT_QUANTIZATION),
    ],
)
def test_normalize_quantization_aliases_and_defaults(value: str | None, expected: str) -> None:
    assert normalize_quantization(value) == expected
