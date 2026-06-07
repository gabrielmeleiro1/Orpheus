"""TTS-only speech normalization."""

from __future__ import annotations

from dataclasses import dataclass
import re

_ONES = {
    0: "zero",
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
    11: "eleven",
    12: "twelve",
    13: "thirteen",
    14: "fourteen",
    15: "fifteen",
    16: "sixteen",
    17: "seventeen",
    18: "eighteen",
    19: "nineteen",
}
_TENS = {
    20: "twenty",
    30: "thirty",
    40: "forty",
    50: "fifty",
    60: "sixty",
    70: "seventy",
    80: "eighty",
    90: "ninety",
}
_ABBREVIATION_SUBSTITUTIONS = (
    (re.compile(r"\bDr\."), "Doctor"),
    (re.compile(r"\bMr\."), "Mister"),
    (re.compile(r"\bMrs\."), "Misses"),
    (re.compile(r"\bMs\."), "Miz"),
)
_PM_RE = re.compile(r"\bp\.m\.", re.IGNORECASE)
_AM_RE = re.compile(r"\ba\.m\.", re.IGNORECASE)
_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_NUMBER_RE = re.compile(r"\b\d{1,4}\b")


@dataclass(frozen=True)
class SpeechNormalizationConfig:
    enabled: bool = False


def _number_to_words(value: int) -> str:
    if value < 20:
        return _ONES[value]
    if value < 100:
        tens = value // 10 * 10
        remainder = value % 10
        return _TENS[tens] if remainder == 0 else f"{_TENS[tens]} {_ONES[remainder]}"
    if value < 1000:
        hundreds = value // 100
        remainder = value % 100
        base = f"{_ONES[hundreds]} hundred"
        return base if remainder == 0 else f"{base} {_number_to_words(remainder)}"
    if 1000 <= value <= 2099:
        first = value // 100
        last = value % 100
        return f"{_number_to_words(first)} {_number_to_words(last)}"
    return str(value)


def normalize_for_speech(text: str, config: SpeechNormalizationConfig | None = None) -> str:
    """Return TTS-only text with conservative speech expansions."""
    config = config or SpeechNormalizationConfig()
    if not config.enabled:
        return text

    spoken = text
    for pattern, replacement in _ABBREVIATION_SUBSTITUTIONS:
        spoken = pattern.sub(replacement, spoken)
    spoken = _PM_RE.sub("P.M.", spoken)
    spoken = _AM_RE.sub("A.M.", spoken)

    def replace_time(match: re.Match[str]) -> str:
        hour = int(match.group(1))
        minute = int(match.group(2))
        return f"{_number_to_words(hour)} {_number_to_words(minute)}"

    spoken = _TIME_RE.sub(replace_time, spoken)

    def replace_number(match: re.Match[str]) -> str:
        return _number_to_words(int(match.group(0)))

    spoken = _NUMBER_RE.sub(replace_number, spoken)
    return spoken
