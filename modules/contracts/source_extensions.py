"""Source file suffix contracts derived from ``SourceType``."""

from __future__ import annotations

from modules.contracts.source_types import SourceType


def source_type_to_suffix(source_type: SourceType) -> str:
    return f".{source_type.value}"


SUPPORTED_SOURCE_SUFFIXES: frozenset[str] = frozenset(
    source_type_to_suffix(source_type) for source_type in SourceType
)

FEEDBACK_LOOP_SOURCE_TYPES: frozenset[SourceType] = frozenset(
    {SourceType.PDF, SourceType.EPUB}
)
FEEDBACK_LOOP_SOURCE_SUFFIXES: frozenset[str] = frozenset(
    source_type_to_suffix(source_type) for source_type in FEEDBACK_LOOP_SOURCE_TYPES
)
