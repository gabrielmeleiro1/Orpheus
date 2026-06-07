"""Voice catalogs shared across TTS strategies and legacy adapters."""

from __future__ import annotations

import re

# Sourced from the Kokoro model repos (mlx-community/Kokoro-82M-<quantization>),
# where each ID maps to voices/<id>.pt.
KOKORO_VOICE_IDS: tuple[str, ...] = (
    "af_alloy",
    "af_aoede",
    "af_bella",
    "af_heart",
    "af_jessica",
    "af_kore",
    "af_nicole",
    "af_nova",
    "af_river",
    "af_sarah",
    "af_sky",
    "am_adam",
    "am_echo",
    "am_eric",
    "am_fenrir",
    "am_liam",
    "am_michael",
    "am_onyx",
    "am_puck",
    "am_santa",
    "bf_alice",
    "bf_emma",
    "bf_isabella",
    "bf_lily",
    "bm_daniel",
    "bm_fable",
    "bm_george",
    "bm_lewis",
)

_LOCALE_BY_PREFIX: dict[str, tuple[str, str]] = {
    "a": ("en-US", "en-US"),
    "b": ("en-GB", "en-GB"),
    "e": ("es", "es"),
    "f": ("fr-FR", "fr-FR"),
    "h": ("hi", "hi-IN"),
    "i": ("it", "it-IT"),
    "j": ("ja", "ja-JP"),
    "p": ("pt-BR", "pt-BR"),
    "z": ("zh", "zh-CN"),
}

_LANG_LABEL_BY_PREFIX: dict[str, str] = {
    "a": "American English",
    "b": "British English",
    "e": "Spanish",
    "f": "French",
    "h": "Hindi",
    "i": "Italian",
    "j": "Japanese",
    "p": "Brazilian Portuguese",
    "z": "Mandarin Chinese",
}

_SAMPLE_TEXT_BY_PREFIX: dict[str, str] = {
    "a": "A few light taps upon the pane made him turn to the window.",
    "b": "A thin rain swept over the moor as dusk approached.",
    "e": "Esta es una vista previa de voz para narracion en espanol.",
    "f": "Ceci est un apercu vocal pour la narration en francais.",
    "h": "Yah Hindi narration ke liye ek awaz preview hai.",
    "i": "Questa e un anteprima vocale per la narrazione in italiano.",
    "j": "Kore wa nihongo no onsei purebyu desu.",
    "p": "Esta e uma previa de voz para narracao em portugues.",
    "z": "Zhe shi yige zhongwen peiyin shili.",
}

_GENDER_BY_CODE: dict[str, str] = {"f": "female", "m": "male"}

# Keep curated descriptions/styles for legacy favorites.
_VOICE_OVERRIDES: dict[str, dict[str, object]] = {
    "am_adam": {
        "name": "Adam",
        "description": "Deep, authoritative American male voice",
        "style_tags": ["narrative", "authoritative"],
        "sample_text": "A few light taps upon the pane made him turn to the window.",
    },
    "af_bella": {
        "name": "Bella",
        "description": "Warm, conversational American female voice",
        "style_tags": ["conversational", "warm"],
        "sample_text": "The quick brown fox jumps over the lazy dog.",
    },
    "am_michael": {
        "name": "Michael",
        "description": "Friendly, casual American male voice",
        "style_tags": ["casual", "friendly"],
        "sample_text": "Every good story starts with one clear sentence.",
    },
    "af_sarah": {
        "name": "Sarah",
        "description": "Professional, clear American female voice",
        "style_tags": ["clear", "professional"],
        "sample_text": "Please listen carefully to the following chapter summary.",
    },
    "bf_emma": {
        "name": "Emma",
        "description": "Refined, articulate British female voice",
        "style_tags": ["refined", "articulate"],
        "sample_text": "A thin rain swept over the moor as dusk approached.",
    },
    "bm_george": {
        "name": "George",
        "description": "Classic, distinguished British male voice",
        "style_tags": ["classic", "distinguished"],
        "sample_text": "The station clock struck twice as the train departed.",
    },
}


def _humanize_name(token: str) -> str:
    parts = [segment for segment in token.split("_") if segment]
    if not parts:
        return token
    return " ".join(segment.capitalize() for segment in parts)


def _default_voice_entry(voice_id: str) -> dict[str, object]:
    match = re.match(r"^([a-z])([fm])_([a-z0-9_]+)$", voice_id)
    if match:
        prefix, gender_code, raw_name = match.groups()
        language, locale = _LOCALE_BY_PREFIX.get(prefix, ("unknown", "unknown"))
        language_label = _LANG_LABEL_BY_PREFIX.get(prefix, "Unknown language")
        gender = _GENDER_BY_CODE.get(gender_code, "neutral")
        return {
            "id": voice_id,
            "name": _humanize_name(raw_name),
            "language": language,
            "locale": locale,
            "gender": gender,
            "description": f"{language_label} {gender} voice",
            "style_tags": ["standard"],
            "sample_text": _SAMPLE_TEXT_BY_PREFIX.get(prefix, "This is a Kokoro voice preview."),
            "is_multilingual": False,
        }

    return {
        "id": voice_id,
        "name": _humanize_name(voice_id),
        "language": "unknown",
        "locale": "unknown",
        "gender": "neutral",
        "description": "Kokoro voice",
        "style_tags": ["standard"],
        "sample_text": "This is a Kokoro voice preview.",
        "is_multilingual": False,
    }


def _build_kokoro_voice_catalog() -> list[dict[str, object]]:
    catalog: list[dict[str, object]] = []
    for voice_id in KOKORO_VOICE_IDS:
        entry = _default_voice_entry(voice_id)
        override = _VOICE_OVERRIDES.get(voice_id)
        if override:
            entry.update(override)
        catalog.append(entry)
    return catalog


KOKORO_VOICE_CATALOG: list[dict[str, object]] = _build_kokoro_voice_catalog()


def kokoro_voice_lookup() -> dict[str, str]:
    """Legacy lookup for engine adapters expecting id->description mapping."""
    return {str(entry["id"]): str(entry["description"]) for entry in KOKORO_VOICE_CATALOG}
