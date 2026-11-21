"""Rule-based text cleaner helpers and hallucination guard primitives."""

from __future__ import annotations

import re

from .cleaner_evidence import COMMON_WORDS

from modules.ingestion.text_layout import (
    hyphenation_word_forms,
    repair_line_break_hyphenation,
    should_join_hyphenated_word,
    strip_page_furniture,
)
from modules.ingestion.extraction_artifact_signals import (
    has_joined_first_person_leakage,
    has_pdf_hyphenation_leakage,
    has_pdf_joined_dash_leakage,
    has_pdf_page_footnote_leakage,
)

_WORD_TOKEN_RE = re.compile(r"[A-Za-z0-9À-ÖØ-öø-ÿ]+")
_DROP_CAP_BREAK = r"(?:\s*(?:<br\s*/?>|\n)\s*)"
# The line prefix is re-emitted verbatim, so starting it at the last newline
# (horizontal whitespace only) yields the same output as spanning blank lines,
# without rescanning a run of blank lines from each of its newlines.
_HASH_DROP_CAP_RE = re.compile(
    rf"(?im)(^|\n[^\S\n]*)#{_DROP_CAP_BREAK}"
    rf"(?P<letter>[B-HJ-NP-Z]){_DROP_CAP_BREAK}(?P<rest>[a-z]{{2,}})\b"
)
_DROP_CAP_RE = re.compile(
    rf"(?m)(^|\n[^\S\n]*)(?P<letter>[B-HJ-NP-Z]){_DROP_CAP_BREAK}"
    rf"(?P<rest>[a-z]{{2,}})\b"
)
_CLEAR_SPACED_HYPHEN_COMPOUND_RE = re.compile(
    r"\b(?P<left>card|self|promise|recipient|well)-\s+"
    r"(?P<right>being|control|governed|making|keeping|cum|sharper)\b",
    re.IGNORECASE,
)
_KNOWN_CLOSED_COMPOUND_HYPHEN_RE = re.compile(
    r"\b(?P<left>country)-\s+(?P<right>woman|women|man|men)\b",
    re.IGNORECASE,
)
def strip_inline_numeric_references(text: str) -> str:
    """Remove bracketed numeric citation markers like [3], [10], [245]."""
    # Leading whitespace is taken only from the start of its run, so a long
    # whitespace run is not rescanned from each of its characters.
    return re.sub(r"(?:(?<!\s)\s+)?\[\d{1,4}\]\s*", " ", text)


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
    """Repair separated drop caps with explicit line-break evidence."""

    def _join(match: re.Match[str]) -> str:
        return f"{match.group(1)}{match.group('letter').upper()}{match.group('rest')}"

    repaired = _HASH_DROP_CAP_RE.sub(_join, text)
    return _DROP_CAP_RE.sub(_join, repaired)


def repair_known_leading_letter_fragments(text: str) -> str:
    """Repair recurring dropped-leading-letter artifacts that LLMs can miss."""
    repaired = text
    repaired = repair_separated_drop_cap_fragments(repaired)
    repaired = strip_inline_numeric_references(repaired)
    repaired = repair_split_uppercase_word_fragments(repaired)
    repaired = repair_spaced_caps_em_dash_attributions(repaired)
    return repaired


def repair_clear_spaced_hyphenated_compounds(text: str) -> str:
    """Repair clear hyphenated compounds left with a space after the hyphen."""
    word_forms = hyphenation_word_forms(text)

    def _join(match: re.Match[str]) -> str:
        left, right = match.group("left"), match.group("right")
        hyphenated = f"{left}-{right}"
        if (left + right).casefold() in word_forms and hyphenated.casefold() not in word_forms:
            return left + right
        return hyphenated

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


def repair_flattened_hyphenation(text: str) -> str:
    """Recover soft hyphens after ingestion/chunking has collapsed line breaks."""
    word_forms = hyphenation_word_forms(text)

    def _join(match: re.Match[str]) -> str:
        left, right = match.group("left"), match.group("right")
        # A suspended compound leaves its hyphen before a conjunction.
        if right in {"and", "or", "nor", "but"}:
            return match.group(0)
        joined = left + right
        # Without a line break, a dash between ordinary words may mark dialogue.
        if left.casefold() in COMMON_WORDS and right in COMMON_WORDS and joined.casefold() not in word_forms:
            return match.group(0)
        if should_join_hyphenated_word(left, right, word_forms):
            return joined
        return match.group(0)

    return re.sub(
        r"(?<![\w-])(?P<left>[^\W\d_]+)-[ \t]+(?P<right>[a-zà-öø-ÿ]+)(?![\w-])",
        _join,
        text,
    )


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


