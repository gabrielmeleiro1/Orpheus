"""Conversion runtime option contracts shared by API routes and app services."""

from __future__ import annotations

import dataclasses

from modules.contracts.conversion_runtime_options import (
    ConversionRuntimeOptions,
)

_RUNTIME_OPTION_FIELD_NAMES = tuple(
    field.name for field in dataclasses.fields(ConversionRuntimeOptions)
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
