"""Scanned chapter cleanup must keep repairs without weakening content checks."""

from types import SimpleNamespace

import pytest

from modules.pipeline.stages.ingestion import ingest_source
from modules.tts.chunker import TextChunker
from modules.tts.cleaner import TextCleaner
from modules.tts.cleaner_config import CleanerConfig
from modules.ingestion.text_layout import page_furniture_lines
from modules.tts.cleaner_edits import reconcile_llm_edits
from modules.tts.cleaner_rules import (
    rule_based_clean,
    strip_page_furniture,
)
from tests.fixtures.scanned_chapter import SCANNED_CHAPTER


def _guard_rejects(source: str, cleaned: str) -> bool:
    """Whether the production LLM edit guard rejects any span of ``cleaned``."""
    edits = reconcile_llm_edits(source, cleaned)
    return bool(edits.alignment_error or edits.rejected_edits)


SCAN = """# Chapter One

THE LIGHTHOUSE KEEPER                                   12

The keep-
er climbed the stair- case at dusk, count ing each of the
one hundred and twelve steps.   The lamp was cold ; the sea was
loud and the w ind pressed against the glass like a hand.

13                    THE LIGHTHOUSE KEEPER

He struck a match , and the wick caught. Fig. 3 — lamp assembly (see p. 47) ¶
Out beyond the rocks a ship's light blinked twice, then went dark. He wa-
ited, listening, but there was only the tide."""

CLEANED = (
    "Chapter One The keeper climbed the staircase at dusk, counting each of the "
    "one hundred and twelve steps. The lamp was cold; the sea was loud and the "
    "wind pressed against the glass like a hand. He struck a match, and the wick "
    "caught. Fig. 3 — lamp assembly (see p. 47) "
    "Out beyond the rocks a ship's light blinked twice, then went dark. "
    "He waited, listening, but there was only the tide."
)


def _ingestion_runtime(**config):
    return SimpleNamespace(
        config=SimpleNamespace(**config),
        log_verbose=lambda *_args: None,
        notify_progress=lambda **_kwargs: None,
    )


@pytest.mark.parametrize("suffix", [".md", ".txt"])
@pytest.mark.parametrize("pre_chunk_cleanup", [True, False])
def test_scan_ingestion_repairs_layout_before_chunking(tmp_path, suffix, pre_chunk_cleanup):
    source_path = tmp_path / f"scan{suffix}"
    source_path.write_text(SCAN, encoding="utf-8")

    document = ingest_source(
        _ingestion_runtime(pre_chunk_document_cleanup=pre_chunk_cleanup), source_path
    )

    assert document.raw_markdown.splitlines() == SCAN.splitlines()
    assert len(document.chapters) == 1
    content = document.chapters[0].content
    assert "The keeper climbed the stair- case" in content
    assert "He waited, listening" in content
    assert "LIGHTHOUSE" not in content
    assert "12" not in content
    assert "13" not in content
    cleaned = " ".join(rule_based_clean(chunk) for chunk in TextChunker().chunk(content))
    assert "The keeper climbed the staircase" in cleaned
    assert "He waited, listening" in cleaned
    assert "count ing" in cleaned
    assert "w ind" in cleaned


@pytest.mark.parametrize("suffix", [".md", ".txt"])
def test_ingestion_preserves_suspended_compounds_when_reflowing(tmp_path, suffix):
    source_path = tmp_path / f"compounds{suffix}"
    source_path.write_text(
        "He compared pre-\nand post-war maps with a twenty-\nor thirty-year-old guide.",
        encoding="utf-8",
    )

    document = ingest_source(_ingestion_runtime(), source_path)
    cleaned = rule_based_clean(" ".join(TextChunker().chunk(document.chapters[0].content)))

    assert cleaned == "He compared pre- and post-war maps with a twenty- or thirty-year-old guide."


