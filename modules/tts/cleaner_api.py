"""API backend transport + parsing helpers for TextCleaner."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import urllib.error as urlerror
import urllib.request as urlrequest
from typing import Any, Optional

from modules.openai_compat import (
    build_chat_completions_headers,
    build_chat_completions_request,
    coerce_content_to_text as compat_coerce_content_to_text,
    completion_token_field,
    parse_chat_response_text,
    supports_temperature_override as compat_supports_temperature_override,
)
from modules.tts.cleaner_config import DEFAULT_API_BASE_URL

logger = logging.getLogger(__name__)


def _default_aiohttp_module() -> Any:
    try:
        import aiohttp
    except ImportError:  # pragma: no cover - runtime dependency guard
        return None
    return aiohttp


def api_token_limit_field(model_name: str) -> str:
    """Choose completion token field that matches known model families."""
    return completion_token_field(model_name)


def supports_temperature_override(model_name: str) -> bool:
    """Return True when explicit temperature is supported by model family."""
    return compat_supports_temperature_override(model_name)


def coerce_content_to_text(content: object) -> str:
    """Normalize API content payloads (string or structured list) into plain text."""
    return compat_coerce_content_to_text(content)


def parse_api_response_text(response_body: str) -> str:
    """Parse and validate chat-completions style response body."""
    return parse_chat_response_text(
        response_body,
        empty_choices_message="Cleaner API returned no choices",
        empty_content_message="Cleaner API returned empty content",
        parse_error_prefix="Cleaner API response parse failed",
    )


def parse_openai_model_ids(response_body: str) -> list[str]:
    """Parse an OpenAI-compatible /models response into model id strings."""
    try:
        payload = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"OpenAI models response parse failed: {exc}") from exc

    model_items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(model_items, list):
        return []

    seen: set[str] = set()
    model_ids: list[str] = []
    for item in model_items:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str):
            continue
        normalized = model_id.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        model_ids.append(normalized)
    return model_ids


def http_error_detail(exc: urlerror.HTTPError) -> str:
    """Decode HTTP error payload safely for diagnostics."""
    return exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)


def swap_token_field_payload(
    payload: dict[str, object],
    *,
    detail: str,
    status_code: int,
    max_tokens: int,
) -> Optional[dict[str, object]]:
    """Swap token limit field for provider/model compatibility retries."""
    if status_code != 400:
        return None

    normalized_detail = detail.lower()
    mentions_token_mismatch = (
        "max_tokens" in normalized_detail
        and "max_completion_tokens" in normalized_detail
        and ("unsupported" in normalized_detail or "not supported" in normalized_detail)
    )
    if not mentions_token_mismatch:
        return None

    retry_payload = dict(payload)
    if "max_tokens" in retry_payload:
        retry_payload.pop("max_tokens", None)
        retry_payload["max_completion_tokens"] = max_tokens
        return retry_payload
    if "max_completion_tokens" in retry_payload:
        retry_payload.pop("max_completion_tokens", None)
        retry_payload["max_tokens"] = max_tokens
        return retry_payload
    return None


def raise_api_transport_error(exc: Exception) -> None:
    """Raise user-facing network/TLS errors for API requests."""
    if "CERTIFICATE_VERIFY_FAILED" in str(exc).upper():
        raise RuntimeError(
            "Cleaner API TLS verification failed. "
            "Set CLEANER_API_CA_BUNDLE to a PEM CA bundle that trusts your network, "
            "or set CLEANER_API_INSECURE_SSL=1 only for temporary troubleshooting."
        ) from exc
    raise RuntimeError(f"Cleaner API connection failed: {exc}") from exc


def urlopen_is_patched() -> bool:
    """Return True when urllib transport has been monkeypatched."""
    urlopen_fn = getattr(urlrequest, "urlopen", None)
    if not callable(urlopen_fn):
        return False
    return getattr(urlopen_fn, "__module__", "urllib.request") != "urllib.request"


def build_api_ssl_context(config: Any) -> ssl.SSLContext:
    """Build SSL context for cleaner API calls with optional custom CA support."""
    context = ssl.create_default_context()

    insecure_env = os.getenv("CLEANER_API_INSECURE_SSL")
    insecure_from_env = (
        insecure_env is not None and insecure_env.strip().lower() in {"1", "true", "yes", "on"}
    )
    allow_insecure = config.api_allow_insecure_ssl or insecure_from_env
    if allow_insecure:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    ca_bundle = (config.api_ca_bundle or os.getenv("CLEANER_API_CA_BUNDLE", "")).strip()
    if ca_bundle:
        try:
            context.load_verify_locations(cafile=ca_bundle)
        except OSError as exc:
            raise RuntimeError(f"Cleaner API CA bundle load failed ({ca_bundle}): {exc}") from exc

    return context


def build_openai_models_request(
    *,
    api_key: str,
    base_url: str = DEFAULT_API_BASE_URL,
) -> urlrequest.Request:
    """Build the OpenAI-compatible /models request for API-key model discovery."""
    normalized_base_url = (base_url or DEFAULT_API_BASE_URL).strip().rstrip("/")
    return urlrequest.Request(
        url=f"{normalized_base_url}/models",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        },
        method="GET",
    )


def fetch_openai_model_ids(
    *,
    api_key: str,
    base_url: str = DEFAULT_API_BASE_URL,
    timeout_seconds: int = 30,
    ssl_context: Optional[ssl.SSLContext] = None,
) -> list[str]:
    """Fetch model ids from OpenAI's /models endpoint using a pasted API key."""
    normalized_key = (api_key or "").strip()
    if not normalized_key:
        raise ValueError("OpenAI API key is required")

    request = build_openai_models_request(
        api_key=normalized_key,
        base_url=base_url,
    )
    try:
        with urlrequest.urlopen(
            request,
            timeout=timeout_seconds,
            context=ssl_context,
        ) as response:
            return parse_openai_model_ids(response.read().decode("utf-8"))
    except urlerror.HTTPError as exc:
        detail = http_error_detail(exc)
        raise RuntimeError(f"OpenAI models HTTP {exc.code}: {detail}") from exc
    except urlerror.URLError as exc:
        raise RuntimeError(f"OpenAI models connection failed: {exc}") from exc


