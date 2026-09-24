"""Rule-based text cleaner helpers and hallucination guard primitives."""

from __future__ import annotations

from collections import Counter
import re

from modules.ingestion.extraction_artifact_signals import (
    has_joined_first_person_leakage,
    has_pdf_hyphenation_leakage,
    has_pdf_joined_dash_leakage,
    has_pdf_page_footnote_leakage,
)

_WORD_TOKEN_RE = re.compile(r"[A-Za-z0-9À-ÖØ-öø-ÿ]+")
_DROP_CAP_BREAK = r"(?:\s*(?:<br\s*/?>|\n)\s*)"
_REVIEW_HASH_DROP_CAP_RE = re.compile(
    rf"(?im)(^|\n\s*)#\s*{_DROP_CAP_BREAK}"
    rf"(?P<letter>[D]){_DROP_CAP_BREAK}(?P<rest>eejay)\b"
)
_REVIEW_DROP_CAP_RE = re.compile(
    rf"(?im)(^|\n\s*)(?P<letter>[TW]){_DROP_CAP_BREAK}"
    rf"(?P<rest>he(?:re(?:['’]s)?)?|hen|hat)\b"
)
_CLEAR_SPACED_HYPHEN_COMPOUND_RE = re.compile(
    r"\b(?P<left>card|self|promise|recipient|well)-\s+"
    r"(?P<right>being|governed|making|keeping|cum|sharper)\b",
    re.IGNORECASE,
)
_KNOWN_CLOSED_COMPOUND_HYPHEN_RE = re.compile(
    r"\b(?P<left>country)-\s+(?P<right>woman|women|man|men)\b",
    re.IGNORECASE,
)
def strip_inline_numeric_references(text: str) -> str:
    """Remove bracketed numeric citation markers like [3], [10], [245]."""
    return re.sub(r"\s*\[\d{1,4}\]\s*", " ", text)


def strip_trailing_numeric_source_note(text: str) -> str:
    """Remove simple terminal notes like `1. Dublin.` from book-chapter text."""
    return re.sub(
        r"(?s)(?<=[.!?])\s+\d{1,3}\.\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.\- ]{1,80}\.?$",
        "",
        text,
    )


def repair_split_uppercase_word_fragments(text: str) -> str:
    """
    Repair OCR fragments like `C AVALRYMEN` -> `CAVALRYMEN`.

    Restrict to long uppercase second token to avoid over-merging valid constructs
    such as `A NEW`.
    """

    def _merge(match: re.Match[str]) -> str:
        return f"{match.group(1)}{match.group(2)}"

    return re.sub(r"\b([B-HJ-Z])\s+([A-Z]{5,})\b", _merge, text)


def repair_spaced_caps_em_dash_attributions(text: str) -> str:
    """
    Repair OCR-style spaced uppercase name fragments following an em dash.

    Example:
        —C ORMAC M C C ARTHY  ->  —Cormac McCarthy
    """

    def _format_name_word(word: str) -> str:
        upper = word.upper()
        if upper.startswith("MC") and len(upper) > 2:
            return "Mc" + upper[2:3] + upper[3:].lower()
        if len(upper) <= 3:
            return upper
        return upper.capitalize()

    def _collapse_spaced_caps(raw_name: str) -> str:
        tokens = [token for token in raw_name.split() if token]
        if len(tokens) < 2:
            return raw_name.strip()
        if not any(len(token) == 1 for token in tokens):
            return raw_name.strip()
        if not any(len(token) > 1 for token in tokens):
            return raw_name.strip()

        collapsed: list[str] = []
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if len(token) == 1:
                j = i
                while j < len(tokens) and len(tokens[j]) == 1:
                    j += 1
                if j < len(tokens) and len(tokens[j]) > 1:
                    collapsed.append("".join(tokens[i:j]) + tokens[j])
                    i = j + 1
                    continue
            collapsed.append(token)
            i += 1

        if collapsed == tokens:
            return raw_name.strip()

        return " ".join(_format_name_word(word) for word in collapsed)

    def _replace(match: re.Match[str]) -> str:
        raw_name = match.group(1)
        collapsed = _collapse_spaced_caps(raw_name)
        if collapsed == raw_name.strip():
            return match.group(0)
        return f"—{collapsed}"

    return re.sub(r"—\s*([A-Z][A-Z \t]*[A-Z])", _replace, text)


def repair_separated_drop_cap_fragments(text: str) -> str:
    """Repair reviewed dropped-cap fragments with explicit line-break evidence."""

    def _join(match: re.Match[str]) -> str:
        return f"{match.group(1)}{match.group('letter').upper()}{match.group('rest')}"

    repaired = _REVIEW_HASH_DROP_CAP_RE.sub(_join, text)
    return _REVIEW_DROP_CAP_RE.sub(_join, repaired)


