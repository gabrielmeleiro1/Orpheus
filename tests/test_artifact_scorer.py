from modules.tts.artifact_scorer import CleaningPath, score_text_artifacts


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

