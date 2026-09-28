"""Align cleaner output and retain only edits that preserve source content."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
import re

from modules.ingestion.extraction_artifact_signals import JOINED_FIRST_PERSON_LEAK_RE, PDF_JOINED_DASH_LEAK_RE
from modules.ingestion.text_layout import (
    hyphenation_word_forms,
    page_furniture_lines,
    should_join_hyphenated_word,
)
from .cleaner_rules import _WORD_TOKEN_RE
from .cleaner_evidence import COMMON_WORDS, has_fragment_evidence


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
    separator: str = ""
    marked: bool = False
    hyphen_join: bool = False
    drop_cap: bool = False
    compound: bool = False
    sentence_start: bool = False


@dataclass(frozen=True)
class _Evidence:
    words: Counter[str]
    forms: set[str]
    capitals: frozenset[str]


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
    word_forms = hyphenation_word_forms(text)
    tokens = []
    offset = 0
    for index, line in enumerate(text.splitlines(keepends=True)):
        matches = list(_WORD_TOKEN_RE.finditer(line))
        for position, match in enumerate(matches):
            word = match.group()
            end = match.end()
            expansions = ()
            if line[end:end + 1] == "." and not line[end + 1:end + 2].isalpha():
                expansions = _ABBREVIATIONS.get(word.casefold(), ())
                if expansions:
                    end += 1
            next_start = matches[position + 1].start() if position + 1 < len(matches) else len(line)
            previous_end = matches[position - 1].end() if position else 0
            gap = line[previous_end:match.start()]
            marked = bool(re.search(r"[#|]+\s*$", gap))
            heading_start = position
            while heading_start and len(matches[heading_start - 1].group()) > 1 and matches[heading_start - 1].group().isupper():
                if heading_start < position and not line[matches[heading_start - 1].end():matches[heading_start].start()].isspace():
                    break
                heading_start -= 1
            sentence_start = (
                position == 0 or bool(re.search(r"[.!?:][\"'’”)]*\s*[#|\s]*$", gap))
                or position - heading_start >= 2 or (heading_start == 0 and position == 1)
            )
            drop_cap = len(word) == 1 and word.isupper() and (
                marked or line.strip() == word
            )
            compound = (
                match.start() >= 2 and line[match.start() - 1] == "-" and line[match.start() - 2].isalpha()
            ) or (line[end:end + 1] == "-" and line[end + 1:end + 2].isalpha())
            tokens.append(_Token(
                word, offset + match.start(), offset + end, expansions, index in furniture,
                line[end:next_start], marked, drop_cap=drop_cap, compound=compound,
                sentence_start=sentence_start,
            ))
        offset += len(line)
    return [
        replace(token, hyphen_join=True)
        if index + 1 < len(tokens) and re.fullmatch(r"-\s+", token.separator)
        and should_join_hyphenated_word(token.word, tokens[index + 1].word, word_forms)
        else token
        for index, token in enumerate(tokens)
    ]


def abbreviation_expansion_allowance(text: str) -> int:
    """Allow the length added by standard expansions in the length safeguard."""
    return sum(
        max(0, max(map(len, token.expansions)) - (token.end - token.start))
        for token in _tokens(text) if token.expansions
    )


def _boundary_repair_end(
    source: list[_Token], output: list[_Token], i: int, j: int, evidence: _Evidence,
) -> tuple[int, int] | None:
    """Find the smallest exact alphabetic merge/split starting at this pair."""
    left, right = source[i].word.casefold(), output[j].word.casefold()
    a, b = i + 1, j + 1
    # An ordinal suffix may be detached by OCR (450 [th] -> 450th). This
    # preserves the number; joining two numeric tokens (1 2 -> 12) does not.
    if left.isdigit() and a < len(source):
        suffix = source[a].word.casefold()
        if suffix in {"st", "nd", "rd", "th"} and right == left + suffix:
            return a + 1, b
    if not left.isalpha() or not right.isalpha():
        return None
    while left != right:
        if right.startswith(left) and a < len(source):
            token = source[a].word
            if not token.isalpha():
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
    if a == i + 1 and b > j + 1:
        if JOINED_FIRST_PERSON_LEAK_RE.fullmatch(source[i].word) and output[j].word == "I":
            return a, b
        if PDF_JOINED_DASH_LEAK_RE.fullmatch(source[i].word):
            return a, b
        if evidence.words[left] == 1 and all(
            evidence.words[token.word.casefold()] for token in output[j:b]
        ):
            return a, b
        # A fused connector inside an existing compound has hyphen evidence.
        parts = [token.word.casefold() for token in output[j:b]]
        if source[i].compound and all(token.separator == "-" for token in output[j:b - 1]):
            if any(word in COMMON_WORDS for word in parts) and not all(word in COMMON_WORDS for word in parts):
                return a, b
    if a > i + 1 and b == j + 1:
        fragments = [token.word.casefold() for token in source[i:a]]
        separators = [token.separator for token in source[i:a - 1]]
        if not all(re.fullmatch(r"[\s#|*_-]+", gap) for gap in separators):
            return None
        if any("-" in token.separator and not token.hyphen_join for token in source[i:a - 1]):
            return None
        if left in evidence.forms:
            return a, b
        if all(token.hyphen_join for token in source[i:a - 1]):
            return a, b
        if all(word in COMMON_WORDS for word in fragments):
            return None
        if (
            any(has_fragment_evidence(first, second) for first, second in zip(fragments, fragments[1:]))
            or any(re.search(r"[#|*_\-]", gap) for gap in separators)
        ):
            return a, b
    return None


def _leading_restoration(source: _Token, output: _Token, evidence: _Evidence) -> bool:
    before, after = source.word.casefold(), output.word.casefold()
    if len(before) < 2 or not before.isalpha() or not after.isalpha() or len(after) != len(before) + 1 or after[1:] != before:
        return False
    displaced = after[:1].upper() in evidence.capitals
    return (
        source.sentence_start and (source.marked or displaced)
        or source.word.islower() and before not in COMMON_WORDS
    )


def _align_block(source: list[_Token], output: list[_Token], evidence: _Evidence) -> list[_Edit]:
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
                elif after.casefold() in source[i].expansions or _leading_restoration(source[i], output[j], evidence):
                    offer(i, j, i + 1, j + 1, True, 1)
                else:
                    offer(i, j, i + 1, j + 1, False, penalty)
                boundary = _boundary_repair_end(source, output, i, j, evidence)
                if boundary:
                    offer(i, j, *boundary, True, 1)
            if i < n:
                allowed = source[i].furniture or source[i].drop_cap
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


def _align(source: list[_Token], output: list[_Token], evidence: _Evidence) -> list[_Edit]:
    if [token.word.casefold() for token in source] == [token.word.casefold() for token in output]:
        return [_Edit(i, i + 1, i, i + 1, True) for i in range(len(source))]
    if (len(source) + 1) * (len(output) + 1) <= _MAX_ALIGNMENT_CELLS:
        return _align_block(source, output, evidence)
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
                for e in _align_block(source[i:a], output[j:b], evidence)
            )
    return edits


def _span(text: str, tokens: list[_Token], start: int, end: int) -> tuple[int, int]:
    first = tokens[start].start if start < len(tokens) else len(text)
    return first, tokens[end - 1].end if end > start else first


def repair_source_backed_fragments(source_text: str, output_text: str) -> str:
    """Restore a lost source initial or an unambiguous displaced drop cap."""
    source, output = _tokens(source_text), _tokens(output_text)
    matcher = SequenceMatcher(
        None, [token.word.casefold() for token in source],
        [token.word.casefold() for token in output], autojunk=False,
    )
    patches = []
    fragments = []
    missing_caps = Counter(token.word for token in source if token.drop_cap)
    missing_caps.subtract(token.word for token in output if len(token.word) == 1)
    for tag, i, a, j, b in matcher.get_opcodes():
        if tag == "equal":
            fragments.extend(
                output[j + k - i] for k in range(i, a)
                if source[k].marked and source[k].sentence_start and len(source[k].word) > 1
            )
        elif tag == "replace" and a - i == b - j == 1:
            before, after = source[i].word, output[j].word
            if (
                before.isalpha() and after.isalpha() and len(after) > 1
                and len(before) == len(after) + 1 and before[1:].casefold() == after.casefold()
            ):
                patches.append((output[j].start, output[j].end, before))
    letters = [letter for letter, count in missing_caps.items() if count > 0]
    if len(letters) == len(fragments) == 1:
        token = fragments[0]
        letter = letters[0] if token.word[:1].isupper() else letters[0].lower()
        patches.append((token.start, token.end, letter + token.word))
    for start, end, replacement in sorted(patches, reverse=True):
        output_text = output_text[:start] + replacement + output_text[end:]
    return output_text


def reconcile_llm_edits(source_text: str, output_text: str) -> GuardedEdits:
    """Keep allowed edits and restore rejected spans, including their separators.

    Rejection offsets are half-open character ranges in the rule-based source
    and the candidate output, so traces can identify precisely what was refused.
    Typography lives between word tokens and is permitted independently.
    """
    result = GuardedEdits(output_text)
    source, output = _tokens(source_text), _tokens(output_text)
    evidence = _Evidence(
        Counter(token.word.casefold() for token in source),
        hyphenation_word_forms(source_text),
        frozenset(token.word for token in source if token.drop_cap),
    )
    try:
        aligned = _align(source, output, evidence)
    except ValueError as exc:
        return GuardedEdits(source_text, alignment_error=str(exc))

    # Each restored initial authorizes removal of at most one displaced drop cap.
    restored_letters = Counter(
        output[edit.output_start].word[:1].upper()
        for edit in aligned
        if edit.allowed and edit.source_end - edit.source_start == edit.output_end - edit.output_start == 1
        and _leading_restoration(source[edit.source_start], output[edit.output_start], evidence)
    )
    for index, edit in enumerate(aligned):
        if edit.allowed and edit.source_end - edit.source_start == 1 and edit.output_start == edit.output_end:
            token = source[edit.source_start]
            if not token.furniture:
                if restored_letters[token.word]:
                    restored_letters[token.word] -= 1
                else:
                    aligned[index] = replace(edit, allowed=False)

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
        if a - i == b - j == 1 and (before.endswith(".") or after.endswith(".")):
            if source_text[s1:s1 + 1] == ".":
                s1 += 1
            if output_text[o1:o1 + 1] == ".":
                o1 += 1
            before, after = source_text[s0:s1], output_text[o0:o1]
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

    # Separators have their own edits, including when a nearby word is rejected.
    boundaries = [(0, 0)] + [(e.source_end, e.output_end) for e in edits]
    for index, (a, b) in enumerate(boundaries):
        previous = edits[index - 1] if index else None
        following = edits[index] if index < len(edits) else None
        if any(
            edit is not None and not edit.allowed
            and not (edit.source_end - edit.source_start == edit.output_end - edit.output_start == 1)
            for edit in (previous, following)
        ):
            continue
        i = following.source_start if following else len(source)
        j = following.output_start if following else len(output)
        s0 = source[a - 1].end if a else 0
        o0 = output[b - 1].end if b else 0
        s1 = source[i].start if i < len(source) else len(source_text)
        o1 = output[j].start if j < len(output) else len(output_text)
        if any(start < o1 and end > o0 for start, end, _ in patches):
            continue
        before, after = source_text[s0:s1], output_text[o0:o1]
        if before == after:
            continue
        left_numeric = a > 0 and source[a - 1].word.isdigit()
        right_numeric = i < len(source) and source[i].word.isdigit()

        def numeric_marks(gap: str) -> str:
            if left_numeric and right_numeric:
                return re.sub(r"\s+", "", gap)
            if right_numeric:
                match = re.search(r"[+\-−.,]+\s*$", gap)
                return match.group().strip() if match else ""
            if left_numeric:
                return "".join(re.findall(r"[%‰°]", gap))
            return ""
        if numeric_marks(before) != numeric_marks(after):
            reason = f"LLM preservation guard rejected numeric punctuation: {before!r} -> {after!r}"
            result.rejected_edits.append({
                "source_start": s0, "source_end": s1, "llm_start": o0, "llm_end": o1,
                "before": before, "after": after, "reason": reason,
            })
            patches.append((o0, o1, before))
        else:
            result.accepted_edits += 1

    for start, end, replacement in sorted(patches, reverse=True):
        result.text = result.text[:start] + replacement + result.text[end:]
    result.text = result.text.strip()
    return result