def post_api_chat_completions_via_urlopen(
    *,
    config: Any,
    base_url: str,
    api_key: str,
    payload: dict[str, object],
    ssl_context: ssl.SSLContext,
    http_error_cls: type[Exception],
) -> str:
    """Compatibility fallback for tests that monkeypatch urllib transport."""
    request = build_chat_completions_request(
        base_url=base_url,
        api_key=api_key,
        payload=payload,
    )
    try:
        with urlrequest.urlopen(
            request,
            timeout=config.api_timeout_seconds,
            context=ssl_context,
        ) as response:
            return response.read().decode("utf-8")
    except urlerror.HTTPError as exc:
        detail = http_error_detail(exc)
        raise http_error_cls(exc.code, detail) from exc
    except urlerror.URLError as exc:
        raise_api_transport_error(exc)


async def post_api_chat_completions_async(
    *,
    config: Any,
    base_url: str,
    api_key: str,
    payload: dict[str, object],
    ssl_context: ssl.SSLContext,
    http_error_cls: type[Exception],
    aiohttp_module: Any = None,
    api_session: Any = None,
) -> str:
    """POST chat completions payload using aiohttp."""
    if urlopen_is_patched():
        return post_api_chat_completions_via_urlopen(
            config=config,
            base_url=base_url,
            api_key=api_key,
            payload=payload,
            ssl_context=ssl_context,
            http_error_cls=http_error_cls,
        )

    aiohttp_runtime = (
        _default_aiohttp_module() if aiohttp_module is None else aiohttp_module
    )

    if aiohttp_runtime is None:
        raise RuntimeError(
            "aiohttp is required for API cleaner backend. Install aiohttp to continue."
        )

    async def _post_with_session(session: Any) -> str:
        async with session.post(
            f"{base_url}/chat/completions",
            json=payload,
            headers=build_chat_completions_headers(api_key),
            ssl=ssl_context,
        ) as response:
            response_body = await response.text()
            if response.status >= 400:
                raise http_error_cls(response.status, response_body)
            return response_body

    try:
        if api_session is not None:
            return await _post_with_session(api_session)

        timeout = aiohttp_runtime.ClientTimeout(total=config.api_timeout_seconds)
        async with aiohttp_runtime.ClientSession(timeout=timeout) as session:
            return await _post_with_session(session)
    except Exception as exc:
        if exc.__class__ is http_error_cls or isinstance(exc, http_error_cls):
            raise
        if aiohttp_runtime is not None and isinstance(exc, aiohttp_runtime.ClientError):
            raise_api_transport_error(exc)
        if isinstance(exc, asyncio.TimeoutError):
            raise RuntimeError(
                f"Cleaner API request timed out after {config.api_timeout_seconds}s"
            ) from exc
        raise


async def post_api_chat_completions_with_retry_async(
    *,
    config: Any,
    base_url: str,
    api_key: str,
    payload: dict[str, object],
    ssl_context: ssl.SSLContext,
    http_error_cls: type[Exception],
    max_tokens: int,
    aiohttp_module: Any = None,
    api_session: Any = None,
) -> str:
    """POST chat completions payload and retry token field once on HTTP 400."""
    try:
        return await post_api_chat_completions_async(
            config=config,
            base_url=base_url,
            api_key=api_key,
            payload=payload,
            ssl_context=ssl_context,
            http_error_cls=http_error_cls,
            aiohttp_module=aiohttp_module,
            api_session=api_session,
        )
    except Exception as exc:
        if not isinstance(exc, http_error_cls):
            raise
        retry_payload = swap_token_field_payload(
            payload,
            detail=getattr(exc, "detail", str(exc)),
            status_code=getattr(exc, "status_code", 0),
            max_tokens=max_tokens,
        )
        if retry_payload is None:
            raise RuntimeError(
                f"Cleaner API HTTP {getattr(exc, 'status_code', 0)}: {getattr(exc, 'detail', str(exc))}"
            ) from exc

        try:
            return await post_api_chat_completions_async(
                config=config,
                base_url=base_url,
                api_key=api_key,
                payload=retry_payload,
                ssl_context=ssl_context,
                http_error_cls=http_error_cls,
                aiohttp_module=aiohttp_module,
                api_session=api_session,
            )
        except Exception as retry_exc:
            if not isinstance(retry_exc, http_error_cls):
                raise
            raise RuntimeError(
                f"Cleaner API HTTP {getattr(retry_exc, 'status_code', 0)}: {getattr(retry_exc, 'detail', str(retry_exc))}"
            ) from retry_exc


