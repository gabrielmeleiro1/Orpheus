"""Adversarial-input bounds for text processing on the conversion hot path.

Each case feeds about 100 KB of input that drove a backtracking pattern or a
per-line rescan into quadratic (or worse) time. Before the fixes every case
took from several seconds to minutes; linear code finishes in milliseconds, so
the one-second bound leaves wide headroom on slow machines.
"""

from __future__ import annotations

import random
import re
import time

import pytest

from modules.ingestion.models import Chapter
from modules.ingestion.pdf_artifact_normalizer import _is_toc_duplicate_page
from modules.text.document_normalizer import _detect_repeated_headers
from modules.tts.artifact_scorer import _strip_code_spans, score_text_artifacts
from modules.tts.cleaner_edits import reconcile_llm_edits
from modules.tts.cleaner_rules import (
    extract_result,
    repair_separated_drop_cap_fragments,
    rule_based_clean,
    strip_inline_numeric_references,
)

ADVERSARIAL_CHARS = 100_000
TIME_BOUND_SECONDS = 1.0


def _elapsed(func, *args) -> float:
    started = time.perf_counter()
    func(*args)
    return time.perf_counter() - started


_ADVERSARIAL_CASES = [
    ("backtick run", score_text_artifacts, "`" * ADVERSARIAL_CHARS + "a"),
    ("section marks before a word", rule_based_clean, "§" * ADVERSARIAL_CHARS + "a"),
    ("whitespace before no rule", rule_based_clean, "\xa0" * ADVERSARIAL_CHARS + "!"),
    ("whitespace before no citation", strip_inline_numeric_references, "\t" * ADVERSARIAL_CHARS + "!"),
    ("blank lines before no drop cap", repair_separated_drop_cap_fragments, "\n" * ADVERSARIAL_CHARS),
    ("blank lines before a hash", repair_separated_drop_cap_fragments, "\n\n" * (ADVERSARIAL_CHARS // 2) + "#"),
    ("dash run not at the end", extract_result, "-" * ADVERSARIAL_CHARS + "!"),
    ("blank page lines", _is_toc_duplicate_page, "\n" * ADVERSARIAL_CHARS + "a"),
]


@pytest.mark.parametrize(
    ("label", "func", "text"),
    _ADVERSARIAL_CASES,
    ids=[case[0] for case in _ADVERSARIAL_CASES],
)
def test_adversarial_text_is_processed_in_linear_time(label, func, text):
    assert _elapsed(func, text) < TIME_BOUND_SECONDS, label


def test_llm_edit_guard_marker_gaps_are_linear():
    marker_gap = "#" * ADVERSARIAL_CHARS
    source = f"alpha {marker_gap}x beta"
    output = f"alpha {marker_gap}x gamma"
    assert _elapsed(reconcile_llm_edits, source, output) < TIME_BOUND_SECONDS


def test_running_header_detection_is_linear_in_chapter_lines():
    # Title-case lines separated by blanks are all header candidates; each one
    # used to rescan the whole chapter for its first and last non-blank lines.
    content = "\n\n".join(f"Running Header Line {index}" for index in range(8_000))
    chapter = Chapter(number=1, title="One", content=content)
    assert _elapsed(_detect_repeated_headers, [chapter]) < TIME_BOUND_SECONDS


def test_speech_typography_cost_does_not_rescan_the_chapter_per_chunk():
    # Learning narrative casing from an ~90 KB chapter takes tens of
    # milliseconds; redoing it for each of 200 chunks would take seconds.
    from modules.pipeline.stages.synthesis import build_batch_items

    class Runtime:
        class config:
            voice = "am_adam"
            speed = 1.0

    source = "Dr. Jekyll met Mr. Utterson near the Strand. " * 2_000
    chunks = [f"DR. JEKYLL walked to the river {index}." for index in range(200)]
    started = time.perf_counter()
    items = build_batch_items(Runtime(), chunks=chunks, source=source)

    assert time.perf_counter() - started < TIME_BOUND_SECONDS
    assert len(items) == 200
    assert items[0].text.startswith("Dr. Jekyll walked to the river")


# Reference patterns as they were before the linear rewrites. On small inputs
# the rewritten rules must produce exactly the same output.
_ORIGINAL_CITATIONS = re.compile(r"\s*\[\d{1,4}\]\s*")
_ORIGINAL_CODE_SPAN = re.compile(r"(`+).*?\1", re.DOTALL)


def _random_strings(alphabet: str, count: int = 3_000, max_len: int = 12):
    rng = random.Random(20261116)
    for _ in range(count):
        yield "".join(rng.choice(alphabet) for _ in range(rng.randint(0, max_len)))


def test_inline_citation_strip_matches_original_output():
    for text in _random_strings("§¶a1I \n\t[]-.,"):
        assert strip_inline_numeric_references(text) == _ORIGINAL_CITATIONS.sub(
            " ", text
        ), repr(text)


_ORIGINAL_LINKS = re.compile(r"\[([^\]]+)\]\([^\)]+\)")
_ORIGINAL_IMAGES = re.compile(r"!\[([^\]]*)\]\([^\)]+\)")
_ORIGINAL_RULE_LINES = re.compile(r"(?m)^\s*---+\s*$")


def test_link_and_rule_line_rewrites_match_original_output():
    from modules.tts.cleaner_rules import replace_markdown_links, strip_rule_lines

    for text in _random_strings("[]()!a \n", count=20_000, max_len=14):
        assert replace_markdown_links(text) == _ORIGINAL_LINKS.sub(r"\1", text), repr(text)
        assert replace_markdown_links(text, image=True) == _ORIGINAL_IMAGES.sub(
            r"\1", text
        ), repr(text)
    for text in _random_strings("-- \n\t\x1ca", count=20_000, max_len=16):
        assert strip_rule_lines(text) == _ORIGINAL_RULE_LINES.sub("", text), repr(text)


@pytest.mark.parametrize(
    "text",
    [
        "[" * ADVERSARIAL_CHARS,
        "[a](" * (ADVERSARIAL_CHARS // 4),
        "![" * (ADVERSARIAL_CHARS // 2),
    ],
    ids=["unclosed brackets", "unclosed targets", "unclosed images"],
)
def test_markdown_links_are_replaced_in_linear_time(text):
    assert _elapsed(rule_based_clean, text) < TIME_BOUND_SECONDS


def test_rule_lines_are_stripped_in_linear_time():
    text = "\n" * ADVERSARIAL_CHARS + "x" + "\n \n" * (ADVERSARIAL_CHARS // 3) + "---x"
    assert _elapsed(extract_result, text) < TIME_BOUND_SECONDS


def test_code_span_strip_matches_markdown_pairs():
    assert _strip_code_spans("use `foo` here") == "use   here"
    assert _strip_code_spans("``a`b`` tail") == "  tail"
    assert _strip_code_spans("it`s fine") == "it`s fine"
    # An unmatched run is literal text, as in Markdown.
    assert _strip_code_spans("``") == "``"
    for text in _random_strings("`ab ", max_len=10):
        # Single-backtick spans pair exactly as the backreference did; longer
        # unpaired runs are now left alone instead of splitting into shorter pairs.
        if all(len(run) == 1 for run in re.findall(r"`+", text)):
            assert _strip_code_spans(text) == _ORIGINAL_CODE_SPAN.sub(" ", text), repr(text)
