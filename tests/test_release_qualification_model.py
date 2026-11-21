"""Tests for release qualification configuration and report state model."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.release_qualification.model import (
    PhaseState,
    QualificationConfig,
    QualificationReport,
    ReleaseState,
    load_qualification_config,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "config" / "release_qualification.toml"
TAURI_CONFIG = REPO_ROOT / "desktop" / "src-tauri" / "tauri.conf.json"


def test_required_bundle_metadata_matches_the_tauri_bundle():
    config = load_qualification_config(DEFAULT_CONFIG)
    tauri = json.loads(TAURI_CONFIG.read_text(encoding="utf-8"))

    assert config.product_name == tauri["productName"]
    assert config.bundle_id == tauri["identifier"]
    assert config.minimum_macos == tauri["bundle"]["macOS"]["minimumSystemVersion"]


def test_performance_budgets_are_positive_and_internally_consistent():
    config = load_qualification_config(DEFAULT_CONFIG)
    budgets = config.budgets

    assert budgets.cold_launch_seconds > 0
    assert budgets.warm_launch_seconds > 0
    assert budgets.window_close_seconds > 0
    assert budgets.process_exit_seconds > 0
    assert budgets.idle_cpu_median_percent > 0
    assert budgets.idle_cpu_p95_percent > 0
    assert budgets.soak_memory_growth_mib > 0
    assert budgets.soak_memory_growth_percent > 0

    assert budgets.warm_launch_seconds < budgets.cold_launch_seconds
    assert budgets.window_close_seconds < budgets.process_exit_seconds
    assert budgets.idle_cpu_median_percent < budgets.idle_cpu_p95_percent


def test_unknown_toml_keys_are_rejected(tmp_path: Path):
    path = tmp_path / "bad.toml"
    path.write_text(
        """
[release]
product_name = "Orpheus"
bundle_id = "com.orpheus.desktop"
minimum_macos = "14.0"
architecture = "arm64"

[budgets]
cold_launch_seconds = 15.0
warm_launch_seconds = 5.0
window_close_seconds = 1.0
process_exit_seconds = 12.0
idle_cpu_median_percent = 3.0
idle_cpu_p95_percent = 8.0
soak_memory_growth_mib = 100
soak_memory_growth_percent = 10.0
typo_budget = 1.0
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="typo_budget|unknown"):
        load_qualification_config(path)


def test_inconsistent_budgets_are_rejected():
    with pytest.raises(ValueError, match="warm_launch|cold_launch|budget"):
        QualificationConfig.from_mapping(
            {
                "release": {
                    "product_name": "Orpheus",
                    "bundle_id": "com.orpheus.desktop",
                    "minimum_macos": "14.0",
                    "architecture": "arm64",
                },
                "budgets": {
                    "cold_launch_seconds": 5.0,
                    "warm_launch_seconds": 15.0,
                    "window_close_seconds": 1.0,
                    "process_exit_seconds": 12.0,
                    "idle_cpu_median_percent": 3.0,
                    "idle_cpu_p95_percent": 8.0,
                    "soak_memory_growth_mib": 100,
                    "soak_memory_growth_percent": 10.0,
                },
            }
        )


def test_report_passes_only_when_every_required_phase_passed():
    report = QualificationReport.new(release_id="1.0.0-test", commit="abc123")
    for phase in report.required_phases:
        report.set_phase(phase, PhaseState.PASSED, "ok")

    assert report.release_state is ReleaseState.PASSED


def test_required_incomplete_phase_blocks_release():
    report = QualificationReport.new(release_id="1.0.0-test", commit="abc123")
    report.set_phase("computer_use", PhaseState.INCOMPLETE, "not recorded")
    assert report.release_state is ReleaseState.INCOMPLETE


def test_skipped_required_phase_yields_incomplete_never_passed():
    report = QualificationReport.new(release_id="1.0.0-test", commit="abc123")
    for phase in report.required_phases:
        if phase == "computer_use":
            report.set_phase(phase, PhaseState.SKIPPED, "assets unavailable")
        else:
            report.set_phase(phase, PhaseState.PASSED, "ok")

    assert report.release_state is ReleaseState.INCOMPLETE
    assert report.release_state is not ReleaseState.PASSED


def test_failed_required_phase_blocks_release():
    report = QualificationReport.new(release_id="1.0.0-test", commit="abc123")
    for phase in report.required_phases:
        report.set_phase(phase, PhaseState.PASSED, "ok")
    report.set_phase("preflight", PhaseState.FAILED, "dirty worktree")

    assert report.release_state is ReleaseState.FAILED


def test_report_timestamps_are_utc_iso8601():
    report = QualificationReport.new(release_id="1.0.0-test", commit="abc123")
    report.set_phase("preflight", PhaseState.RUNNING, "started")

    assert report.created_at.endswith("Z") or "+00:00" in report.created_at
    phase = report.phases["preflight"]
    assert phase.updated_at.endswith("Z") or "+00:00" in phase.updated_at
