"""Shared helpers for OpenAI-compatible chat-completions providers."""

from __future__ import annotations

import json
import urllib.error as urlerror
import urllib.request as urlrequest
from urllib.parse import urlsplit
from typing import Any, Optional


def _request_origin(url: str) -> tuple[str, str | None, int | None]:
    parts = urlsplit(url)
    port = parts.port if parts.port is not None else {"http": 80, "https": 443}.get(parts.scheme)
    return parts.scheme, parts.hostname, port


class _AuthenticatedRedirectHandler(urlrequest.HTTPRedirectHandler):
    """Keep provider credentials on their original HTTP origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and _request_origin(req.full_url) != _request_origin(newurl):
            for name, _ in redirected.header_items():
                if name.lower() in {"authorization", "proxy-authorization"}:
                    redirected.remove_header(name)
        return redirected


def open_authenticated_request(request, *, timeout, context=None):
    """Open a provider request without forwarding credentials to other origins."""
    # Keep the existing injected-transport seam used by offline callers/tests.
    # Production urllib needs a per-request opener: installing a global opener
    # would change unrelated concurrent downloads and requests.
    if getattr(urlrequest.urlopen, "__module__", "urllib.request") != "urllib.request":
        return urlrequest.urlopen(request, timeout=timeout, context=context)
    opener = urlrequest.build_opener(
        _AuthenticatedRedirectHandler(), urlrequest.HTTPSHandler(context=context),
    )
    return opener.open(request, timeout=timeout)


def redact_api_error(detail: object, api_key: str) -> str:
    """Remove the active credential before provider diagnostics leave transport."""
    message = str(detail)
    # Providers can trim pasted/environment credentials before echoing them.
    # Redact both spellings, preserving the request's existing interpretation.
    for credential in (api_key, api_key.strip()):
        if credential:
            message = message.replace(credential, "[REDACTED]")
    return message


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
        if not isinstance(data, dict):
            raise RuntimeError("Expected a JSON object")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError(empty_choices_message)

        content = extract_choice_text(choices[0]).strip()
        if not content:
            raise RuntimeError(empty_content_message)
        return content
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
        with open_authenticated_request(
            request,
            timeout=timeout_seconds,
            context=ssl_context,
        ) as response:
            return response.read().decode("utf-8")
    except UnicodeDecodeError:
        raise RuntimeError(f"{http_error_prefix}: provider returned invalid UTF-8") from None
    except TimeoutError:
        raise RuntimeError(f"{connection_error_prefix}: provider response timed out") from None
    except urlerror.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        except TimeoutError:
            detail = "provider response body timed out"
        detail = redact_api_error(detail, api_key)
        raise RuntimeError(f"{http_error_prefix} HTTP {exc.code}: {detail}") from None
    except urlerror.URLError as exc:
        detail = redact_api_error(exc, api_key)
        raise RuntimeError(f"{connection_error_prefix}: {detail}") from None
