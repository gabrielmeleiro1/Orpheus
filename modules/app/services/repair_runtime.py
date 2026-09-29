"""Resolve and persist conversion runtime settings for repair jobs."""

from __future__ import annotations

import dataclasses
import json
import os
import re
from typing import Any, Iterable

from modules.app.config import AppConfig
from modules.app.services.conversion_runtime import (
    ConversionRuntimeOptions,
    NormalizedConversionRuntimeOptions,
)

PERSISTED_REPAIR_RUNTIME_FIELDS: frozenset[str] = frozenset(
    {
        "voice",
        "speed",
        "tts_engine",
        "tts_quantization",
        "tts_provider",
        "tts_device",
        "cleaner_model",
        "cleaner_backend",
        "cleaner_cuda_device",
        "cleaner_cuda_quantization",
        "cleaner_api_model",
        "tts_options",
    }
)

# Secrets are never persisted on book records, but a repair request may supply
# them explicitly (e.g. re-entering a cloud cleaner API key).
EXPLICIT_ONLY_REPAIR_RUNTIME_FIELDS: frozenset[str] = frozenset({"cleaner_api_key"})

_PERSISTED_CREDENTIAL_KEY_SUFFIXES: tuple[str, ...] = (
    "apikey",
    "token",
    "secret",
    "password",
    "authorization",
)


def _is_persisted_credential_key(key: object) -> bool:
    """Return whether a mapping key names a credential-like value."""
    normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", str(key)).casefold()
    segments = tuple(part for part in re.split(r"[_-]+", normalized) if part)
    compact = normalized.replace("_", "").replace("-", "")
    return any(
        compact == suffix or compact.endswith(suffix) or suffix in segments
        for suffix in _PERSISTED_CREDENTIAL_KEY_SUFFIXES
    )


