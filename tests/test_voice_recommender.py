"""Tests for voice recommendation API payload compatibility."""

from __future__ import annotations

import json
import urllib.error as urlerror

import pytest

from modules.app.voice_recommender import VoiceRecommender
from modules.ingestion.models import Chapter as IngestionChapter
from modules.ingestion.models import Document as IngestionDocument


def _stubbed_recommender(model: str, monkeypatch) -> tuple[VoiceRecommender, dict]:
    monkeypatch.setenv("VOICE_API_KEY_TEST", "test-key")

    recommender = VoiceRecommender(
        api_base_url="https://example.test",
        api_key_env="VOICE_API_KEY_TEST",
        api_model=model,
    )

    captured: dict = {}

    def fake_post_chat_completions(*, payload, api_key):
        captured["payload"] = payload
        captured["api_key"] = api_key
        return json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "book_type": "general",
                                    "tone": "balanced",
                                    "recommended_voice_id": "af_alloy",
                                    "recommended_voice_style": "neutral long-form narration",
                                    "reasoning": "Clear and steady.",
                                }
                            )
                        }
                    }
                ]
            }
        )

    recommender._post_chat_completions = fake_post_chat_completions  # type: ignore[method-assign]
    return recommender, captured


def test_gpt5_models_omit_temperature_override(monkeypatch) -> None:
    recommender, captured = _stubbed_recommender("gpt-5-nano", monkeypatch)

    recommender._request_model_suggestion(
        book_sample="Sample text",
        voices=[{"id": "af_alloy", "name": "Alloy", "style_tags": []}],
    )

    payload = captured["payload"]
    assert "temperature" not in payload
    assert payload["max_completion_tokens"] == 600


def test_non_gpt5_models_keep_temperature_override(monkeypatch) -> None:
    recommender, captured = _stubbed_recommender("gpt-4o-mini", monkeypatch)

    recommender._request_model_suggestion(
        book_sample="Sample text",
        voices=[{"id": "af_alloy", "name": "Alloy", "style_tags": []}],
    )

    payload = captured["payload"]
    assert payload["temperature"] == 0.2
    assert payload["max_tokens"] == 600


def test_plain_text_key_value_response_is_parsed(monkeypatch) -> None:
    monkeypatch.setenv("VOICE_API_KEY_TEST", "test-key")
    recommender = VoiceRecommender(
        api_base_url="https://example.test",
        api_key_env="VOICE_API_KEY_TEST",
        api_model="gpt-5-nano",
    )

    def fake_post_chat_completions(*, payload, api_key):  # noqa: ANN001
        _ = (payload, api_key)
        return json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "book type: general\n"
                                "tone: balanced\n"
                                "recommended voice: af_alloy\n"
                                "recommended style: neutral long-form narration\n"
                                "reason: clear and consistent voice."
                            )
                        }
                    }
                ]
            }
        )

    recommender._post_chat_completions = fake_post_chat_completions  # type: ignore[method-assign]
    parsed = recommender._request_model_suggestion(
        book_sample="Sample text",
        voices=[{"id": "af_alloy", "name": "Alloy", "style_tags": []}],
    )

    assert parsed["book_type"] == "general"
    assert parsed["tone"] == "balanced"
    assert parsed["recommended_voice_id"] == "af_alloy"
    assert parsed["recommended_voice_style"] == "neutral long-form narration"
    assert parsed["reasoning"] == "clear and consistent voice."


def test_structured_content_response_is_parsed(monkeypatch) -> None:
    monkeypatch.setenv("VOICE_API_KEY_TEST", "test-key")
    recommender = VoiceRecommender(
        api_base_url="https://example.test",
        api_key_env="VOICE_API_KEY_TEST",
        api_model="gpt-5-nano",
    )

    def fake_post_chat_completions(*, payload, api_key):  # noqa: ANN001
        _ = (payload, api_key)
        return json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {
                                    "text": {
                                        "value": json.dumps(
                                            {
                                                "book_type": "general",
                                                "tone": "balanced",
                                                "recommended_voice_id": "af_alloy",
                                                "recommended_voice_style": "neutral long-form narration",
                                                "reasoning": "Clear and steady.",
                                            }
                                        )
                                    }
                                }
                            ]
                        }
                    }
                ]
            }
        )

    recommender._post_chat_completions = fake_post_chat_completions  # type: ignore[method-assign]
    parsed = recommender._request_model_suggestion(
        book_sample="Sample text",
        voices=[{"id": "af_alloy", "name": "Alloy", "style_tags": []}],
    )

    assert parsed["book_type"] == "general"
    assert parsed["tone"] == "balanced"
    assert parsed["recommended_voice_id"] == "af_alloy"
    assert parsed["recommended_voice_style"] == "neutral long-form narration"
    assert parsed["reasoning"] == "Clear and steady."


