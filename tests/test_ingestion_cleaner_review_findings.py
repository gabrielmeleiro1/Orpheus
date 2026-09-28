"""Content-preservation regressions from the UI-refactor review."""

from unittest.mock import patch

import numpy as np
import pytest
import soundfile as sf
from ebooklib import epub

from modules.ingestion.epub_sections import build_epub_sections, parse_epub_markup, soup_to_epub_text
from modules.ingestion.markdown_parser import MarkdownParser
from modules.ingestion.pdf_artifact_normalizer import normalize_pdf_artifacts
from modules.ingestion.text_layout import repair_text_layout
from modules.ingestion.text_parser import TextParser
from modules.pipeline.chapter_narration import chapter_body
from modules.text.document_normalizer import normalize_extracted_document
from modules.tts.cleaner_edits import reconcile_llm_edits
from modules.tts.cleaner_rules import rule_based_clean
from tests.test_cleaner_edits import _cleaner as _mock_cleaner


def _cleaner(monkeypatch, candidate):
    cleaner = _mock_cleaner(monkeypatch, candidate)
    monkeypatch.setattr(cleaner, "_needs_llm_cleaning", lambda _text: True)
    return cleaner


@pytest.mark.parametrize("text", ["The answer is\n42\nwhich is exact.", "42"])
@pytest.mark.parametrize("kind", ["txt", "md", "epub", "cleaner"])
def test_01_standalone_numbers_are_content(tmp_path, text, kind):
    if kind in {"txt", "md"}:
        path = tmp_path / f"numbers.{kind}"
        path.write_text(text, encoding="utf-8")
        parser = TextParser if kind == "txt" else MarkdownParser
        result = parser(path).parse().chapters[0].content
    elif kind == "epub":
        result = soup_to_epub_text(parse_epub_markup("<p>" + text + "</p>"))
    else:
        result = rule_based_clean(text)
    assert "42" in result


@pytest.mark.parametrize("text", [
    "CHAPTER 1\nThe voyage began.",
    "The Wind Rises\nA first verse.\nThe Wind Rises\nA second verse.",
])
def test_02_headings_and_refrains_are_not_furniture(text):
    assert repair_text_layout(text) == text


@pytest.mark.parametrize("tag", ["a", "span"])
def test_03_epub_fragment_anchors_survive_text_extraction(tag):
    book = epub.EpubBook()
    page = epub.EpubHtml(title="Stories", file_name="stories.xhtml")
    page.content = (
        f'<p><{tag} id="one"></{tag}>The first story begins. ' + "First story prose. " * 12 + "</p>"
        f'<p><{tag} id="two"></{tag}>The second story begins. ' + "Second story prose. " * 12 + "</p>"
    )
    book.add_item(page)
    book.spine = [page]
    book.toc = [epub.Link("stories.xhtml#one", "First", "one"), epub.Link("stories.xhtml#two", "Second", "two")]
    sections = build_epub_sections(book)
    assert [section.title for section in sections] == ["First", "Second"]
    assert "second story" not in sections[0].content
    assert "first story" not in sections[1].content


def test_04_pdf_numbered_headings_remain_distinct():
    pages = [{"text": f"Experiment {number}\nThe measurement was recorded for this trial."} for number in (1, 2)]
    result = normalize_pdf_artifacts(pages)
    assert all(f"Experiment {number}" in page["text"] for number, page in enumerate(result, 1))


@pytest.mark.parametrize("text, expected", [
    ("Her self-\ncontrol returned.", "Her self-control returned."),
    ("Her self- control returned.", "Her self-control returned."),
    ("Their well-\nbeing mattered.", "Their well-being mattered."),
])
def test_05_compound_hyphens_survive(text, expected):
    assert rule_based_clean(text) == expected
    if "\n" in text:
        assert repair_text_layout(text) == expected


def test_06_markdown_opening_sentence_is_not_a_heading(tmp_path):
    path = tmp_path / "enough.md"
    path.write_text("# Enough\n\nEnough.\nI refused to take another step.", encoding="utf-8")
    assert chapter_body(MarkdownParser(path).parse().chapters[0]).startswith("Enough.")


