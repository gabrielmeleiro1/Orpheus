"""Conversion runtime option contracts shared by API routes and app services."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, MutableMapping
from typing import Any

from modules.contracts.conversion_runtime_options import (
    ConversionRuntimeOptions,
)

_DEFAULT_RUNTIME_OPTIONS = ConversionRuntimeOptions()
_RUNTIME_OPTION_FIELD_NAMES = tuple(
    field.name for field in dataclasses.fields(ConversionRuntimeOptions)
)
_DEFAULT_RUNTIME_OPTION_VALUES: dict[str, Any] = {
    name: getattr(_DEFAULT_RUNTIME_OPTIONS, name)
    for name in _RUNTIME_OPTION_FIELD_NAMES
}
_REQUEST_ONLY_FIELDS = frozenset(
    {
        "source_path",
        "conversion_mode",
        "title",
        "author",
        "runtime_options",
    }
)


def runtime_option_field_names() -> tuple[str, ...]:
    """Return the canonical runtime-option field names."""
    return _RUNTIME_OPTION_FIELD_NAMES


def _runtime_options_from_values(values: Mapping[str, Any]) -> ConversionRuntimeOptions:
    return ConversionRuntimeOptions(
        **{field: values[field] for field in _RUNTIME_OPTION_FIELD_NAMES}
    )


def merge_runtime_options(
    flat: ConversionRuntimeOptions,
    nested: ConversionRuntimeOptions | None = None,
    *,
    nested_fields_set: set[str] | frozenset[str] | None = None,
) -> ConversionRuntimeOptions:
    """Merge flat top-level runtime options with nested ``runtime_options`` values.

    Nested values win only for fields explicitly set in the nested payload.
    """
    if nested is None:
        return flat

    nested_fields = nested_fields_set or frozenset()
    option_values = {
        field: (
            getattr(nested, field)
            if field in nested_fields
            else getattr(flat, field)
        )
        for field in _RUNTIME_OPTION_FIELD_NAMES
    }
    return ConversionRuntimeOptions(**option_values)


def _flat_values_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    values = dict(_DEFAULT_RUNTIME_OPTION_VALUES)
    for field in _RUNTIME_OPTION_FIELD_NAMES:
        if field in payload:
            values[field] = payload[field]
    return values


def _nested_values_from_payload(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, set[str] | None]:
    nested_raw = payload.get("runtime_options")
    if nested_raw is None:
        return None, None
    if not isinstance(nested_raw, Mapping):
        raise TypeError("runtime_options must be an object")
    nested_values = dict(_DEFAULT_RUNTIME_OPTION_VALUES)
    nested_fields_set: set[str] = set()
    for field in _RUNTIME_OPTION_FIELD_NAMES:
        if field in nested_raw:
            nested_values[field] = nested_raw[field]
            nested_fields_set.add(field)
    return nested_values, nested_fields_set


def from_api_payload(payload: Mapping[str, Any]) -> ConversionRuntimeOptions:
    """Build ``ConversionRuntimeOptions`` from a conversion start/repair API payload.

    Supports the frontend's canonical nested ``runtime_options`` object as well as
    legacy flat top-level runtime fields. Nested values override flat values only
    when explicitly present in ``runtime_options``.
    """
    unknown_fields = set(payload) - set(_RUNTIME_OPTION_FIELD_NAMES) - _REQUEST_ONLY_FIELDS
    if unknown_fields:
        extra = ", ".join(sorted(unknown_fields))
        raise ValueError(f"Unsupported conversion payload fields: {extra}")

    flat_values = _flat_values_from_payload(payload)
    nested_values, nested_fields_set = _nested_values_from_payload(payload)
    flat_options = _runtime_options_from_values(flat_values)
    if nested_values is None:
        return flat_options

    nested_options = _runtime_options_from_values(nested_values)
    return merge_runtime_options(
        flat_options,
        nested_options,
        nested_fields_set=nested_fields_set,
    )


def values_from_request_model(
    payload: Mapping[str, Any],
    *,
    nested_payload: Mapping[str, Any] | None = None,
    nested_fields_set: set[str] | frozenset[str] | None = None,
) -> ConversionRuntimeOptions:
    """Build runtime options from validated request-model mappings."""
    flat_values: MutableMapping[str, Any] = dict(_DEFAULT_RUNTIME_OPTION_VALUES)
    for field in _RUNTIME_OPTION_FIELD_NAMES:
        if field in payload:
            flat_values[field] = payload[field]

    flat_options = _runtime_options_from_values(flat_values)
    if nested_payload is None:
        return flat_options

    nested_values: MutableMapping[str, Any] = dict(_DEFAULT_RUNTIME_OPTION_VALUES)
    for field in _RUNTIME_OPTION_FIELD_NAMES:
        if field in nested_payload:
            nested_values[field] = nested_payload[field]

    nested_options = _runtime_options_from_values(nested_values)
    return merge_runtime_options(
        flat_options,
        nested_options,
        nested_fields_set=nested_fields_set,
    )
