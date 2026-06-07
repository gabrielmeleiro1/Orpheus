"""Transport orchestration helpers for the TextCleaner facade."""

from __future__ import annotations

from typing import Any

from . import cleaner_api as cleaner_api_backend
from .api_rate_limit import acquire_global_limiter_async
from modules.async_bridge import run_coroutine_sync


async def generate_api_response_async(
    cleaner: Any,
    prompt: str,
    *,
    http_error_cls: type[Exception],
    aiohttp_module: Any = None,
    api_session: Any = None,
) -> str:
    """Resolve API runtime, perform the request, and parse the response."""
    base_url, api_key, model_name = cleaner_api_backend.resolve_api_runtime(cleaner.config)
    payload = cleaner_api_backend.build_api_payload(
        prompt=prompt,
        model_name=model_name,
        max_tokens=cleaner.config.max_tokens,
        temperature=cleaner.config.temperature,
    )
    ssl_context = cleaner._build_api_ssl_context()  # noqa: SLF001
    async with acquire_global_limiter_async(cleaner.config.api_global_limiter):
        response_body = await cleaner._post_api_chat_completions_with_retry_async(  # noqa: SLF001
            base_url=base_url,
            api_key=api_key,
            payload=payload,
            ssl_context=ssl_context,
            aiohttp_module=aiohttp_module,
            api_session=api_session,
        )
    return cleaner._parse_api_response_text(response_body)  # noqa: SLF001


def generate_api_response(
    cleaner: Any,
    prompt: str,
    *,
    http_error_cls: type[Exception],
    aiohttp_module: Any = None,
) -> str:
    """Synchronous wrapper around the async API transport flow."""
    return run_coroutine_sync(
        generate_api_response_async(
            cleaner,
            prompt,
            http_error_cls=http_error_cls,
            aiohttp_module=aiohttp_module,
        )
    )