def build_api_payload(
    *,
    prompt: str,
    model_name: str,
    max_tokens: int,
    temperature: float,
) -> dict[str, object]:
    token_field = api_token_limit_field(model_name)
    payload: dict[str, object] = {
        "model": model_name,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You fix OCR/PDF extraction artifacts in text for TTS. "
                    "Assume each input should read naturally as a spoken paragraph. "
                    "Only remove stray formatting, rejoin broken words, remove inline numeric references like [5], "
                    "and fix obviously garbled OCR words/punctuation/casing while preserving meaning. "
                    "If unsure, return the input unchanged. "
                    "Preserve literary repetition, emphasis, dialect, unusual phrasing, epigraphs, poems, "
                    "titles, foreign-language text, roman numerals, and all-caps emphasis unless clearly OCR damage. "
                    "Do NOT rewrite, expand abbreviations, or convert numbers to words. "
                    "Return only the corrected text."
                ),
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        token_field: max_tokens,
    }
    if supports_temperature_override(model_name):
        payload["temperature"] = temperature
    return payload


def resolve_api_runtime(config: Any) -> tuple[str, str, str]:
    """Resolve and validate base_url, api_key, model_name."""
    base_url = (config.api_base_url or "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError("Cleaner API base URL is empty")

    api_key = str(getattr(config, "api_key", "") or "").strip()
    if not api_key:
        key_env = str(getattr(config, "api_key_env", "") or "").strip() or "OPENAI_API_KEY"
        api_key = os.getenv(key_env)
        if not api_key:
            raise RuntimeError(f"Missing API key in environment variable: {key_env}")

    model_name = (config.api_model or config.model_name).strip()
    if not model_name:
        raise RuntimeError("Cleaner API model name is empty")

    return base_url, api_key, model_name


class CleanerAPIStrategy:
    """API cleaner backend strategy for TextCleaner facade."""

    backend = "api"

    async def _clean_batch_bounded(
        self,
        cleaner: Any,
        texts: list[str],
        semaphore: asyncio.Semaphore,
        *,
        api_session: Any = None,
    ) -> list[Any]:
        results: list[Any] = [None] * len(texts)
        iterator = iter(enumerate(texts))
        worker_count = min(len(texts), cleaner._get_api_batch_concurrency())

        async def worker() -> None:
            for idx, text in iterator:
                results[idx] = await cleaner._clean_with_trace_api_async(
                    text,
                    semaphore,
                    api_session=api_session,
                )

        await asyncio.gather(*(worker() for _ in range(worker_count)))
        return results

    def load(self, cleaner: Any) -> None:
        if cleaner._loaded:
            return
        cleaner._loaded = True
        logger.info(
            "Text cleaner API backend ready: %s (%s)",
            cleaner.config.api_model or cleaner.config.model_name,
            cleaner.config.api_base_url,
        )

    async def clean_batch_with_trace_async(self, cleaner: Any, texts: list[str]) -> list[Any]:
        if not texts:
            return []
        semaphore = asyncio.Semaphore(cleaner._get_api_batch_concurrency())
        aiohttp_runtime = self._get_aiohttp_runtime(cleaner)
        if urlopen_is_patched() or aiohttp_runtime is None:
            return await self._clean_batch_bounded(
                cleaner,
                texts,
                semaphore,
            )

        timeout = aiohttp_runtime.ClientTimeout(total=cleaner.config.api_timeout_seconds)
        async with aiohttp_runtime.ClientSession(timeout=timeout) as api_session:
            return await self._clean_batch_bounded(
                cleaner,
                texts,
                semaphore,
                api_session=api_session,
            )

    def generate_llm_response(self, cleaner: Any, prompt: str) -> str:
        return cleaner._generate_api_response(prompt)

    @staticmethod
    def _get_aiohttp_runtime(cleaner: Any) -> Any:
        getter = getattr(cleaner, "_get_api_aiohttp_module", None)
        if callable(getter):
            return getter()
        return _default_aiohttp_module()

    def unload(self, cleaner: Any, *, keep_in_cache: bool = True) -> None:
        _ = keep_in_cache
        if not cleaner._loaded:
            return
        cleaner._loaded = False
        logger.info("Text cleaner API backend released")

    def clear_cache(self, cleaner: Any) -> None:
        _ = cleaner
        logger.info("Cleaner API backend has no local cache to clear")
