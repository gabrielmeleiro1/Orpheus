"""Shared helpers for OpenAI-compatible chat-completions providers."""

from __future__ import annotations

import json
import urllib.error as urlerror
import urllib.request as urlrequest
from typing import Any, Optional


def completion_token_field(model_name: str) -> str:
    """Choose the completion token field that matches known model families."""

    normalized = model_name.strip().lower()
    if (
        normalized.startswith("gpt-5")
        or normalized.startswith("o1")
        or normalized.startswith("o3")
        or normalized.startswith("o4")
    ):
        return "max_completion_tokens"
    return "max_tokens"


def supports_temperature_override(model_name: str) -> bool:
    """Return True when explicit temperature override is supported."""

    return completion_token_field(model_name) == "max_tokens"


def coerce_content_to_text(content: object) -> str:
    """Normalize string or structured content payloads into plain text."""

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
                continue

            if not isinstance(part, dict):
                continue

            text_value = part.get("text")
            if isinstance(text_value, str):
                parts.append(text_value)
                continue
            if isinstance(text_value, dict):
                nested_value = text_value.get("value")
                if isinstance(nested_value, str):
                    parts.append(nested_value)
                    continue

            nested_content = part.get("content")
            nested_text = coerce_content_to_text(nested_content)
            if nested_text:
                parts.append(nested_text)

        return "".join(parts)

    return ""


def extract_choice_text(choice: object) -> str:
    """Extract text from common OpenAI-compatible choice payload shapes."""

    if not isinstance(choice, dict):
        return ""

    message = choice.get("message")
    if isinstance(message, dict):
        text = coerce_content_to_text(message.get("content"))
        if text:
            return text

    text = coerce_content_to_text(choice.get("text"))
    if text:
        return text

    delta = choice.get("delta")
    if isinstance(delta, dict):
        text = coerce_content_to_text(delta.get("content"))
        if text:
            return text

    return ""


def parse_chat_response_text(
    response_body: str,
    *,
    empty_choices_message: str,
    empty_content_message: str,
    parse_error_prefix: str,
) -> str:
    """Parse a chat-completions style response body into plain text."""

    try:
        data = json.loads(response_body)
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(empty_choices_message)

        content = extract_choice_text(choices[0])
        if not content:
            raise RuntimeError(empty_content_message)
        return content.strip()
    except (json.JSONDecodeError, RuntimeError) as exc:
        raise RuntimeError(f"{parse_error_prefix}: {exc}") from exc


def build_chat_completions_headers(api_key: str) -> dict[str, str]:
    """Build standard headers for OpenAI-compatible chat-completions requests."""

    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept-Encoding": "identity",
    }


def build_chat_completions_request(
    *,
    base_url: str,
    api_key: str,
    payload: dict[str, Any],
) -> urlrequest.Request:
    """Build a urllib request for a chat-completions POST."""

    normalized_base_url = (base_url or "").strip().rstrip("/")
    return urlrequest.Request(
        url=f"{normalized_base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=build_chat_completions_headers(api_key),
        method="POST",
    )


def post_chat_completions_via_urlopen(
    *,
    base_url: str,
    api_key: str,
    payload: dict[str, Any],
    timeout_seconds: int,
    http_error_prefix: str,
    connection_error_prefix: str,
    ssl_context: Optional[Any] = None,
) -> str:
    """Send a sync chat-completions request with consistent error formatting."""

    request = build_chat_completions_request(
        base_url=base_url,
        api_key=api_key,
        payload=payload,
    )
    try:
        with urlrequest.urlopen(
            request,
            timeout=timeout_seconds,
            context=ssl_context,
        ) as response:
            return response.read().decode("utf-8")
    except urlerror.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        raise RuntimeError(f"{http_error_prefix} HTTP {exc.code}: {detail}") from exc
    except urlerror.URLError as exc:
        raise RuntimeError(f"{connection_error_prefix}: {exc}") from exc
