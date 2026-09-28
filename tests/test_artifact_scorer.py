import pytest

from modules.tts.artifact_scorer import CleaningPath, score_text_artifacts
from modules.tts.cleaner_config import CleanerConfig
from modules.tts.cleaner_rules import rule_based_clean
from tests.fixtures.scanned_chapter import SCANNED_CHAPTER


def test_artifact_scorer_keeps_clean_prose_on_rules_only_path():
    score = score_text_artifacts(
        "Dr. Smith walked through the quiet room. The conversation continued normally."
    )

    assert score.total < 0.20
    assert score.cleaning_path == CleaningPath.RULES_ONLY
    assert "clean" in score.reason_codes


def test_artifact_scorer_escalates_pdf_and_ocr_artifacts():
    score = score_text_artifacts(
        "conver-\nsation\n\nA B C broken   words � with 123l mixed page 42\nx\nq"
    )

    assert score.hyphenation_breaks == 1
    assert score.replacement_char_count == 1
    assert score.short_line_ratio > 0
    assert score.total >= 0.55
    assert score.cleaning_path == CleaningPath.HEAVY_LLM
    assert "hyphenation_breaks" in score.reason_codes


@pytest.mark.parametrize("text", [
    "The w ind pressed against the glass.",
    "P erhaps the keeper waited.",
    "The b roken lamp was cold.",
    "He was count ing the steps.",
    "The conversa tion ended.",
    "Several conversa tions followed.",
    "His amaze ment showed.",
    "Her kind ness helped.",
    "He spoke quiet ly.",
    "They collected inform ation.",
    "She moved q uickly.",
    "The z ebra approached.",
    "It was a joy ful reunion.",
    "Several arrange ments followed.",
])
def test_one_clear_split_crosses_light_gate(text):
    score = score_text_artifacts(text)

    assert score.split_word_count == 1
    assert score.total > CleanerConfig().artifact_gate_light_threshold
    assert score.cleaning_path == CleaningPath.LIGHT_LLM
    assert score.reason_codes == ["split_words"]
    assert score.as_trace_dict()["split_word_count"] == 1


@pytest.mark.parametrize("apply_rules", [False, True])
def test_flattened_scanned_chapter_escalates_for_split_words(apply_rules):
    text = rule_based_clean(SCANNED_CHAPTER) if apply_rules else SCANNED_CHAPTER
    score = score_text_artifacts(text)

    assert score.split_word_count == 2
    assert score.total > CleanerConfig().artifact_gate_light_threshold
    assert score.reason_codes == ["split_words"]


@pytest.mark.parametrize("text", [
    '"I am ready," said Ed. "A quiet night, or a storm? O wind, be still!"',
    "J. R. R. Tolkien and U. S. Grant met Dr. Smith and Mr. Ly.",
    "J. smith and T. he were the labels on the samples.",
    "They walked 5 km in 2 h with 3 kg, then measured 4 m in length.",
    "The 1st, 2nd, and 3rd runners finished on the 21st of May.",
    "b. ind is the label; (c) he is the next entry; d: tion follows.",
    "The W symbol and the x axis are shown; x is positive and y varies.",
    "Use h and w as dimensions. Put w here and give t his original value.",
    "A new lamp stood where I left it. O little town, a wind is rising.",
    "Well er perhaps we should wait, said Ed to her brother.",
    "She studied special ed and read about the suffix 'ing' and ending 'ly'.",
    "for w in words: print(w); if x is None: return count_ing",
    "Use `count ing` and `w ind` as literal inputs to the function.",
    "```text\ncount ing\nw ind\n```",
    "foo_ing count_ing foo.ly path/ing STEM ing",
    "The keeper climbed the staircase, counting steps as the wind blew.",
    "T he keeper waited.",
    "A cat slept while I am working.",
    "Let x equals y describe the equation; z represents a value.",
    "The beam is 5 m tall and the pulse lasted 2 s exactly.",
    "P. erhaps and Q. uickly are initials on a form.",
])
def test_split_word_signal_preserves_clean_prose_and_literals(text):
    score = score_text_artifacts(text)

    assert score.split_word_count == 0
    assert "split_words" not in score.reason_codes
    # The existing digit/letter signal may score ordinals independently.
    if not score.suspicious_digit_letter_mix:
        assert score.total == 0


def test_unbalanced_list_marker_does_not_add_split_word_signal():
    score = score_text_artifacts("t) he is the next entry")

    assert score.split_word_count == 0
    assert "split_words" not in score.reason_codes
