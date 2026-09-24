"""Align cleaner output and retain only edits that preserve source content."""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .cleaner_rules import (
    _WORD_TOKEN_RE,
    _is_dropped_leading_letter_repair,
    _is_orphan_single_letter_delete,
    page_furniture_lines,
)


# Only dotted abbreviations qualify. Ambiguous St. has two standard readings;
# other substitutions (including changes to nearby names/numbers) remain edits.
_ABBREVIATIONS = {
    "fig": ("figure",),
    "figs": ("figures",),
    "p": ("page",),
    "pp": ("pages",),
    "dr": ("doctor",),
    "mr": ("mister",),
    "mrs": ("missus",),
    "st": ("saint", "street"),
    "prof": ("professor",),
    "rev": ("reverend",),
    "jr": ("junior",),
    "sr": ("senior",),
}
_MAX_ALIGNMENT_CELLS = 250_000


@dataclass(frozen=True)
class _Token:
    word: str
    start: int
    end: int
    expansions: tuple[str, ...] = ()
    furniture: bool = False


@dataclass(frozen=True)
class _Edit:
    source_start: int
    source_end: int
    output_start: int
    output_end: int
    allowed: bool


@dataclass
class GuardedEdits:
    text: str
    accepted_edits: int = 0
    rejected_edits: list[dict[str, object]] = field(default_factory=list)
    alignment_error: str | None = None


def _tokens(text: str) -> list[_Token]:
    furniture = page_furniture_lines(text)
    tokens = []
    offset = 0
    for index, line in enumerate(text.splitlines(keepends=True)):
        for match in _WORD_TOKEN_RE.finditer(line):
            word = match.group()
            end = match.end()
            expansions = ()
            if line[end:end + 1] == "." and not line[end + 1:end + 2].isalpha():
                expansions = _ABBREVIATIONS.get(word.casefold(), ())
                if expansions:
                    end += 1
            tokens.append(_Token(word, offset + match.start(), offset + end, expansions, index in furniture))
        offset += len(line)
    return tokens


def abbreviation_expansion_allowance(text: str) -> int:
    """Allow the length added by standard expansions in the length safeguard."""
    return sum(
        max(0, max(map(len, token.expansions)) - (token.end - token.start))
        for token in _tokens(text) if token.expansions
    )


def _boundary_repair_end(source: list[_Token], output: list[_Token], i: int, j: int) -> tuple[int, int] | None:
    """Find the smallest exact alphabetic merge/split starting at this pair."""
    left, right = source[i].word.casefold(), output[j].word.casefold()
    a, b = i + 1, j + 1
    # An ordinal suffix may be detached by OCR (450 [th] -> 450th). This
    # preserves the number; joining two numeric tokens (1 2 -> 12) does not.
    if left.isdigit() and a < len(source):
        suffix = source[a].word.casefold()
        if suffix in {"st", "nd", "rd", "th"} and right == left + suffix:
            return a + 1, b
    if not left.isalpha() or not right.isalpha() or source[i].word in {"A", "I"}:
        return None
    while left != right:
        if right.startswith(left) and a < len(source):
            token = source[a].word
            if not token.isalpha() or token in {"A", "I"}:
                return None
            left += token.casefold()
            a += 1
        elif left.startswith(right) and b < len(output):
            token = output[b].word
            if not token.isalpha():
                return None
            right += token.casefold()
            b += 1
        else:
            return None
    return (a, b) if a > i + 1 or b > j + 1 else None


def _align_block(source: list[_Token], output: list[_Token]) -> list[_Edit]:
    """Minimize rejected tokens first, then the number of permitted repairs.

    Unlike a plain sequence diff, this treats a many-to-one word join as one
    valid edit, even beside a changed name or another join with no shared word.
    """
    n, m = len(source), len(output)
    if (n + 1) * (m + 1) > _MAX_ALIGNMENT_CELLS:
        raise ValueError("LLM preservation guard could not align a large changed span")
    penalty = n + m + 1
    costs = [[float("inf")] * (m + 1) for _ in range(n + 1)]
    paths: dict[tuple[int, int], _Edit] = {}
    costs[0][0] = 0

    def offer(i: int, j: int, a: int, b: int, allowed: bool, cost: int) -> None:
        candidate = costs[i][j] + cost
        if candidate < costs[a][b]:
            costs[a][b] = candidate
            paths[a, b] = _Edit(i, a, j, b, allowed)

    for i in range(n + 1):
        for j in range(m + 1):
            if i < n and j < m:
                before, after = source[i].word, output[j].word
                if before.casefold() == after.casefold():
                    offer(i, j, i + 1, j + 1, True, 0)
                elif after.casefold() in source[i].expansions or (
                    before[:1].islower() and before.isalpha() and after.isalpha()
                    and _is_dropped_leading_letter_repair([before], [after])
                ):
                    offer(i, j, i + 1, j + 1, True, 1)
                else:
                    offer(i, j, i + 1, j + 1, False, penalty)
                boundary = _boundary_repair_end(source, output, i, j)
                if boundary:
                    offer(i, j, *boundary, True, 1)
            if i < n:
                allowed = source[i].furniture or _is_orphan_single_letter_delete([source[i].word], [])
                offer(i, j, i + 1, j, allowed, 1 if allowed else penalty)
            if j < m:
                offer(i, j, i, j + 1, False, penalty)

    edits = []
    i, j = n, m
    while i or j:
        edit = paths[i, j]
        edits.append(edit)
        i, j = edit.source_start, edit.output_start
    return list(reversed(edits))