def _strip_persisted_credentials(value: Any) -> Any:
    """Deep-copy JSON-like runtime options while dropping credential keys."""
    if isinstance(value, dict):
        return {
            key: _strip_persisted_credentials(item)
            for key, item in value.items()
            if not _is_persisted_credential_key(key)
        }
    if isinstance(value, list):
        return [_strip_persisted_credentials(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_strip_persisted_credentials(item) for item in value)
    return value


class RepairRuntimeUnavailableError(ValueError):
    """Raised when repair cannot infer narration settings safely."""


def serialize_persisted_runtime_options(
    runtime_options: ConversionRuntimeOptions,
) -> dict[str, Any]:
    """Return narration settings safe to store on a book record."""
    payload = {
        field: _strip_persisted_credentials(getattr(runtime_options, field))
        for field in PERSISTED_REPAIR_RUNTIME_FIELDS
    }
    return {key: value for key, value in payload.items() if value is not None}


def effective_runtime_options_for_persistence(
    *,
    request_options: ConversionRuntimeOptions | None,
    normalized: NormalizedConversionRuntimeOptions,
) -> ConversionRuntimeOptions:
    """Build persistable options from the effective pipeline configuration."""
    request_options = request_options or ConversionRuntimeOptions()
    tts_runtime = normalized.tts_runtime
    return ConversionRuntimeOptions(
        voice=normalized.voice or request_options.voice,
        speed=float(normalized.speed),
        tts_engine=normalized.tts_engine,
        tts_quantization=normalized.tts_quantization,
        tts_provider=getattr(tts_runtime, "provider", None),
        tts_device=getattr(tts_runtime, "device", None),
        cleaner_model=normalized.cleaner_model,
        cleaner_backend=normalized.cleaner_backend,
        cleaner_cuda_device=normalized.cleaner_cuda_device,
        cleaner_cuda_quantization=normalized.cleaner_cuda_quantization,
        cleaner_api_model=normalized.cleaner_api_model,
        cleaner_api_key=None,
        tts_options=request_options.tts_options,
        parallel_worker_mode=None,
        enable_parallel=None,
        max_parallel_chapters=None,
    )


def legacy_repair_runtime_options_from_app_config(
    app_config: AppConfig,
) -> ConversionRuntimeOptions:
    """Safe fallback for pre-v7 books that never persisted conversion settings."""
    preferences = app_config.setup_preferences()
    return ConversionRuntimeOptions(
        voice=app_config.default_voice,
        speed=float(app_config.default_speed),
        tts_engine=app_config.tts_engine,
        tts_quantization=app_config.tts_quantization,
        tts_provider=app_config.tts_provider,
        tts_device=app_config.tts_device,
        cleaner_model=preferences.cleaner_model_name,
        cleaner_backend=preferences.cleaner_backend,
        cleaner_cuda_device=app_config.cleaner_cuda_device,
        cleaner_cuda_quantization=preferences.cleaner_cuda_quantization,
        cleaner_api_model=app_config.cleaner_api_model,
        cleaner_api_key=None,
        tts_options=None,
        parallel_worker_mode=None,
        enable_parallel=None,
        max_parallel_chapters=None,
    )


def deserialize_persisted_runtime_options(
    raw_value: str | dict[str, Any] | None,
) -> ConversionRuntimeOptions | None:
    if raw_value is None:
        return None

    if isinstance(raw_value, str):
        stripped = raw_value.strip()
        if not stripped:
            return None
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return None
    elif isinstance(raw_value, dict):
        payload = raw_value
    else:
        return None

    if not isinstance(payload, dict) or not payload:
        return None

    values = {field: payload.get(field) for field in PERSISTED_REPAIR_RUNTIME_FIELDS}
    if values.get("voice") in (None, ""):
        return None

    return ConversionRuntimeOptions(
        voice=str(values["voice"]),
        speed=float(values.get("speed") or 1.0),
        tts_engine=values.get("tts_engine"),
        tts_quantization=values.get("tts_quantization"),
        tts_provider=values.get("tts_provider"),
        tts_device=values.get("tts_device"),
        cleaner_model=values.get("cleaner_model"),
        cleaner_backend=values.get("cleaner_backend"),
        cleaner_cuda_device=values.get("cleaner_cuda_device"),
        cleaner_cuda_quantization=values.get("cleaner_cuda_quantization"),
        cleaner_api_model=values.get("cleaner_api_model"),
        cleaner_api_key=None,
        tts_options=values.get("tts_options"),
        parallel_worker_mode=None,
        enable_parallel=None,
        max_parallel_chapters=None,
    )


def apply_repair_worker_defaults(
    runtime_options: ConversionRuntimeOptions,
) -> ConversionRuntimeOptions:
    """Force repair jobs through safe automatic worker planning."""
    return dataclasses.replace(
        runtime_options,
        parallel_worker_mode="auto",
        enable_parallel=None,
        max_parallel_chapters=None,
    )


def _explicit_narration_fields(fields_set: Iterable[str]) -> set[str]:
    return set(fields_set) & (
        PERSISTED_REPAIR_RUNTIME_FIELDS | EXPLICIT_ONLY_REPAIR_RUNTIME_FIELDS
    )


def _require_cloud_cleaner_key(
    runtime_options: ConversionRuntimeOptions,
    app_config: AppConfig | None,
) -> None:
    """Fail fast when an API-cleaner repair has no usable key.

    Keys are intentionally never persisted with the book, so without this
    check the repair would start, synthesize chapters, and only fail once the
    cleaner is first invoked.
    """
    if str(runtime_options.cleaner_backend or "").strip().lower() != "api":
        return
    if str(runtime_options.cleaner_api_key or "").strip():
        return
    env_name = (
        str(getattr(app_config, "cleaner_api_key_env", "") or "").strip()
        or "OPENAI_API_KEY"
    )
    if os.environ.get(env_name, "").strip():
        return
    raise RepairRuntimeUnavailableError(
        "This book was converted with a cloud cleaner, but API keys are not "
        "stored with the book. Provide a cleaner API key with the repair "
        f"request or set {env_name} for the backend."
    )


def _merge_explicit_overrides(
    base: ConversionRuntimeOptions,
    request_options: ConversionRuntimeOptions,
    explicit_fields: set[str],
) -> ConversionRuntimeOptions:
    values = dataclasses.asdict(base)
    for field in explicit_fields:
        values[field] = getattr(request_options, field)
    return ConversionRuntimeOptions(**values)


def resolve_repair_runtime_options(
    *,
    book: Any,
    request_options: ConversionRuntimeOptions,
    request_fields_set: Iterable[str],
    app_config: AppConfig | None = None,
) -> ConversionRuntimeOptions:
    """Resolve repair runtime options from persisted book settings and request overrides."""
    persisted = deserialize_persisted_runtime_options(
        getattr(book, "conversion_runtime_options", None)
    )
    explicit_fields = _explicit_narration_fields(request_fields_set)

    if persisted is None:
        if app_config is not None:
            # Legacy books: unspecified narration fields come from app config,
            # not ConversionRuntimeOptions() hard-coded defaults.
            base = legacy_repair_runtime_options_from_app_config(app_config)
        elif explicit_fields:
            base = request_options
        else:
            raise RepairRuntimeUnavailableError(
                "Historical conversion settings are unavailable; provide runtime options "
                "before repairing this book."
            )
        if explicit_fields:
            resolved = _merge_explicit_overrides(
                base,
                request_options,
                explicit_fields,
            )
        else:
            resolved = base
    elif explicit_fields:
        resolved = _merge_explicit_overrides(
            persisted,
            request_options,
            explicit_fields,
        )
    else:
        resolved = persisted

    resolved = apply_repair_worker_defaults(resolved)
    _require_cloud_cleaner_key(resolved, app_config)
    return resolved


def repair_runtime_fields_set(payload: Any) -> set[str]:
    """Return explicitly provided runtime fields from a repair request model."""
    fields_set: set[str] = set(getattr(payload, "model_fields_set", None) or set())
    nested = getattr(payload, "runtime_options", None)
    if nested is None:
        return fields_set

    nested_fields = set(getattr(nested, "model_fields_set", None) or set())
    if nested_fields:
        fields_set.update(nested_fields)
    return fields_set