def test_epub_scan_ingestion_repairs_explicit_and_markup_line_breaks(tmp_path):
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_identifier("scan-layout-test")
    book.set_title("Scanned chapter")
    book.set_language("en")
    chapter = epub.EpubHtml(title="Chapter One", file_name="chapter.xhtml")
    chapter.content = (
        "<h1>Chapter One</h1><p>THE LIGHTHOUSE KEEPER 12</p>"
        "<p>The keep-<br/>er climbed the stair- case at dusk. He wa-\n"
        "ited, listening, but there was only the tide.</p>"
        "<p>13 THE LIGHTHOUSE KEEPER</p>"
        "<p>The sea was loud and the wind pressed against the glass like a hand.</p>"
        "<p>He counted <b>12</b> steps and read <em>THE LIGHTHOUSE KEEPER</em> aloud.</p>"
    )
    book.add_item(chapter)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.toc = [chapter]
    book.spine = [chapter]
    source_path = tmp_path / "scan.epub"
    epub.write_epub(str(source_path), book)

    document = ingest_source(_ingestion_runtime(), source_path)

    content = "\n\n".join(ch.content for ch in document.chapters)
    assert "The keeper climbed" in content
    assert "He waited, listening" in content
    assert content.count("THE LIGHTHOUSE KEEPER") == 1
    assert "He counted 12 steps and read THE LIGHTHOUSE KEEPER aloud." in content
    assert "13" not in content


@pytest.mark.parametrize("backend", ["fitz", "pymupdf4llm"])
def test_pdf_scan_ingestion_removes_numbered_running_headers(tmp_path, backend):
    import fitz

    source_path = tmp_path / "scan.pdf"
    with fitz.open() as pdf:
        for number, body in enumerate([
            "The keep-\ner climbed the stair- case at dusk, counting each step.\n"
            "The lamp was cold; the sea was loud and the wind pressed\n"
            "against the glass like a hand. He was alone in the tower.",
            "He struck a match, and the wick caught.\n"
            "Out beyond the rocks a ship's light blinked twice, then went dark.\n"
            "He wa-\nited, listening, but there was only the tide.",
        ], start=12):
            page = pdf.new_page()
            header = f"The lighthouse keeper {number}" if number == 12 else f"{number} The lighthouse keeper"
            page.insert_text((72, 35), header, fontsize=10)
            page.insert_text((72, 120), body, fontsize=12)
            page.insert_text((72, 800), str(number), fontsize=10)
        pdf.save(source_path)

    document = ingest_source(_ingestion_runtime(pdf_extraction_backend=backend), source_path)

    content = "\n\n".join(ch.content for ch in document.chapters)
    assert "lighthouse keeper" not in content.lower()
    assert "12" not in content
    assert "13" not in content
    assert "The keeper climbed" in content
    assert "He waited, listening" in content
    cleaned = " ".join(rule_based_clean(chunk) for chunk in TextChunker().chunk(content))
    assert "The keeper climbed the staircase" in cleaned


def test_scan_rules_repair_hyphenation_and_remove_page_furniture():
    cleaned = rule_based_clean(SCAN)
    assert cleaned.startswith("Chapter One The keeper climbed")
    assert "He waited, listening" in cleaned
    assert "LIGHTHOUSE" not in cleaned
    assert "12" not in cleaned
    assert "13" not in cleaned
    # Spaced hyphens are repairable; unmarked word boundaries still need the LLM.
    assert "staircase" in cleaned
    assert "count ing" in cleaned
    assert "w ind" in cleaned


def test_scan_fake_llm_trace_accepts_cleaned_text(monkeypatch):
    cleaner = TextCleaner(CleanerConfig(backend="cuda", skip_llm_when_not_needed=False))
    cleaner._loaded = True
    monkeypatch.setattr(cleaner, "_generate_llm_response", lambda _prompt: CLEANED)
    trace = cleaner.clean_with_trace(SCAN)
    assert trace.mode == "llm_success"
    assert trace.llm_error is None
    assert trace.cleaned_text == CLEANED
    assert trace.rule_based_text == rule_based_clean(SCAN)
    assert not _guard_rejects(SCAN, CLEANED)


