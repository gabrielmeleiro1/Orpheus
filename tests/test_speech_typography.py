"""Capitalized print typography must not turn narrative names into initialisms."""
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from modules.ingestion.models import Chapter
from modules.pipeline.chapter_narration import chapter_announcement
from modules.pipeline.stages.synthesis import build_batch_items
from modules.pipeline.stages.overlap_synthesis import _synthesize_overlap_chunk_audio
from modules.tts.speech_typography import normalize_speech_typography


SOURCE = "Dr. Jekyll was quite at ease. Lanyon knew him.\n\nHASTIE LANYON."


def test_spoken_heading_recovers_narrative_case_without_changing_source():
    chapter = Chapter(4, "DR. JEKYLL WAS QUITE AT EASE", SOURCE)
    assert chapter_announcement(chapter) == "Chapter number 4. Dr. Jekyll was quite at ease."
    assert chapter.title == "DR. JEKYLL WAS QUITE AT EASE"
    assert chapter.content == SOURCE


@pytest.mark.parametrize("apostrophe", ["'", "\u2019"])
def test_possessive_heading_uses_known_name_stem_and_preserves_acronym(apostrophe):
    heading = f"DR. LANYON{apostrophe}S NARRATIVE AND JEKYLL{apostrophe}S CASE AT NASA{apostrophe}S LAB"
    spoken = normalize_speech_typography(heading, SOURCE, heading=True)
    assert f"Lanyon{apostrophe}s" in spoken
    assert f"Jekyll{apostrophe}s" in spoken
    assert f"NASA{apostrophe}S" in spoken


@pytest.mark.parametrize("signature", ["\n\nHASTIE LANYON.", ". HASTIE LANYON"])
def test_terminal_name_recovers_signature_with_known_surname(signature):
    source = "Lanyon knew Jekyll" + signature
    assert normalize_speech_typography(source, source).endswith(
        "Hastie Lanyon." if signature.endswith(".") else "Hastie Lanyon"
    )


@pytest.mark.parametrize("text", [
    "FBI CPU NASA", "US UK USA", "DNA RNA RAM ROM", "UNESCO UNICEF LANYON",
    "IV XIV MIX", "CPU_1 JEKYLL_2 AT1 X86", "He said JEKYLL and AT.",
    "THIS UNKNOWN HEADING", "FBI LANYON", "HASTIE UNKNOWN",
])
def test_acronyms_numerals_identifiers_and_unproven_casing_are_retained(text):
    # Even an ordinary-word lookalike elsewhere must not change an acronym.
    source = "us ram mix at Jekyll Nasa Cpu Fbi Lanyon.\n\n" + text
    actual = normalize_speech_typography(text, source)
    # A known surname can recover its case in a name run; the acronym cannot.
    assert actual == text.replace("LANYON", "Lanyon")


def test_printed_opening_name_uses_narrative_evidence():
    source = "MR. UTTERSON was waiting. Mr. Utterson saw him."
    assert normalize_speech_typography(source, source) == "Mr. Utterson was waiting. Mr. Utterson saw him."


def test_single_word_heading_requires_source_evidence():
    assert normalize_speech_typography("JEKYLL", SOURCE, heading=True) == "Jekyll"
    assert normalize_speech_typography("NASA", SOURCE, heading=True) == "NASA"
    assert normalize_speech_typography("UNSEEN", SOURCE, heading=True) == "UNSEEN"


@pytest.mark.parametrize("title", ["AT&T", "JEKYLL-ID", "AT/JEKYLL", "AT:JEKYLL", "AT.JEKYLL", "AT_1", "AT1", "AT@JEKYLL"])
def test_punctuated_identifiers_keep_their_exact_spelling_in_headings(title):
    assert normalize_speech_typography(title, SOURCE, heading=True) == title


def test_batch_and_overlap_use_same_speech_copy_and_preserve_cleaned_chunks():
    runtime = SimpleNamespace(
        config=SimpleNamespace(speech_normalization=False, voice="am_adam", speed=1.0),
        synthesize_speech=Mock(return_value=np.ones(24, dtype=np.float32)),
    )
    chunks = ["HASTIE LANYON."]
    items = build_batch_items(runtime, chunks=chunks, source=SOURCE)
    assert items[0].text == "Hastie Lanyon."
    audio = _synthesize_overlap_chunk_audio(runtime, chunk_idx=0, cleaned_chunk=chunks[0], source=SOURCE)
    assert len(audio) == 24
    runtime.synthesize_speech.assert_called_once_with(text="Hastie Lanyon.", voice="am_adam", speed=1.0)
    assert chunks == ["HASTIE LANYON."]
