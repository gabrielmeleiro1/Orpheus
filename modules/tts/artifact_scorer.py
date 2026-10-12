"""Artifact scoring for selective local cleaner use."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re

from .cleaner_evidence import has_fragment_evidence


_WORD_PAIR_RE = re.compile(
    r"(?<![\w.'’`/\-])(?=([A-Za-z]+)[ \t]+([a-z]+)(?![\w'’`/\-]))"
)


_BACKTICK_RUN_RE = re.compile(r"`+")


def _strip_code_spans(text: str) -> str:
    """Blank out backtick code spans in linear time.

    A span opens at a backtick run and closes at the next run of the same
    length, as in Markdown. The backreference regex this replaces rescanned the
    rest of the text from every backtick, which is cubic on long backtick runs.
    """
    runs = [(match.start(), match.end()) for match in _BACKTICK_RUN_RE.finditer(text)]
    next_same_length: list[int | None] = [None] * len(runs)
    latest_by_length: dict[int, int] = {}
    for index in range(len(runs) - 1, -1, -1):
        length = runs[index][1] - runs[index][0]
        next_same_length[index] = latest_by_length.get(length)
        latest_by_length[length] = index
    pieces: list[str] = []
    position = 0
    index = 0
    while index < len(runs):
        closing = next_same_length[index]
        if closing is None:
            index += 1
            continue
        pieces.append(text[position:runs[index][0]])
        pieces.append(" ")
        position = runs[closing][1]
        index = closing + 1
    pieces.append(text[position:])
    return "".join(pieces)


def _count_split_words(text: str) -> int:
    """Count conservative English OCR splits, leaving ambiguous boundaries alone."""
    # Literal code may intentionally contain fragments as identifiers.
    prose = _strip_code_spans(text)
    count = 0
    for match in _WORD_PAIR_RE.finditer(prose):
        left, right = match.groups()
        prefix = prose[max(0, match.start() - 80):match.start()]
        if re.search(r"\b\d+(?:[.,]\d+)?\s*$", prefix):
            continue
        previous = re.search(r"(?<![A-Za-z])([A-Za-z]+)\s+$", prefix)
        if has_fragment_evidence(
            left, right, conservative=True, previous_word=previous.group(1) if previous else "",
        ):
            count += 1
    return count


class CleaningPath(str, Enum):
    """Recommended cleaner path for a text chunk."""

    RULES_ONLY = "rules_only"
    LIGHT_LLM = "light_llm"
    HEAVY_LLM = "heavy_llm"


@dataclass(frozen=True)
class ArtifactScore:
    total: float
    hyphenation_breaks: int = 0
    mojibake_count: int = 0
    short_line_ratio: float = 0.0
    weird_spacing_count: int = 0
    split_word_count: int = 0
    replacement_char_count: int = 0
    suspicious_digit_letter_mix: int = 0
    broken_quote_balance: bool = False
    broken_parentheses_balance: bool = False
    reason_codes: list[str] = field(default_factory=list)

    @property
    def cleaning_path(self) -> CleaningPath:
        if self.total < 0.20:
            return CleaningPath.RULES_ONLY
        if self.total < 0.55:
            return CleaningPath.LIGHT_LLM
        return CleaningPath.HEAVY_LLM

    def as_trace_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "hyphenation_breaks": self.hyphenation_breaks,
            "mojibake_count": self.mojibake_count,
            "short_line_ratio": self.short_line_ratio,
            "weird_spacing_count": self.weird_spacing_count,
            "split_word_count": self.split_word_count,
            "replacement_char_count": self.replacement_char_count,
            "suspicious_digit_letter_mix": self.suspicious_digit_letter_mix,
            "broken_quote_balance": self.broken_quote_balance,
            "broken_parentheses_balance": self.broken_parentheses_balance,
            "reason_codes": list(self.reason_codes),
            "cleaning_path": self.cleaning_path.value,
        }


def score_text_artifacts(text: str) -> ArtifactScore:
    """Score extraction artifacts likely to benefit from local LLM cleanup."""
    normalized = text or ""
    hyphenation_breaks = len(re.findall(r"[a-z]-\s*(?:\n|\s{1,3})\s*[a-z]{2,}", normalized))
    replacement_char_count = normalized.count("\ufffd")
    mojibake_count = len(re.findall(r"(?:Ã.|Â.|â[€\x80-\xbf])", normalized))
    weird_spacing_count = len(re.findall(r"[ \t]{3,}|[A-Za-z]\s{2,}[A-Za-z]", normalized))
    split_word_count = _count_split_words(normalized)
    suspicious_digit_letter_mix = len(re.findall(r"\b(?:\d+[A-Za-z]|[A-Za-z]+\d+[A-Za-z]*)\b", normalized))

    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    short_lines = [line for line in lines if len(line) <= 2 or re.fullmatch(r"[A-Za-z]", line)]
    short_line_ratio = len(short_lines) / len(lines) if lines else 0.0

    broken_quote_balance = normalized.count('"') % 2 == 1
    broken_parentheses_balance = normalized.count("(") != normalized.count(")")

    score = 0.0
    reasons: list[str] = []

    def add(condition: bool, amount: float, reason: str) -> None:
        nonlocal score
        if condition:
            score += amount
            reasons.append(reason)

    add(hyphenation_breaks > 0, min(0.35, hyphenation_breaks * 0.18), "hyphenation_breaks")
    add(replacement_char_count > 0, min(0.30, replacement_char_count * 0.20), "replacement_chars")
    add(mojibake_count > 0, min(0.35, mojibake_count * 0.20), "mojibake")
    add(short_line_ratio >= 0.25, min(0.30, short_line_ratio * 0.40), "excessive_short_lines")
    add(weird_spacing_count > 0, min(0.20, weird_spacing_count * 0.08), "weird_spacing")
    add(split_word_count > 0, min(0.50, split_word_count * 0.25), "split_words")
    add(suspicious_digit_letter_mix > 0, min(0.20, suspicious_digit_letter_mix * 0.10), "digit_letter_mix")
    add(broken_quote_balance, 0.10, "broken_quote_balance")
    add(broken_parentheses_balance, 0.10, "broken_parentheses_balance")

    if not reasons:
        reasons.append("clean")

    return ArtifactScore(
        total=round(min(score, 1.0), 3),
        hyphenation_breaks=hyphenation_breaks,
        mojibake_count=mojibake_count,
        short_line_ratio=round(short_line_ratio, 3),
        weird_spacing_count=weird_spacing_count,
        split_word_count=split_word_count,
        replacement_char_count=replacement_char_count,
        suspicious_digit_letter_mix=suspicious_digit_letter_mix,
        broken_quote_balance=broken_quote_balance,
        broken_parentheses_balance=broken_parentheses_balance,
        reason_codes=reasons,
    )
