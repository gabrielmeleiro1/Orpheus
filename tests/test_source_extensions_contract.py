"""Contract tests for source suffix consumers."""

from __future__ import annotations

from modules.app.services import library_service
from modules.contracts.source_extensions import SUPPORTED_SOURCE_SUFFIXES
from modules.ingestion import preflight


def test_upload_and_preflight_suffixes_match_supported_source_contract() -> None:
    assert library_service.SUPPORTED_UPLOAD_SUFFIXES == SUPPORTED_SOURCE_SUFFIXES
    assert preflight.SUPPORTED_PREFLIGHT_SUFFIXES == SUPPORTED_SOURCE_SUFFIXES
