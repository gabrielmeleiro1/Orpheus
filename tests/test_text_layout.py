"""Dehyphenation uses document spelling before compound heuristics."""

import pytest

from modules.ingestion.pdf_artifact_normalizer import repair_line_break_hyphenation as repair_pdf
from modules.ingestion.text_layout import repair_line_break_hyphenation
from modules.tts.cleaner_edits import reconcile_llm_edits
from modules.tts.cleaner_rules import rule_based_clean


@pytest.mark.parametrize("repair", [repair_line_break_hyphenation, repair_pdf, rule_based_clean])
@pytest.mark.parametrize("text, expected", [
    ("an exam-\nple of infor-\nmation", "an example of information"),
    ("her self-\ncontrol", "her self-control"),
    ("a well-\nknown fact", "a well-known fact"),
    ("study neurophysiol-\nogy", "study neurophysiology"),
    ("wellbeing matters to well-\nbeing", "wellbeing matters to wellbeing"),
    ("The unusual exam-ple is an exam-\nple.", "The unusual exam-ple is an exam-ple."),
    ("nonexistent is non-\nexistent", "nonexistent is nonexistent"),
    ("MacDonald met Mac-\nDonald.", "MacDonald met MacDonald."),
])
def test_wrapped_words_use_evidence_then_general_dehyphenation(repair, text, expected):
    assert repair(text) == expected


@pytest.mark.parametrize("left, right", [
    ("self", "control"), ("well", "known"), ("non", "fiction"),
    ("half", "hearted"), ("all", "seeing"), ("ex", "president"),
    ("cross", "country"), ("full", "length"), ("high", "speed"),
    ("low", "cost"), ("long", "term"), ("short", "term"),
    ("great", "grandfather"), ("semi", "formal"), ("quasi", "legal"),
    ("anti", "war"), ("multi", "layered"), ("co", "operate"),
])
def test_compound_prefixes_keep_the_hyphen(left, right):
    assert repair_line_break_hyphenation(f"{left}-\n{right}") == f"{left}-{right}"


def test_co_before_a_consonant_joins_by_default():
    assert repair_line_break_hyphenation("co-\nver") == "cover"


@pytest.mark.parametrize("text", ["Anglo-\nSaxon", "Jean-\nPaul", "French-\nCanadian", "Mc-\nDonald"])
def test_capitalized_right_parts_keep_the_hyphen(text):
    assert "-" in repair_line_break_hyphenation(text)


@pytest.mark.parametrize("repair", [repair_line_break_hyphenation, repair_pdf, rule_based_clean])
@pytest.mark.parametrize("left, right", [("Keep", "er"), ("Stair", "case"), ("Inform", "ation")])
def test_capitalized_left_parts_join_with_lowercase_right(repair, left, right):
    assert repair(f"{left}-\n{right}") == left + right


@pytest.mark.parametrize("left, right", [("Self", "control"), ("Well", "known"), ("Non", "fiction")])
def test_capitalized_compound_prefixes_keep_hyphens(left, right):
    assert repair_line_break_hyphenation(f"{left}-\n{right}") == f"{left}-{right}"
    assert "-" in rule_based_clean(f"{left}- {right}")


@pytest.mark.parametrize("text", ["Wait- what happened?", "Look- there it is."])
def test_flattened_dashes_between_common_words_are_ambiguous(text):
    assert rule_based_clean(text) == text


def test_common_words_still_join_with_suffix_or_document_evidence():
    assert rule_based_clean("Go- ing home.") == "Going home."
    assert rule_based_clean("Into town, then in- to town.") == "Into town, then into town."


def test_suspended_compounds_keep_their_boundary():
    text = "pre-\nand post-war"
    assert repair_line_break_hyphenation(text) == text
    assert rule_based_clean(text) == "pre- and post-war"


@pytest.mark.parametrize("text, candidate, allowed", [
    ("an exam-\nple", "an example", True),
    ("self-\ncontrol", "selfcontrol", False),
    ("wellbeing and well-\nbeing", "wellbeing and wellbeing", True),
    ("exam-ple and exam-\nple", "exam-ple and example", False),
    ("Keep- er and Stair- case", "Keeper and Staircase", True),
    ("Go in-\nto town.", "Go into town.", True),
    ("Self- control", "Selfcontrol", False),
    ("Jean-\nPaul", "JeanPaul", False),
    ("stair case", "staircase", False),
])
def test_guard_uses_the_same_hyphenation_evidence(text, candidate, allowed):
    edits = reconcile_llm_edits(text, candidate)
    assert (not edits.rejected_edits) is allowed


@pytest.mark.parametrize("spelling, wrapped, expected", [
    ("exam-ple", "exam-\nple", "exam-ple"),
    ("wellbeing", "well-\nbeing", "wellbeing"),
])
def test_pdf_uses_spelling_evidence_from_other_pages(spelling, wrapped, expected):
    from modules.ingestion.pdf_artifact_normalizer import PDFLineArtifactPolicy

    pages = [{"text": f"The earlier spelling is {spelling}."}, {"text": f"Here is {wrapped}."}]
    assert PDFLineArtifactPolicy().normalize_pages(pages)[1]["text"] == f"Here is {expected}."
