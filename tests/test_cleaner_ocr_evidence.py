"""OCR guard decisions use document evidence rather than particular repairs."""

import pytest

from modules.tts.cleaner_edits import reconcile_llm_edits, repair_source_backed_fragments


@pytest.mark.parametrize("source, candidate", [
    ("inform ation travels", "information travels"),
    ("move q uickly", "move quickly"),
    ("a joy ful sound", "a joyful sound"),
    ("re configure the device", "reconfigure the device"),
    ("the river ## bank", "the riverbank"),
    ("the blue|berry", "the blueberry"),
    ("Sometimes we say some times.", "Sometimes we say sometimes."),
    ("She walked into town, then went in to town.", "She walked into town, then went into town."),
    ("sun light and sunlight", "sun light and sun light"),
    ("Icould see.", "I could see."),
    ("Her motherin-law came.", "Her mother-in-law came."),
    ("They were ready-forbattle.", "They were ready-for-battle."),
    ("## ould you stay? A # W small request.", "Would you stay? A small request."),
    ("| less noise", "bless noise"),
    ("the ungle grew", "the jungle grew"),
    ("B RIGHT mornings", "BRIGHT mornings"),
    ("S omething moved.", "Something moved."),
])
def test_general_ocr_evidence_allows_repairs(source, candidate):
    edits = reconcile_llm_edits(source, candidate)
    assert edits.text == candidate
    assert not edits.rejected_edits


@pytest.mark.parametrize("source, candidate", [
    ("step in to town", "step into town"),
    ("some times are hard", "sometimes are hard"),
    ("now here", "nowhere"),
    ("any way", "anyway"),
    ("every one", "everyone"),
    ("may be", "maybe"),
    ("in ## to town", "into town"),
    ("apart", "a part"),
    ("a nowhere-bound path", "a now-here-bound path"),
    ("The motherinlaw came.", "The mother in law came."),
    ("Away we go. A. way home.", "Away we go. Away home."),
    ("the ungle grew", "the 2ungle grew"),
    ("sunlight sunlight sun light", "sun light sunlight sun light"),
    ("We ate less today.", "We ate bless today."),
    ("Use vitamin D daily.", "Use vitamin daily."),
    ("Let X vary.", "Let vary."),
    ("A. B. Carter spoke.", "A. Carter spoke."),
    ("## ould you stay? # D please.", "Would you stay? please."),
    ("## ould you stay? # W please. # W again.", "Would you stay? please. again."),
    ("## ould you stay? Vitamin W helps.", "Would you stay? Vitamin helps."),
])
def test_ordinary_boundaries_and_unrelated_letters_are_protected(source, candidate):
    assert reconcile_llm_edits(source, candidate).rejected_edits


def test_spelling_evidence_is_available_outside_large_alignment_blocks():
    prefix = "Sometimes the bell rings. " * 150
    source = prefix + "It rings some times."
    candidate = prefix + "It rings sometimes."
    assert not reconcile_llm_edits(source, candidate).rejected_edits


def test_source_backed_postprocessing_restores_a_displaced_drop_cap():
    source = "## ould you listen? A # W quiet request."
    candidate = "ould you listen? A quiet request."
    repaired = repair_source_backed_fragments(source, candidate)
    assert repaired == "would you listen? A quiet request."
    assert not reconcile_llm_edits(source, repaired).rejected_edits


def test_source_backed_postprocessing_does_not_guess_an_initial():
    assert repair_source_backed_fragments("## ould you listen?", "ould you listen?") == "ould you listen?"


def test_source_backed_postprocessing_leaves_numbers_for_the_guard():
    source, candidate = "There were 125 people.", "There were 25 people."
    assert repair_source_backed_fragments(source, candidate) == candidate
    assert reconcile_llm_edits(source, candidate).rejected_edits


def test_rejected_abbreviation_keeps_its_period_with_general_ocr_repairs():
    source = "Dr. Cole gathered inform ation while q uickly arrang ing displays."
    candidate = "Doc. Cole gathered information while quickly arranging displays."
    edits = reconcile_llm_edits(source, candidate)
    assert edits.text == "Dr. Cole gathered information while quickly arranging displays."
    assert edits.accepted_edits == 3
    assert len(edits.rejected_edits) == 1


@pytest.mark.parametrize("prefix", ["", "Earlier words.\n", "Done. ", "Ready! ", "Really? ", "Listen: ", "BEST LOSER WINS ", "INTRODUCTION "])
@pytest.mark.parametrize("marker", ["#", "##", "|"])
def test_marked_restoration_requires_a_sentence_or_heading_boundary(prefix, marker):
    source = f"{prefix}{marker} he next train arrived."
    candidate = f"{prefix}the next train arrived."
    assert not reconcile_llm_edits(source, candidate).rejected_edits


@pytest.mark.parametrize("source, candidate", [
    ("We ate # less today.", "We ate bless today."),
    ("We saw ## he next train.", "We saw the next train."),
    ("Ask NASA | he next question.", "Ask NASA the next question."),
    ("We ate # less today. A # B marker.", "We ate bless today. A marker."),
])
def test_mid_sentence_markers_do_not_authorize_leading_letters(source, candidate):
    assert reconcile_llm_edits(source, candidate).rejected_edits


def test_postprocessing_does_not_move_a_drop_cap_into_mid_sentence_prose():
    source = "We ate # less today. A # B marker."
    candidate = "We ate less today. A marker."
    assert repair_source_backed_fragments(source, candidate) == candidate