def replace_markdown_links(text: str, *, image: bool = False) -> str:
    """Replace ``[label](target)`` (or ``![alt](target)``) with its label in linear time.

    Same result as ``re.sub(r"\\[([^\\]]+)\\]\\([^\\)]+\\)", r"\\1", text)`` and,
    for images, ``re.sub(r"!\\[([^\\]]*)\\]\\([^\\)]+\\)", r"\\1", text)``. Those
    patterns rescan to the next bracket from every ``[``, which is quadratic on
    text full of unclosed brackets; here each scan position is visited once.
    """
    min_label = 0 if image else 1
    pieces: list[str] = []
    position = 0
    close_bracket = -1
    paren_search_from = -1
    close_paren = -1
    start = text.find("[")
    while start != -1:
        match_start = start - 1 if image else start
        if image and (start == 0 or text[start - 1] != "!" or match_start < position):
            start = text.find("[", start + 1)
            continue
        if close_bracket <= start:
            close_bracket = text.find("]", start + 1)
            if close_bracket == -1:
                break
        target = close_bracket + 2
        if (
            close_bracket - start - 1 >= min_label
            and text.startswith("(", close_bracket + 1)
            and target < len(text)
            and text[target] != ")"
        ):
            if paren_search_from != target:
                paren_search_from = target
                close_paren = text.find(")", target)
            if close_paren != -1:
                pieces.append(text[position:match_start])
                pieces.append(text[start + 1:close_bracket])
                position = close_paren + 1
                start = text.find("[", position)
                continue
        start = text.find("[", start + 1)
    pieces.append(text[position:])
    return "".join(pieces)


def strip_rule_lines(text: str) -> str:
    """Remove ``---`` rule lines with their surrounding blank space in linear time.

    Same result as ``re.sub(r"(?m)^\\s*---+\\s*$", "", text)``, which rescans a run
    of blank lines from each of its line starts.
    """
    pieces: list[str] = []
    position = 0
    search_from = 0
    length = len(text)
    while True:
        dash = text.find("---", search_from)
        if dash == -1:
            break
        run_start = dash
        while run_start > position and text[run_start - 1] == "-":
            run_start -= 1
        run_end = dash + 3
        while run_end < length and text[run_end] == "-":
            run_end += 1
        search_from = run_end
        if run_start > 0 and text[run_start - 1] == "-":
            # The run continues into text an earlier match already consumed.
            continue
        lead = run_start
        while lead > position and text[lead - 1].isspace():
            lead -= 1
        if lead == 0 or text[lead - 1] == "\n":
            line_start = lead
        else:
            newline = text.find("\n", lead, run_start)
            if newline == -1:
                continue
            line_start = newline + 1
        trail = run_end
        while trail < length and text[trail].isspace():
            trail += 1
        if trail == length:
            line_end = length
        else:
            line_end = text.rfind("\n", run_end, trail)
            if line_end == -1:
                continue
        pieces.append(text[position:line_start])
        position = line_end
        search_from = max(search_from, line_end)
    pieces.append(text[position:])
    return "".join(pieces)


def rule_based_clean(text: str) -> str:
    """Apply fast rule-based text normalization for TTS preprocessing."""
    text = strip_page_furniture(text)
    text = repair_clear_spaced_hyphenated_compounds(text)
    text = repair_line_break_hyphenation(text)
    text = repair_separated_drop_cap_fragments(text)
    text = re.sub(r"(?i)<br\s*/?>", " ", text)
    text = strip_inline_numeric_references(text)
    text = normalize_markdown_tables(text)

    text = re.sub(r"(?m)^#{1,6}\s+", "", text)
    text = re.sub(r"[\*_]{1,3}([^\*_]+)[\*_]{1,3}", r"\1", text)
    text = text.replace("**", "")
    # Images first: the link pass would otherwise consume ``[alt](src)`` and
    # leave the image's ``!`` behind.
    text = replace_markdown_links(text, image=True)
    text = replace_markdown_links(text)
    text = re.sub(r"`", "", text)
    text = re.sub(r"\|+", " ", text)
    # Runs of whitespace and of section marks are matched only from the start of
    # the run (the mark rule keeps its one mid-run start, right after a word),
    # which keeps these substitutions linear on long runs.
    text = re.sub(r"(?:(?<!\s)\s+)?---+\s*", " ", text)
    # Keep numbered (and Roman numeral) references such as § 12 or ¶ IV.
    text = re.sub(
        r"(?:(?<![\w¶§])|(?<=\w[¶§]))[¶§]+(?![\w¶§]|\s*(?:\d|[IVXLCDM]+\b))", " ", text,
    )
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"(?<!\s)\s+([,.!?;:])", r"\1", text)
    text = re.sub(r"([,!?;:])([A-Za-z])", r"\1 \2", text)
    # A period is a joined sentence boundary only after a word and before a
    # capital; elsewhere it belongs to an initialism (Ph.D., p.m.) or a domain.
    text = re.sub(r"(?<=[a-z]{2})\.(?=[A-Z])", ". ", text)
    text = strip_trailing_numeric_source_note(text)
    text = repair_split_uppercase_word_fragments(text)
    text = repair_spaced_caps_em_dash_attributions(text)
    text = repair_clear_spaced_hyphenated_compounds(text)
    text = repair_known_closed_compound_hyphenation(text)
    text = repair_flattened_hyphenation(text)
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


def extract_result(response: str) -> str:
    """Extract cleaned text payload from model output."""
    result = response.strip()
    result = strip_rule_lines(result).strip()
    result = re.sub(r"(?<!-)---+\s*$", "", result).strip()
    result = re.sub(r"^\s*---+(?:\s|$)", "", result).strip()
    return result