def _align(source: list[_Token], output: list[_Token]) -> list[_Edit]:
    if [token.word.casefold() for token in source] == [token.word.casefold() for token in output]:
        return [_Edit(i, i + 1, i, i + 1, True) for i in range(len(source))]
    if (len(source) + 1) * (len(output) + 1) <= _MAX_ALIGNMENT_CELLS:
        return _align_block(source, output)
    # Long chapters usually have plentiful unchanged anchors. Bound the work
    # on changed blocks; an unalignable block causes whole-chunk fallback.
    matcher = SequenceMatcher(
        None,
        [token.word.casefold() for token in source],
        [token.word.casefold() for token in output],
        autojunk=False,
    )
    edits = []
    for tag, i, a, j, b in matcher.get_opcodes():
        if tag == "equal":
            edits.extend(_Edit(k, k + 1, j + k - i, j + k - i + 1, True) for k in range(i, a))
        else:
            edits.extend(
                _Edit(i + e.source_start, i + e.source_end, j + e.output_start, j + e.output_end, e.allowed)
                for e in _align_block(source[i:a], output[j:b])
            )
    return edits


def _span(text: str, tokens: list[_Token], start: int, end: int) -> tuple[int, int]:
    first = tokens[start].start if start < len(tokens) else len(text)
    return first, tokens[end - 1].end if end > start else first


def reconcile_llm_edits(source_text: str, output_text: str) -> GuardedEdits:
    """Keep allowed edits and restore rejected spans, including their separators.

    Rejection offsets are half-open character ranges in the rule-based source
    and the candidate output, so traces can identify precisely what was refused.
    Typography lives between word tokens and is permitted independently.
    """
    result = GuardedEdits(output_text)
    source, output = _tokens(source_text), _tokens(output_text)
    try:
        aligned = _align(source, output)
    except ValueError as exc:
        return GuardedEdits(source_text, alignment_error=str(exc))

    edits: list[_Edit] = []
    for edit in aligned:
        if not edit.allowed and edits and not edits[-1].allowed:
            previous = edits.pop()
            edit = _Edit(previous.source_start, edit.source_end, previous.output_start, edit.output_end, False)
        edits.append(edit)

    patches = []
    for edit in edits:
        i, a, j, b = edit.source_start, edit.source_end, edit.output_start, edit.output_end
        s0, s1 = _span(source_text, source, i, a)
        o0, o1 = _span(output_text, output, j, b)
        before, after = source_text[s0:s1], output_text[o0:o1]
        if edit.allowed:
            if before != after:
                result.accepted_edits += 1
            continue
        tag = "insert" if i == a else "delete" if j == b else "replace"
        reason = f"LLM preservation guard rejected token {tag}: {before!r} -> {after!r}"
        result.rejected_edits.append({
            "source_start": s0, "source_end": s1, "llm_start": o0, "llm_end": o1,
            "before": before, "after": after, "reason": reason,
        })
        if a - i == b - j == 1:
            # A changed name/number needs only its token restored. Preserve
            # independently valid quote/dash normalization around that token.
            patches.append((o0, o1, before))
            continue
        # Include the surrounding gaps, so reverting an invented sentence also
        # removes its punctuation and restores any source sentence boundary.
        s0 = source[i - 1].end if i else 0
        s1 = source[a].start if a < len(source) else len(source_text)
        o0 = output[j - 1].end if j else 0
        o1 = output[b].start if b < len(output) else len(output_text)
        if patches and o0 < patches[-1][1]:
            return GuardedEdits(
                source_text,
                rejected_edits=result.rejected_edits,
                alignment_error="LLM preservation guard found overlapping edit boundaries",
            )
        patches.append((o0, o1, source_text[s0:s1]))

    for start, end, replacement in reversed(patches):
        result.text = result.text[:start] + replacement + result.text[end:]
    result.text = result.text.strip()
    if not result.rejected_edits and not result.accepted_edits and result.text != source_text:
        result.accepted_edits = 1  # Punctuation/whitespace-only normalization.
    return result