def test_openai_compat_helpers_are_shared_for_model_family_rules() -> None:
    from modules.openai_compat import completion_token_field, supports_temperature_override

    assert completion_token_field("gpt-5-nano") == "max_completion_tokens"
    assert completion_token_field("gpt-4o-mini") == "max_tokens"
    assert supports_temperature_override("gpt-5-nano") is False
    assert supports_temperature_override("gpt-4o-mini") is True


def test_openai_compat_sync_transport_formats_http_and_connection_errors(monkeypatch) -> None:
    from modules.openai_compat import post_chat_completions_via_urlopen

    class _FakeHttpError(urlerror.HTTPError):
        def __init__(self):
            super().__init__(
                url="https://example.test/chat/completions",
                code=429,
                msg="Too Many Requests",
                hdrs=None,
                fp=None,
            )

    monkeypatch.setattr(
        "modules.openai_compat.urlrequest.urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(_FakeHttpError()),
    )

    try:
        post_chat_completions_via_urlopen(
            base_url="https://example.test",
            api_key="test-key",
            payload={"model": "gpt-5-nano", "messages": []},
            timeout_seconds=5,
            http_error_prefix="Voice suggestion API",
            connection_error_prefix="Voice suggestion API connection failed",
        )
    except RuntimeError as exc:
        assert "Voice suggestion API HTTP 429" in str(exc)
    else:
        raise AssertionError("expected HTTP error to be surfaced")


def test_extract_book_sample_supports_txt_sources(tmp_path) -> None:
    source_path = tmp_path / "notes.txt"
    source_path.write_text(
        "Short note.\n\nAnother paragraph with more detail.\n",
        encoding="utf-8",
    )
    recommender = VoiceRecommender(
        api_base_url="https://example.test",
        api_key_env="VOICE_API_KEY_TEST",
        api_model="gpt-5-nano",
    )

    sample = recommender._extract_book_sample(
        source_path=source_path,
        max_units=3,
        max_chars=5000,
    )

    assert sample["unit"] == "sections"
    assert sample["units_analyzed"] == 1
    assert sample["total_units"] == 1
    assert "Short note." in sample["excerpt"]


def test_extract_book_sample_supports_markdown_sources(tmp_path) -> None:
    source_path = tmp_path / "guide.md"
    source_path.write_text(
        "# Guide\n\nOverview text.\n\n## Install\n\nInstall steps.\n",
        encoding="utf-8",
    )
    recommender = VoiceRecommender(
        api_base_url="https://example.test",
        api_key_env="VOICE_API_KEY_TEST",
        api_model="gpt-5-nano",
    )

    sample = recommender._extract_book_sample(
        source_path=source_path,
        max_units=3,
        max_chars=5000,
    )

    assert sample["unit"] == "sections"
    assert sample["units_analyzed"] == 2
    assert sample["total_units"] == 2
    assert "Overview text." in sample["excerpt"]
    assert "Install steps." in sample["excerpt"]


@pytest.mark.parametrize(
    ("suffix", "patch_target", "validate_target"),
    [
        (".txt", "modules.ingestion.text_parser.TextParser.parse", None),
        (".md", "modules.ingestion.markdown_parser.MarkdownParser.parse", None),
        (".epub", "modules.ingestion.epub_parser.EPUBParser.parse", "modules.ingestion.epub_parser.validate_epub"),
        (".pdf", "modules.ingestion.pdf_parser.PDFParser.parse", "modules.ingestion.pdf_parser.validate_pdf"),
    ],
)
def test_extract_book_sample_reuses_ingestion_document_contract_for_all_source_types(
    monkeypatch,
    tmp_path,
    suffix,
    patch_target,
    validate_target,
) -> None:
    source_path = tmp_path / f"sample{suffix}"
    source_path.write_text("ignored", encoding="utf-8")
    monkeypatch.setenv("VOICE_API_KEY_TEST", "test-key")

    recommender = VoiceRecommender(
        api_base_url="https://example.test",
        api_key_env="VOICE_API_KEY_TEST",
        api_model="gpt-5-nano",
    )

    document = IngestionDocument(
        title="Shared Sample",
        author=None,
        chapters=[
            IngestionChapter(number=1, title="One", content="First shared excerpt."),
            IngestionChapter(number=2, title="Two", content="Second shared excerpt."),
        ],
        raw_markdown="First shared excerpt.\n\nSecond shared excerpt.",
    )

    if validate_target is not None:
        monkeypatch.setattr(validate_target, lambda *_args, **_kwargs: True)
    monkeypatch.setattr(patch_target, lambda *_args, **_kwargs: document)

    if suffix == ".pdf":
        monkeypatch.setattr("fitz.open", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("direct PDF sampling should not be used")))

    sample = recommender._extract_book_sample(
        source_path=source_path,
        max_units=3,
        max_chars=5000,
    )

    assert sample["units_analyzed"] == 2
    assert sample["total_units"] == 2
    assert "First shared excerpt." in sample["excerpt"]
    assert "Second shared excerpt." in sample["excerpt"]