@pytest.mark.parametrize("backend", ["cuda", "mlx", "api"])
@pytest.mark.parametrize("source", [SCANNED_CHAPTER, "The w ind blew.", "He was count ing steps."])
def test_split_words_reach_llm_with_default_gate(monkeypatch, backend, source):
    cleaner = TextCleaner(CleanerConfig(backend=backend))
    cleaner._loaded = True
    expected = rule_based_clean(source).replace("count ing", "counting").replace("w ind", "wind")
    prompts = []

    def generate(prompt):
        prompts.append(prompt)
        return expected

    monkeypatch.setattr(cleaner, "_generate_llm_response", generate)
    trace = cleaner.clean_with_trace(source)

    assert len(prompts) == 1
    assert "¶" not in prompts[0]
    assert trace.mode == "llm_success"
    assert trace.llm_error is None
    assert trace.cleaned_text == expected
    assert "split_words" in trace.artifact_score["reason_codes"]
    assert trace.artifact_score["total"] > cleaner.config.artifact_gate_light_threshold


@pytest.mark.parametrize("skip_llm", [False, True])
def test_basic_split_word_cleanup_never_loads_or_calls_llm(monkeypatch, skip_llm):
    cleaner = TextCleaner(CleanerConfig(backend="basic", skip_llm_when_not_needed=skip_llm))

    def unexpected_call(*_args):
        pytest.fail("Basic cleanup must never load or call an LLM")

    monkeypatch.setattr(cleaner, "load", unexpected_call)
    monkeypatch.setattr(cleaner, "_generate_llm_response", unexpected_call)
    trace = cleaner.clean_with_trace(SCANNED_CHAPTER)

    assert trace.mode == "rule_only"
    assert trace.cleaned_text == rule_based_clean(SCANNED_CHAPTER)
    assert "¶" not in trace.cleaned_text
    assert "count ing" in trace.cleaned_text
    assert "w ind" in trace.cleaned_text


@pytest.mark.parametrize("source, expected", [
    ("He waited. ¶ Out beyond the rocks.", "He waited. Out beyond the rocks."),
    ("He waited. § Then he left.", "He waited. Then he left."),
    ("¶ The lamp was cold. §", "The lamp was cold."),
    ("The lamp ¶ § was cold.", "The lamp was cold."),
    ("¶\n§\nThe lamp was cold.", "The lamp was cold."),
    ("See § 12 and §12, §§ 13–14, § IV, and ¶ 9.", "See § 12 and §12, §§ 13–14, § IV, and ¶ 9."),
])
def test_rules_remove_stray_marks_and_preserve_references(source, expected):
    assert rule_based_clean(source) == expected


@pytest.mark.parametrize("source, expected", [
    ("The keep-\ner wa-\nited.", "The keeper waited."),
    ("The keep-  \r\n  er waited.", "The keeper waited."),
    ("The stair- case is well-known.", "The staircase is well-known."),
    ("The keep- er wa- ited.", "The keeper waited."),
    ("Keep- er and Stair- case", "Keeper and Staircase"),
    ("twenty- or thirty-year-old", "twenty- or thirty-year-old"),
    ("pre- and post-war", "pre- and post-war"),
    ("twenty-\nor thirty-year-old", "twenty- or thirty-year-old"),
    ("pre-\nand post-war", "pre- and post-war"),
    ("Anglo- Saxon and type- 2", "Anglo- Saxon and type- 2"),
    ("well- being and self- governed", "well-being and self-governed"),
    ("She paused—\nthen spoke–\nsoftly.", "She paused— then spoke– softly."),
    ("He said-\nNo.", "He said- No."),
    ("# THE LIGHTHOUSE KEEPER 12\n# THE LIGHTHOUSE KEEPER 13", "THE LIGHTHOUSE KEEPER 12 THE LIGHTHOUSE KEEPER 13"),
    ("12\nThe lamp was cold.\n13", "12 The lamp was cold. 13"),
    ("THE LIGHTHOUSE KEEPER 12\nThe lamp was cold.", "THE LIGHTHOUSE KEEPER 12 The lamp was cold."),
    ("13 THE LIGHTHOUSE KEEPER\nThe lamp was cold.", "13 THE LIGHTHOUSE KEEPER The lamp was cold."),
    ("The Lighthouse Keeper 12\nThe lamp was cold.\n13 The Lighthouse Keeper", "The lamp was cold."),
    ("The Lighthouse Keeper\nThe lamp was cold.\nThe Lighthouse Keeper", "The Lighthouse Keeper The lamp was cold. The Lighthouse Keeper"),
    ("12\nThe lamp was cold.\f13\nThe wick caught.", "The lamp was cold. The wick caught."),
])
def test_layout_rules(source, expected):
    assert rule_based_clean(source) == expected


