import json

import pytest

from modules.tts import cleaner_api


@pytest.mark.parametrize("raw", ["null", "[]", "1", '"text"', '{"choices":1}', '{"choices":{"unexpected":1}}'])
def test_malformed_chat_shape_raises_public_runtime_error(raw):
    with pytest.raises(RuntimeError, match="parse failed"):
        cleaner_api.parse_api_response_text(raw)


@pytest.mark.parametrize("content", [" ", "\n\t", [{"type": "text", "text": " \n "}]])
def test_whitespace_only_chat_content_is_rejected(content):
    raw = json.dumps({"choices": [{"message": {"content": content}}]})
    with pytest.raises(RuntimeError, match="empty content"):
        cleaner_api.parse_api_response_text(raw)


def test_malformed_json_has_public_parse_error():
    with pytest.raises(RuntimeError, match="parse failed"):
        cleaner_api.parse_api_response_text("{")


def test_structured_response_text_is_trimmed_without_loss():
    raw = json.dumps({"choices": [{"message": {"content": [{"text": " sample "}]}}]})
    assert cleaner_api.parse_api_response_text(raw) == "sample"