def repair_known_leading_letter_fragments(text: str) -> str:
    """Repair recurring dropped-leading-letter artifacts that LLMs can miss."""
    repaired = text
    repaired = repair_separated_drop_cap_fragments(repaired)
    repaired = strip_inline_numeric_references(repaired)
    repaired = repair_split_uppercase_word_fragments(repaired)
    repaired = re.sub(r"\bhe time\b", "the time", repaired)
    repaired = re.sub(r"\bHe time\b", "The time", repaired)
    repaired = repair_spaced_caps_em_dash_attributions(repaired)
    return repaired


def repair_clear_spaced_hyphenated_compounds(text: str) -> str:
    """Repair clear hyphenated compounds left with a space after the hyphen."""

    def _join(match: re.Match[str]) -> str:
        return f"{match.group('left')}-{match.group('right')}"

    return _CLEAR_SPACED_HYPHEN_COMPOUND_RE.sub(_join, text)


def repair_known_closed_compound_hyphenation(text: str) -> str:
    """Repair reviewed PDF line-break artifacts in closed compound words."""

    def _join(match: re.Match[str]) -> str:
        left = match.group("left")
        repaired = f"{left}{match.group('right')}"
        if left[:1].isupper():
            return repaired[:1].upper() + repaired[1:]
        return repaired

    return _KNOWN_CLOSED_COMPOUND_HYPHEN_RE.sub(_join, text)


def is_markdown_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.count("|") >= 2