def test_07_heading_only_part_generates_announcement(tmp_path):
    from modules.ingestion.epub_parser import EPUBParser
    from modules.pipeline import ConversionPipeline, PipelineConfig

    book = epub.EpubBook()
    book.set_identifier("part-divider")
    book.set_title("Voyage")
    book.set_language("en")
    part = epub.EpubHtml(title="Part I", file_name="part.xhtml")
    part.content = '<h1 id="part">Part I</h1><h1 id="arrival">Arrival</h1><p>' + "The ship approached the shore at dawn. " * 20 + "</p>"
    book.add_item(part)
    book.spine = [part]
    book.toc = [epub.Link("part.xhtml#part", "Part I", "part"), epub.Link("part.xhtml#arrival", "Arrival", "arrival")]
    book.add_item(epub.EpubNcx())
    path = tmp_path / "part.epub"
    epub.write_epub(str(path), book)
    document = normalize_extracted_document(EPUBParser(path).parse(), source_type="epub")
    chapter = document.chapters[0]
    assert chapter.title == "Part I"
    pipeline = ConversionPipeline(PipelineConfig(output_dir=tmp_path / "out", temp_dir=tmp_path / "temp"))
    processor = pipeline.stages.chapter_processor
    with (
        patch.object(processor, "ensure_chapter_tts_engine"),
        patch.object(type(processor.runtime), "synthesize_speech", return_value=np.ones(12000, dtype=np.float32)) as speak,
    ):
        result, output, cancelled = processor.prepare_chapter_audio(
            chapter=chapter, chapter_idx=0, total_chapters=2, output_dir=tmp_path / "out",
        )
    assert result.error is None
    assert not cancelled
    assert sf.info(output).frames == 156000
    assert speak.call_args.kwargs["text"] == "Part number 1."


@pytest.mark.parametrize("source, candidate", [
    ("We went in to see her.", "We went into see her."),
    ("They are now here.", "They are nowhere."),
    ("They stood apart.", "They stood a part."),
])
def test_08_ambiguous_word_boundaries_are_rejected(monkeypatch, source, candidate):
    assert reconcile_llm_edits(source, candidate).rejected_edits
    assert _cleaner(monkeypatch, candidate).clean_with_trace(source).cleaned_text == source


def test_09_arbitrary_leading_letter_is_rejected(monkeypatch):
    source = "We ate less today."
    trace = _cleaner(monkeypatch, "We ate bless today.").clean_with_trace(source)
    assert trace.cleaned_text == source
    assert trace.llm_rejected_edits


@pytest.mark.parametrize("source", ["Use vitamin D daily.", "Let X equal two.", "Ask J. Smith today.", "Apply 5 V here."])
def test_10_single_letters_are_preserved(monkeypatch, source):
    candidate = source.replace("D ", "").replace("X ", "").replace("J. ", "").replace("V ", "")
    trace = _cleaner(monkeypatch, candidate).clean_with_trace(source)
    assert trace.cleaned_text == source
    assert trace.llm_rejected_edits


@pytest.mark.parametrize("before, after", [("-12.5", "12.5"), ("+12.5", "12.5"), ("12.5", "12,5"), ("1:30", "1 30"), ("3/4", "3-4")])
def test_11_numeric_punctuation_is_protected(monkeypatch, before, after):
    source = f"The answer is {before}."
    trace = _cleaner(monkeypatch, f"The answer is {after}.").clean_with_trace(source)
    assert trace.cleaned_text == source
    assert trace.llm_rejected_edits


def test_12_typography_repairs_count_toward_edit_budget(monkeypatch):
    source = '“Wait”—Mara said. “Stay”—she added.'
    candidate = '"Wait"–Nora said. "Stay"–she added.'
    edits = reconcile_llm_edits(source, candidate)
    assert edits.accepted_edits >= 4
    traces = _cleaner(monkeypatch, candidate).clean_chunks_with_trace([source, source])
    assert [trace.mode for trace in traces] == ["llm_success", "llm_success"]
    assert all(trace.cleaned_text == candidate.replace("Nora", "Mara") for trace in traces)


def test_13_pdf_heading_provenance_survives_reflow(tmp_path, monkeypatch):
    from modules.ingestion.pdf_parser import PDFParser

    path = tmp_path / "arrival.pdf"
    path.touch()
    parser = PDFParser(path, validate=False)
    parser._metadata = {"title": "Voyage"}
    parser._total_pages = 1
    parser._toc = [[1, "Arrival", 1]]
    body = "The ship approached the shore at dawn. " * 20
    monkeypatch.setattr(parser, "_extract_pages", lambda: [{"text": "Arrival\n" + body}])
    document = normalize_extracted_document(parser.parse(), source_type="pdf", skip_non_prose_sections=False)
    assert chapter_body(document.chapters[0]) == body.strip()


def test_14_rejected_abbreviation_does_not_duplicate_period(monkeypatch):
    source = "Dr. Smith was count ing steps and gathering inform ation in the w ind."
    candidate = "Doc. Smith was counting steps and gathering information in the wind."
    trace = _cleaner(monkeypatch, candidate).clean_with_trace(source)
    assert trace.mode == "llm_success"
    assert trace.cleaned_text == candidate.replace("Doc.", "Dr.")


def test_15_rejection_offsets_address_recorded_candidate(monkeypatch):
    source = "At the time Mara was count ing steps by the stair case in the w ind."
    candidate = "At he time Nora was counting steps by the staircase in the wind."
    trace = _cleaner(monkeypatch, candidate).clean_with_trace(source)
    assert trace.llm_rejected_edits
    for edit in trace.llm_rejected_edits:
        assert trace.llm_output[edit["llm_start"]:edit["llm_end"]] == edit["after"]
