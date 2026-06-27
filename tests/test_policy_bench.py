from __future__ import annotations

from pathlib import Path

import pytest

from modules.pipeline.models import PipelineConfig
from orpheus.bench.policy import (
    load_policy_manifest,
    policy_fixture_paths,
    validate_policy_parse_metrics,
)
from scripts.benchmark_nagel_pipeline import _parse_source_metrics

POLICY_FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "policy"


@pytest.fixture
def policy_config() -> PipelineConfig:
    return PipelineConfig(
        output_dir=Path("."),
        temp_dir=Path("."),
        pre_chunk_document_cleanup=True,
        skip_non_prose_sections=True,
    )


def test_policy_manifest_lists_all_fixture_sources():
    manifest = load_policy_manifest()
    assert len(policy_fixture_paths()) == len(manifest["cases"])


@pytest.mark.parametrize("case", load_policy_manifest()["cases"], ids=lambda case: case["id"])
def test_policy_fixtures_match_parse_skip_expectations(case: dict, policy_config: PipelineConfig) -> None:
    source = POLICY_FIXTURE_ROOT / case["source"]
    metrics = _parse_source_metrics(source, policy_config)
    assert validate_policy_parse_metrics(case, metrics) == []
