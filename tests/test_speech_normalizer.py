from modules.tts.speech_normalizer import SpeechNormalizationConfig, normalize_for_speech


def test_speech_normalizer_prepares_tts_only_text_without_rewriting_source():
    text = "Dr. Smith arrived at 5:30 p.m. on 12 April 1892."

    spoken = normalize_for_speech(text, SpeechNormalizationConfig(enabled=True))

    assert spoken == "Doctor Smith arrived at five thirty P.M. on twelve April eighteen ninety two."
    assert text == "Dr. Smith arrived at 5:30 p.m. on 12 April 1892."


def test_speech_normalizer_renders_clock_minutes_and_round_years():
    config = SpeechNormalizationConfig(enabled=True)

    assert normalize_for_speech("at 10:05 sharp", config) == "at ten oh five sharp"
    assert normalize_for_speech("at 13:00 hours", config) == "at thirteen o'clock hours"
    assert normalize_for_speech("in 2005 he left", config) == "in twenty oh five he left"
    assert normalize_for_speech("circa 1500 AD", config) == "circa fifteen hundred AD"
    assert normalize_for_speech("by 2000 it ended", config) == "by two thousand it ended"