@pytest.mark.parametrize("source", [
    "The lamp was cold.\nThe lamp was cold.",
    "The lamp was cold\nThe lamp was cold",
    '"COME BACK"\n"COME BACK"',
    "“Come back,” he said.\n“Come back,” he said.",
    "1. First item\n2. First item\n3) Third item",
    "1. OPEN THE DOOR\n2. OPEN THE DOOR",
    "- OPEN THE DOOR\n- OPEN THE DOOR",
    "It happened in 1912.\nThey returned in 1912.",
    "in 1912\nin 1912",
    "In 1912\nIn 1912",
    "No\nNo",
    "I\nI",
    "# Chapter One\n# Chapter One",
])
def test_furniture_detector_preserves_prose_dialogue_lists_and_headings(source):
    assert page_furniture_lines(source) == set()
    assert strip_page_furniture(source) == source


@pytest.mark.parametrize("source, cleaned", [
    ("The keep er climbed the stair- case in the w ind.", "The keeper climbed the staircase in the wind."),
    ("12\nThe keep er waited.\f13\nThe lamp was cold.", "The keeper waited. The lamp was cold."),
    ("12\nThe keep er waited.\f13\nThe lamp was cold.", "12 The keeper waited. The lamp was cold."),
    ("THE KEEPER 12\nThe keep er waited.\n13 THE KEEPER", "The keeper waited."),
    ("THE KEEPER 12\nThe keep er waited.\n13 THE KEEPER", "THE KEEPER 12 The keeper waited."),
])
def test_guard_allows_only_identified_furniture_and_exact_merges(source, cleaned):
    assert not _guard_rejects(source, cleaned)


@pytest.mark.parametrize("source, cleaned", [
    ("He returned in 1912.", "He returned."),
    ("The keeper waited for 12 hours.", "The keeper waited for hours."),
    ("THE KEEPER 12\nThe keep er waited.", "The keeper left."),
    ("THE KEEPER 12\nThe keep er waited.", "The keeper."),
    ("THE KEEPER 12\nThe keeper met the keeper.", "The keeper met."),
    ("The keeper waited, waited, waited.", "The keeper waited."),
    ("1. First item\n2. Second item", "First item Second item"),
    ("# Chapter One\nThe keeper waited.", "The keeper waited."),
    ("The keep er climbed the stair- case.", "The keeper climbed the stairs."),
])
def test_guard_still_rejects_content_loss_or_substitution(source, cleaned):
    assert _guard_rejects(source, cleaned)


@pytest.mark.parametrize("sampler_api", ["sampler", "temp", "temperature"])
def test_mlx_cleanup_is_greedy_even_with_legacy_temperature(monkeypatch, sampler_api):
    import modules.tts.cleaner_mlx as mlx

    cleaner = TextCleaner(CleanerConfig(backend="mlx", temperature=0.35))
    calls = []

    def generate(_model, _tokenizer, **kwargs):
        if sampler_api == "temperature" and "temp" in kwargs:
            raise TypeError("unexpected keyword argument 'temp'")
        calls.append(kwargs)
        return "The keeper waited."

    monkeypatch.setattr(mlx, "generate", generate)
    monkeypatch.setattr(mlx, "make_sampler", (lambda temp: ("greedy", temp)) if sampler_api == "sampler" else None)
    assert cleaner._generate_mlx_response("prompt") == "The keeper waited."
    assert calls[0][sampler_api] == (("greedy", 0.0) if sampler_api == "sampler" else 0.0)