def parse_markdown_table_row(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    cells = [cell.strip() for cell in stripped.split("|")]
    return [re.sub(r"\s+", " ", cell).strip() for cell in cells]


def is_markdown_separator_row(cells: list[str]) -> bool:
    meaningful = [cell for cell in cells if cell]
    if not meaningful:
        return False
    return all(re.fullmatch(r"[:\- ]+", cell) is not None for cell in meaningful)


def markdown_table_block_to_text(block: list[str]) -> str:
    rows = [parse_markdown_table_row(line) for line in block]
    if len(rows) < 2:
        return "\n".join(block)

    headers = rows[0]
    data_rows = [row for row in rows[1:] if not is_markdown_separator_row(row)]
    if not data_rows:
        return "\n".join(block)

    sentences: list[str] = []
    for row in data_rows:
        parts: list[str] = []
        for idx, cell in enumerate(row):
            if not cell:
                continue
            header = headers[idx] if idx < len(headers) else f"Column {idx + 1}"
            if header:
                parts.append(f"{header}: {cell}")
            else:
                parts.append(cell)

        if parts:
            sentence = "; ".join(parts).strip()
            if sentence and sentence[-1] not in ".!?":
                sentence += "."
            sentences.append(sentence)

    return "\n".join(sentences)


def normalize_markdown_tables(text: str) -> str:
    """Convert markdown table blocks into plain sentences."""
    text = re.sub(r"\|\s+\|", "|\n|", text)

    lines = text.splitlines()
    out: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        if is_markdown_table_line(line):
            block: list[str] = []
            while i < len(lines) and is_markdown_table_line(lines[i]):
                block.append(lines[i])
                i += 1
            out.append(markdown_table_block_to_text(block))
            continue

        out.append(line)
        i += 1

    return "\n".join(out)


def page_furniture_lines(text: str) -> set[int]:
    """Identify page numbers and short, title-like running headers/footers.

    Repetition alone is not enough for prose or dialogue: candidates must look
    like titles, without sentence punctuation or list/Markdown markers.
    """
    furniture: set[int] = set()
    candidates: dict[int, str] = {}
    for index, line in enumerate(text.splitlines()):
        line = line.strip()
        if re.fullmatch(r"\d+", line):
            furniture.add(index)
            continue
        if not line or line.startswith("#") or len(line) > 100:
            continue
        title = re.sub(r"^\d+\s+|\s+\d+$", "", line).strip()
        if (
            len(title.split()) > 12
            or not re.fullmatch(r"[^\W\d_][\w\s'’–—-]*", title)
            or not (title.isupper() or title.istitle())
            or (len(title.split()) == 1 and (not title.isupper() or len(title) < 4))
        ):
            continue
        key = re.sub(r"[\d\s]+", "", title).casefold()
        candidates[index] = key
        if title.isupper() and title != line:
            furniture.add(index)
    counts = Counter(candidates.values())
    furniture.update(index for index, key in candidates.items() if counts[key] >= 2)
    return furniture


def strip_page_furniture(text: str) -> str:
    furniture = page_furniture_lines(text)
    return "".join(
        line for index, line in enumerate(text.splitlines(keepends=True))
        if index not in furniture
    )


def repair_line_break_hyphenation(text: str) -> str:
    """Join word fragments only across an explicit line break and ASCII hyphen."""
    return re.sub(r"([^\W\d_])-[ \t]*\r?\n[ \t]*(?=[a-zà-öø-ÿ])", r"\1", text)


def rule_based_clean(text: str) -> str:
    """Apply fast rule-based text normalization for TTS preprocessing."""
    text = strip_page_furniture(text)
    text = repair_line_break_hyphenation(text)
    text = repair_separated_drop_cap_fragments(text)
    text = re.sub(r"(?i)<br\s*/?>", " ", text)
    text = strip_inline_numeric_references(text)
    text = normalize_markdown_tables(text)

    text = re.sub(r"(?m)^#{1,6}\s+", "", text)
    text = re.sub(r"[\*_]{1,3}([^\*_]+)[\*_]{1,3}", r"\1", text)
    text = text.replace("**", "")
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    text = re.sub(r"!\[([^\]]*)\]\([^\)]+\)", r"\1", text)
    text = re.sub(r"`", "", text)
    text = re.sub(r"\|+", " ", text)
    text = re.sub(r"(?m)\s*---+\s*", " ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\n\s*\n", "\n\n", text)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    text = re.sub(r"([,.!?;:])([A-Za-z])", r"\1 \2", text)
    text = re.sub(r"\be\.\s+g\.", "e.g.", text, flags=re.IGNORECASE)
    text = re.sub(r"\bi\.\s+e\.", "i.e.", text, flags=re.IGNORECASE)
    text = strip_trailing_numeric_source_note(text)
    text = repair_split_uppercase_word_fragments(text)
    text = repair_spaced_caps_em_dash_attributions(text)
    text = repair_clear_spaced_hyphenated_compounds(text)
    text = repair_known_closed_compound_hyphenation(text)
    return text.strip()


def _is_pdf_source_type(source_type: str | None) -> bool:
    if source_type is None:
        return True
    return str(source_type).strip().lower().lstrip(".") == "pdf"


def needs_llm_cleaning(text: str, *, source_type: str | None = None) -> bool:
    """Heuristic gate for when LLM cleanup should be attempted.

    ``source_type`` scopes PDF extraction-artifact triggers. Unknown source
    type keeps legacy behavior; known EPUB/TXT/MD input is authoritative for
    hyphenation and PDF page residue, so those triggers are skipped.
    """
    normalized = text.strip()
    if not normalized:
        return False

    if not re.search(r"[A-Za-z0-9]", normalized):
        return False

    if re.search(r"(?<!^)\s#\s*[A-Za-z]\b", normalized):
        return True

    if _is_pdf_source_type(source_type):
        if has_pdf_hyphenation_leakage(normalized):
            return True

        if has_pdf_page_footnote_leakage(normalized):
            return True

        if has_pdf_joined_dash_leakage(normalized):
            return True

    if has_joined_first_person_leakage(normalized):
        return True

    return False


def describe_llm_cleaning_reason(text: str, *, source_type: str | None = None) -> str:
    """Return a concise reason for sending this chunk to the LLM cleaner."""
    normalized = text.strip()
    if not normalized:
        return "empty text"

    if re.search(r"(?<!^)\s#\s*[A-Za-z]\b", normalized):
        return "mid-sentence markdown heading marker"

    if _is_pdf_source_type(source_type):
        if has_pdf_hyphenation_leakage(normalized):
            return "possible PDF line-break hyphenation"

        if has_pdf_page_footnote_leakage(normalized):
            return "possible PDF page or footnote residue"

        if has_pdf_joined_dash_leakage(normalized):
            return "possible joined PDF dash line-break artifact"

    if has_joined_first_person_leakage(normalized):
        return "possible joined first-person word extraction artifact"

    return "default conservative cleanup pass"


def _is_dropped_leading_letter_repair(before: list[str], after: list[str]) -> bool:
    if len(before) != 1 or len(after) != 1:
        return False
    source = before[0]
    candidate = after[0]
    if len(candidate) != len(source) + 1:
        return False
    if candidate[:1].upper() in {"A", "I"}:
        return False
    return candidate.casefold().endswith(source.casefold())


def _is_orphan_single_letter_delete(before: list[str], after: list[str]) -> bool:
    if after or len(before) != 1:
        return False
    token = before[0]
    if len(token) != 1:
        return False
    if token in {"A", "I"}:
        return False
    return token.isalpha() and token.upper() == token


def llm_preservation_violation(source_text: str, cleaned_text: str) -> str | None:
    """Return the first rejected edit without applying partial acceptance."""
    from .cleaner_edits import reconcile_llm_edits

    result = reconcile_llm_edits(source_text, cleaned_text)
    if result.alignment_error:
        return result.alignment_error
    if result.rejected_edits:
        return str(result.rejected_edits[0]["reason"])
    return None


def extract_result(response: str) -> str:
    """Extract cleaned text payload from model output."""
    result = response.strip()
    result = re.sub(r"(?m)^\s*---+\s*$", "", result).strip()
    result = re.sub(r"---+\s*$", "", result).strip()
    result = re.sub(r"^\s*---+(?:\s|$)", "", result).strip()
    return result
