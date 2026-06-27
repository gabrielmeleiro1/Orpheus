"""Contract tests for source suffix consumers."""

from __future__ import annotations

from modules.app.services import library_service
from modules.contracts.source_extensions import (
    FEEDBACK_LOOP_SOURCE_SUFFIXES,
    SUPPORTED_SOURCE_SUFFIXES,
    source_type_to_suffix,
)
from modules.ingestion import preflight
from modules.storage.models import SourceType
import tools.feedback_loop_runner as feedback_loop_runner


def test_supported_source_suffixes_derived_from_source_type() -> None:
    expected = frozenset(source_type_to_suffix(source_type) for source_type in SourceType)

    assert SUPPORTED_SOURCE_SUFFIXES == expected


def test_upload_and_preflight_suffixes_match_supported_source_contract() -> None:
    assert library_service.SUPPORTED_UPLOAD_SUFFIXES == SUPPORTED_SOURCE_SUFFIXES
    assert preflight.SUPPORTED_PREFLIGHT_SUFFIXES == SUPPORTED_SOURCE_SUFFIXES


def test_feedback_loop_runner_syncs_with_contract() -> None:
    assert feedback_loop_runner.SUPPORTED_SOURCE_SUFFIXES == FEEDBACK_LOOP_SOURCE_SUFFIXES
