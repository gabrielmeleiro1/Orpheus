from modules.tts.speech_normalizer import SpeechNormalizationConfig, normalize_for_speech


def test_speech_normalizer_prepares_tts_only_text_without_rewriting_source():
    text = "Dr. Smith arrived at 5:30 p.m. on 12 April 1892."

    spoken = normalize_for_speech(text, SpeechNormalizationConfig(enabled=True))

    assert spoken == "Doctor Smith arrived at five thirty P.M. on twelve April eighteen ninety two."
    assert text == "Dr. Smith arrived at 5:30 p.m. on 12 April 1892."
