"""Tests for the local release qualification CLI and checklist recording."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tools.release_qualification.checklist import CHECKLIST_IDS, required_fields_for
from tools.release_qualification.evidence import DEFAULT_EVIDENCE_ROOT, EvidenceRoot, SENTINEL_NAME
from tools.release_qualification.metrics import OBSERVER_SCENARIOS
from tools.release_qualification.model import PhaseState, ReleaseState
from tools.release_qualification.runtime import app_tree_digest, artifact_fingerprint

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "release_qualification.py"
FINALIZATION_COMPLETE_MARKER = ".finalization-complete"
_USERNAME = os.environ.get("USER") or os.environ.get("USERNAME") or "testuser"


# The CLI rejects absolute paths and requires the artifact to live under the
# repository, so fixture DMGs must be written inside the tree. They go to an
# ignored scratch directory rather than the real bundle output: a fixture
# written there pollutes the packaging tree, and a test using the release
# version string would overwrite the actual release DMG with a few test bytes.
FIXTURE_DMG_DIR = REPO_ROOT / ".artifacts" / "release-qualification-tests" / "dmg"


@pytest.fixture(autouse=True)
def _clear_fixture_dmgs():
    yield
    shutil.rmtree(FIXTURE_DMG_DIR, ignore_errors=True)


def _write_repo_relative_dmg(version: str, payload: bytes) -> tuple[Path, str, str]:
    """Create a source DMG under the packaging tree; return path, repo-relative posix, digest."""
    dmg_dir = FIXTURE_DMG_DIR
    dmg_dir.mkdir(parents=True, exist_ok=True)
    dmg = dmg_dir / f"Orpheus_{version}_aarch64.dmg"
    dmg.write_bytes(payload)
    rel = dmg.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    digest = hashlib.sha256(payload).hexdigest()
    return dmg, rel, digest


def _write_artifacts_manifest(
    report_dir: Path,
    *,
    dmg_rel: str,
    dmg_digest: str,
    app_source: Path | None = None,
) -> None:
    app = report_dir / "fixture.app"
    if app_source is None:
        app.mkdir(exist_ok=True)
        (app / "executable").write_bytes(b"qualified application")
    else:
        shutil.copytree(app_source, app)
    app_digest = app_tree_digest(app)
    (report_dir / "artifacts.json").write_text(
        json.dumps({"app": app.relative_to(REPO_ROOT).as_posix(), "dmg": dmg_rel,
                    "dmg_sha256": dmg_digest, "app_tree_sha256": app_digest,
                    "artifact_hash": artifact_fingerprint(dmg_digest, app_digest)}),
        encoding="utf-8",
    )


def _run_cli(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def cleanup_evidence():
    created: list[Path] = []
    yield created
    for path in created:
        sentinel = path / SENTINEL_NAME
        if not sentinel.exists():
            continue
        EvidenceRoot(
            path=path,
            release_id=sentinel.read_text(encoding="utf-8").strip(),
            expected_root=(REPO_ROOT / DEFAULT_EVIDENCE_ROOT).resolve(),
        ).cleanup()


def test_prepare_creates_evidence_without_building(cleanup_evidence: list[Path]) -> None:
    result = _run_cli(["prepare", "--version", "1.0.0"])
    assert result.returncode == 2  # incomplete, not a release pass
    report_dir = None
    for line in result.stdout.splitlines():
        if "release-qualification" in line and Path(line.strip()).exists():
            report_dir = Path(line.strip())
            break
    assert report_dir is not None
    cleanup_evidence.append(report_dir)

    assert (report_dir / "report.json").is_file()
    assert (report_dir / "config" / "release_qualification.toml").is_file()
    assert (report_dir / "manual-checklist.md").is_file()
    payload = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
    assert payload["release_state"] == ReleaseState.INCOMPLETE.value
    # prepare must not invoke packaging/build commands
    commands_dir = report_dir / "commands"
    if commands_dir.exists():
        logs = list(commands_dir.glob("*"))
        assert not any("build" in path.name.lower() for path in logs)


def test_prepare_rejects_a_blank_version() -> None:
    result = _run_cli(["prepare", "--version", "   "])
    assert result.returncode == 1
    assert "version must be non-empty" in result.stderr


def test_run_stops_before_computer_use_as_incomplete(
    cleanup_evidence: list[Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare = _run_cli(["prepare", "--version", "1.0.0-test"])
    report_dir = _extract_report_dir(prepare.stdout)
    cleanup_evidence.append(report_dir)

    import importlib.util

    spec = importlib.util.spec_from_file_location("release_qualification_cli", SCRIPT)
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    def fake_handlers(report_dir: Path):  # noqa: ARG001
        from tools.release_qualification.engine import PhaseOutcome
        from tools.release_qualification.model import PhaseState as PS

        def ok(name: str, path: Path):  # noqa: ARG001
            return PhaseOutcome(PS.PASSED, "ok")

        def stop_cu(name: str, path: Path):  # noqa: ARG001
            return PhaseOutcome(PS.INCOMPLETE, "awaiting manual checks")

        return {
            "preflight": ok,
            "source_validation": ok,
            "sidecar_validation": ok,
            "bundle_validation": ok,
            "lifecycle_performance": ok,
            "fault_persistence": ok,
            "computer_use": stop_cu,
            "evidence_finalization": ok,
        }

    monkeypatch.setattr(cli, "build_phase_handlers", fake_handlers)
    code = cli.main(["run", "--report-dir", str(report_dir)])
    assert code == 2
    payload = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
    assert payload["phases"]["computer_use"]["state"] == PhaseState.INCOMPLETE.value
    assert payload["release_state"] == ReleaseState.INCOMPLETE.value


def test_resume_straight_after_prepare_starts_from_the_first_phase(
    cleanup_evidence: list[Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.util

    prepare = _run_cli(["prepare", "--version", "1.0.0-resume-fresh"])
    report_dir = _extract_report_dir(prepare.stdout)
    cleanup_evidence.append(report_dir)

    spec = importlib.util.spec_from_file_location("release_qualification_cli_fresh", SCRIPT)
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    from tools.release_qualification.engine import PhaseOutcome

    ran: list[str] = []

    def record(name: str, path: Path):  # noqa: ARG001
        ran.append(name)
        return PhaseOutcome(PhaseState.INCOMPLETE, "stop")

    monkeypatch.setattr(
        cli, "build_phase_handlers", lambda _dir: {name: record for name in cli.PHASE_REGISTRY}
    )
    assert cli.main(["run", "--resume", "--report-dir", str(report_dir)]) == 2
    assert ran == ["preflight"]


def test_record_check_copies_and_hashes_evidence(
    cleanup_evidence: list[Path],
    tmp_path: Path,
) -> None:
    prepare = _run_cli(["prepare", "--version", "1.0.0-check"])
    report_dir = _extract_report_dir(prepare.stdout)
    cleanup_evidence.append(report_dir)

    evidence = tmp_path / "shot.png"
    evidence.write_bytes(b"png-bytes-for-hash")
    metrics = tmp_path / "cold.jsonl"
    metrics.write_text('{"pid":1}\n', encoding="utf-8")
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()

    result = _run_cli(
        [
            "record-check",
            "cold_launch",
            "--result",
            "pass",
            "--notes",
            "Cold launch reached dashboard in 4.2s",
            "--evidence",
            str(evidence),
            "--metrics",
            str(metrics),
            "--report-dir",
            str(report_dir),
        ]
    )
    assert result.returncode == 0
    copied = report_dir / "screenshots" / "cold_launch.png"
    assert copied.is_file()
    assert hashlib.sha256(copied.read_bytes()).hexdigest() == digest
    checklist = json.loads((report_dir / "checklist.json").read_text(encoding="utf-8"))
    item = checklist["checks"]["cold_launch"]
    assert item["result"] == "pass"
    assert item["evidence_sha256"] == digest


def test_checks_cannot_pass_without_required_notes_metrics_or_evidence(
    cleanup_evidence: list[Path],
) -> None:
    prepare = _run_cli(["prepare", "--version", "1.0.0-req"])
    report_dir = _extract_report_dir(prepare.stdout)
    cleanup_evidence.append(report_dir)

    missing_notes = _run_cli(
        [
            "record-check",
            "warm_launch",
            "--result",
            "pass",
            "--notes",
            "",
            "--report-dir",
            str(report_dir),
        ]
    )
    assert missing_notes.returncode == 1

    # Metrics-required checks reject pass without metrics file reference in notes/evidence.
    fields = required_fields_for("idle_shutdown")
    assert "metrics" in fields or "notes" in fields
    missing_metrics = _run_cli(
        [
            "record-check",
            "idle_shutdown",
            "--result",
            "pass",
            "--notes",
            "looked fine",
            "--report-dir",
            str(report_dir),
        ]
    )
    assert missing_metrics.returncode == 1


def test_finalize_refuses_failed_or_incomplete_required_items(
    cleanup_evidence: list[Path],
) -> None:
    prepare = _run_cli(["prepare", "--version", "1.0.0-final"])
    report_dir = _extract_report_dir(prepare.stdout)
    cleanup_evidence.append(report_dir)

    result = _run_cli(["finalize", "--report-dir", str(report_dir)])
    assert result.returncode == 2
    payload = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
    assert payload["release_state"] != ReleaseState.PASSED.value


def test_status_reports_incomplete_exit_code(cleanup_evidence: list[Path]) -> None:
    prepare = _run_cli(["prepare", "--version", "1.0.0-status"])
    report_dir = _extract_report_dir(prepare.stdout)
    cleanup_evidence.append(report_dir)
    result = _run_cli(["status", "--report-dir", str(report_dir)])
    assert result.returncode == 2
    assert "incomplete" in result.stdout.lower() or "INCOMPLETE" in result.stdout


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Requires macOS release inspection or POSIX fixture permissions",
)
def test_finalize_succeeds_after_checklist_and_writes_validated_checksums(
    cleanup_evidence: list[Path],
    tmp_path: Path,
) -> None:
    version = "1.0.0-finok"
    prepare = _run_cli(["prepare", "--version", version])
    report_dir = _extract_report_dir(prepare.stdout)
    cleanup_evidence.append(report_dir)

    source_dmg, dmg_rel, dmg_digest = _write_repo_relative_dmg(version, b"dmg-payload-for-finalize")
    _write_artifacts_manifest(report_dir, dmg_rel=dmg_rel, dmg_digest=dmg_digest)
    _seed_phases_ready_for_computer_use(report_dir, artifact_hash=dmg_digest)
    _seed_all_checklist_passed(report_dir, tmp_path)

    result = _run_cli(["finalize", "--report-dir", str(report_dir)])
    assert result.returncode == 0, result.stderr
    assert "finalize ok" in result.stdout.lower()

    payload = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
    assert payload["release_state"] == ReleaseState.PASSED.value
    assert payload["phases"]["computer_use"]["state"] == PhaseState.PASSED.value
    assert payload["phases"]["evidence_finalization"]["state"] == PhaseState.PASSED.value

    # Staged four-release-input model: DMG + immutable md + SHA256SUMS (+ notes elsewhere).
    staged_dmg = report_dir / source_dmg.name
    final_md = report_dir / f"release-qualification-{version}.md"
    sums = report_dir / "SHA256SUMS"
    marker = report_dir / FINALIZATION_COMPLETE_MARKER
    assert staged_dmg.is_file() and not staged_dmg.is_symlink()
    assert final_md.is_file() and not final_md.is_symlink()
    assert sums.is_file()
    assert marker.is_file()
    assert hashlib.sha256(staged_dmg.read_bytes()).hexdigest() == dmg_digest
    assert hashlib.sha256(source_dmg.read_bytes()).hexdigest() == dmg_digest

    sums_text = sums.read_text(encoding="utf-8")
    assert dmg_digest in sums_text
    assert source_dmg.name in sums_text
    assert final_md.name in sums_text
    assert "report.json" not in sums_text
    assert "artifacts.json" not in sums_text
    assert "/" not in sums_text.replace("  ", " ")
    for line in sums_text.splitlines():
        digest, _, name = line.partition("  ")
        assert digest and name
        assert "/" not in name and "\\" not in name
        assert Path(name).name == name
        assert hashlib.sha256((report_dir / name).read_bytes()).hexdigest() == digest

    # Portable check: shasum resolves basenames relative to CWD (= report_dir).
    shasum = subprocess.run(
        ["shasum", "-a", "256", "-c", "SHA256SUMS"],
        cwd=str(report_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    assert shasum.returncode == 0, shasum.stderr or shasum.stdout

    status = _run_cli(["status", "--report-dir", str(report_dir)])
    assert status.returncode == 0

    # No absolute path / username leakage in uploadable or portable manifests.
    artifacts_text = (report_dir / "artifacts.json").read_text(encoding="utf-8")
    assert not Path(json.loads(artifacts_text)["dmg"]).is_absolute()
    assert not Path(json.loads(artifacts_text)["app"]).is_absolute()
    for blob in (artifacts_text, sums_text, final_md.read_text(encoding="utf-8")):
        assert _USERNAME not in blob
        assert "/Users/" not in blob
        assert str(REPO_ROOT) not in blob


def test_finalize_failed_checklist_exits_1_incomplete_exits_2(
    cleanup_evidence: list[Path],
    tmp_path: Path,
) -> None:
    version = "1.0.0-exitcodes"
    prepare = _run_cli(["prepare", "--version", version])
    report_dir = _extract_report_dir(prepare.stdout)
    cleanup_evidence.append(report_dir)
    _source, dmg_rel, digest = _write_repo_relative_dmg(version, b"dmg")
    _write_artifacts_manifest(report_dir, dmg_rel=dmg_rel, dmg_digest=digest)
    _seed_phases_ready_for_computer_use(report_dir, artifact_hash=digest)

    incomplete = _run_cli(["finalize", "--report-dir", str(report_dir)])
    assert incomplete.returncode == 2

    _seed_all_checklist_passed(report_dir, tmp_path)
    checks = json.loads((report_dir / "checklist.json").read_text(encoding="utf-8"))
    checks["checks"]["cold_launch"]["result"] = "fail"
    (report_dir / "checklist.json").write_text(json.dumps(checks), encoding="utf-8")

    failed = _run_cli(["finalize", "--report-dir", str(report_dir)])
    assert failed.returncode == 1


def test_finalize_requires_a_passing_observe_decision_for_every_budget(
    cleanup_evidence: list[Path],
    tmp_path: Path,
) -> None:
    version = "1.0.0-budgets"
    prepare = _run_cli(["prepare", "--version", version])
    report_dir = _extract_report_dir(prepare.stdout)
    cleanup_evidence.append(report_dir)
    _source, dmg_rel, digest = _write_repo_relative_dmg(version, b"budget-dmg")
    _write_artifacts_manifest(report_dir, dmg_rel=dmg_rel, dmg_digest=digest)
    _seed_phases_ready_for_computer_use(report_dir, artifact_hash=digest)
    _seed_all_checklist_passed(report_dir, tmp_path)
    artifact_hash = json.loads((report_dir / "report.json").read_text())["artifact_hash"]

    (report_dir / "metrics" / "soak-decision.json").unlink()
    missing = _run_cli(["finalize", "--report-dir", str(report_dir)])
    assert missing.returncode == 2
    assert "soak" in missing.stderr

    _write_observe_decision(report_dir, "soak", artifact_hash="older-build")
    stale = _run_cli(["finalize", "--report-dir", str(report_dir)])
    assert stale.returncode == 2
    assert "soak" in stale.stderr

    _write_observe_decision(report_dir, "soak", artifact_hash=artifact_hash)
    _write_observe_decision(report_dir, "idle-dashboard", artifact_hash=artifact_hash, passed=False)
    failed = _run_cli(["finalize", "--report-dir", str(report_dir)])
    assert failed.returncode == 1
    assert "idle-dashboard" in failed.stderr

    payload = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
    assert payload["release_state"] != ReleaseState.PASSED.value
    assert not (report_dir / "SHA256SUMS").exists()


def test_status_fails_integrity_when_passed_without_finalization_marker(
    cleanup_evidence: list[Path],
    tmp_path: Path,
) -> None:
    """Hard crash after PASSED persist but before marker must not status-pass."""
    version = "1.0.0-crash"
    prepare = _run_cli(["prepare", "--version", version])
    report_dir = _extract_report_dir(prepare.stdout)
    cleanup_evidence.append(report_dir)
    _source, dmg_rel, digest = _write_repo_relative_dmg(version, b"crash-dmg")
    _write_artifacts_manifest(report_dir, dmg_rel=dmg_rel, dmg_digest=digest)
    _seed_phases_ready_for_computer_use(report_dir, artifact_hash=digest)
    _seed_all_checklist_passed(report_dir, tmp_path)

    import importlib.util

    spec = importlib.util.spec_from_file_location("release_qualification_cli_crash", SCRIPT)
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    def boom(_report_dir: Path):
        raise RuntimeError("simulated crash before checksums/marker")

    cli.write_and_verify_checksums = boom  # type: ignore[method-assign]
    crashed = cli.main(["finalize", "--report-dir", str(report_dir)])
    assert crashed == 1
    assert not (report_dir / FINALIZATION_COMPLETE_MARKER).exists()
    payload = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
    assert payload["phases"]["evidence_finalization"]["state"] == PhaseState.FAILED.value

    # The failure detail is persisted with the same redaction as engine-run phases.
    def leaky(_report_dir: Path):
        raise OSError(f"copy failed: /Users/{_USERNAME}/Library/staged.dmg")

    cli.write_and_verify_checksums = leaky  # type: ignore[method-assign]
    assert cli.main(["finalize", "--report-dir", str(report_dir)]) == 1
    for name in ("report.json", "report.md"):
        text = (report_dir / name).read_text(encoding="utf-8")
        assert "/Users/[REDACTED]/Library/staged.dmg" in text
        assert f"/Users/{_USERNAME}/" not in text
    payload = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))

    # Simulate hard crash: release_state passed, no marker, no valid sums.
    payload["release_state"] = ReleaseState.PASSED.value
    payload["phases"]["evidence_finalization"] = {
        "name": "evidence_finalization",
        "state": PhaseState.PASSED.value,
        "detail": "crashed mid-finalize",
        "updated_at": payload.get("created_at", ""),
    }
    (report_dir / "report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (report_dir / "SHA256SUMS").unlink(missing_ok=True)
    (report_dir / FINALIZATION_COMPLETE_MARKER).unlink(missing_ok=True)

    status = _run_cli(["status", "--report-dir", str(report_dir)])
    assert status.returncode == 1
    assert "integrity" in (status.stdout + status.stderr).lower()


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Requires macOS release inspection or POSIX fixture permissions",
)
def test_finalize_retry_removes_stale_sums_and_marker(
    cleanup_evidence: list[Path],
    tmp_path: Path,
) -> None:
    version = "1.0.0-retry"
    prepare = _run_cli(["prepare", "--version", version])
    report_dir = _extract_report_dir(prepare.stdout)
    cleanup_evidence.append(report_dir)
    source_dmg, dmg_rel, digest = _write_repo_relative_dmg(version, b"retry-dmg")
    _write_artifacts_manifest(report_dir, dmg_rel=dmg_rel, dmg_digest=digest)
    _seed_phases_ready_for_computer_use(report_dir, artifact_hash=digest)
    _seed_all_checklist_passed(report_dir, tmp_path)

    stale_sums = report_dir / "SHA256SUMS"
    stale_sums.write_text("deadbeef  stale.dmg\n", encoding="utf-8")
    marker = report_dir / FINALIZATION_COMPLETE_MARKER
    marker.write_text("stale-marker-content\n", encoding="utf-8")

    result = _run_cli(["finalize", "--report-dir", str(report_dir)])
    assert result.returncode == 0, result.stderr
    sums_text = (report_dir / "SHA256SUMS").read_text(encoding="utf-8")
    assert "stale.dmg" not in sums_text
    assert source_dmg.name in sums_text
    assert digest in sums_text
    assert marker.is_file()
    assert "stale-marker-content" not in marker.read_text(encoding="utf-8")


def test_resume_rejects_stale_config_toolchain_artifact_or_commit_fingerprints(
    cleanup_evidence: list[Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import importlib.util

    prepare = _run_cli(["prepare", "--version", "1.0.0-fp"])
    report_dir = _extract_report_dir(prepare.stdout)
    cleanup_evidence.append(report_dir)

    spec = importlib.util.spec_from_file_location("release_qualification_cli_fp", SCRIPT)
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    _source, dmg_rel, digest = _write_repo_relative_dmg("1.0.0-fp", b"original-dmg")
    _write_artifacts_manifest(report_dir, dmg_rel=dmg_rel, dmg_digest=digest)

    def fake_handlers(report_dir: Path, **_kwargs):  # noqa: ARG001
        from tools.release_qualification.engine import PhaseOutcome
        from tools.release_qualification.model import PhaseState as PS

        def ok(name: str, path: Path):  # noqa: ARG001
            return PhaseOutcome(PS.PASSED, "ok")

        def stop_cu(name: str, path: Path):  # noqa: ARG001
            return PhaseOutcome(PS.INCOMPLETE, "awaiting manual checks")

        return {
            "preflight": ok,
            "source_validation": ok,
            "sidecar_validation": ok,
            "bundle_validation": ok,
            "lifecycle_performance": ok,
            "fault_persistence": ok,
            "computer_use": stop_cu,
            "evidence_finalization": ok,
        }

    monkeypatch.setattr(cli, "build_phase_handlers", fake_handlers)
    monkeypatch.setattr(cli, "compute_toolchain_fingerprint", lambda **_k: "tool-fixed")
    monkeypatch.setattr(cli, "_git_commit", lambda: "commit-fixed")

    first = cli.main(["run", "--report-dir", str(report_dir)])
    assert first == 2

    payload = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
    assert payload["toolchain_fingerprint"] == "tool-fixed"
    assert payload["commit"] == "commit-fixed"
    # Bundle phase must have refreshed artifact fingerprint from artifacts.json.
    manifest = json.loads((report_dir / "artifacts.json").read_text())
    expected_artifact_hash = hashlib.sha256(
        json.dumps([digest, manifest["app_tree_sha256"]]).encode()
    ).hexdigest()
    assert payload["artifact_hash"] == expected_artifact_hash
    assert payload["artifact_hash"] != digest

    # Stale config
    config_path = report_dir / "config" / "release_qualification.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8"
    )
    stale_config = cli.main(["run", "--resume", "--report-dir", str(report_dir)])
    assert stale_config == 1

    # Restore config hash by re-copying original snapshot hash into a fresh prepare is heavy;
    # instead rewrite payload fingerprints and mutate artifact bytes.
    prepare2 = _run_cli(["prepare", "--version", "1.0.0-fp2"])
    report_dir2 = _extract_report_dir(prepare2.stdout)
    cleanup_evidence.append(report_dir2)
    dmg2, dmg_rel2, digest2 = _write_repo_relative_dmg("1.0.0-fp2", b"dmg-v1")
    _write_artifacts_manifest(report_dir2, dmg_rel=dmg_rel2, dmg_digest=digest2)
    monkeypatch.setattr(cli, "compute_toolchain_fingerprint", lambda **_k: "tool-fixed")
    monkeypatch.setattr(cli, "_git_commit", lambda: "commit-fixed")
    assert cli.main(["run", "--report-dir", str(report_dir2)]) == 2

    dmg2.write_bytes(b"dmg-v2-changed")
    stale_artifact = cli.main(["run", "--resume", "--report-dir", str(report_dir2)])
    assert stale_artifact == 1

    # Restore DMG bytes so artifact hash matches again, then stale commit.
    dmg2.write_bytes(b"dmg-v1")
    monkeypatch.setattr(cli, "compute_toolchain_fingerprint", lambda **_k: "tool-fixed")
    monkeypatch.setattr(cli, "_git_commit", lambda: "commit-changed")
    stale_commit = cli.main(["run", "--resume", "--report-dir", str(report_dir2)])
    assert stale_commit == 1

    monkeypatch.setattr(cli, "_git_commit", lambda: "commit-fixed")
    monkeypatch.setattr(cli, "compute_toolchain_fingerprint", lambda **_k: "tool-changed")
    stale_tool = cli.main(["run", "--resume", "--report-dir", str(report_dir2)])
    assert stale_tool == 1


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_observe_rejects_unsupported_platform_before_accessing_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    platform: str,
) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("release_qualification_cli_platform", SCRIPT)
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli.sys, "platform", platform)

    code = cli.main(["observe", "warm-launch", "--report-dir", str(tmp_path / "missing")])

    captured = capsys.readouterr()
    assert code == cli.EXIT_FAIL
    assert captured.out == ""
    assert captured.err.strip() == (
        "observe requires macOS app bundles and macOS process sampling; "
        f"unsupported platform: {platform}"
    )


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="observe samples macOS app bundle processes using the macOS ps command",
)
def test_observe_prints_ready_marker_and_warm_launch_needs_handshake(
    cleanup_evidence: list[Path],
    tmp_path: Path,
) -> None:
    prepare = _run_cli(["prepare", "--version", "1.0.0-obs"])
    report_dir = _extract_report_dir(prepare.stdout)
    cleanup_evidence.append(report_dir)

    app = tmp_path / "Orpheus.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "Orpheus").write_text("x", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    _, dmg_rel, dmg_digest = _write_repo_relative_dmg(report_dir.name, b"qualified image")
    _write_artifacts_manifest(report_dir, dmg_rel=dmg_rel, dmg_digest=dmg_digest, app_source=app)

    missing = _run_cli(
        [
            "observe",
            "warm-launch",
            "--report-dir",
            str(report_dir),
            "--app-bundle",
            str(app),
            "--runtime-root",
            str(runtime),
        ]
    )
    assert "ORPHEUS_OBSERVE_READY" in missing.stdout
    assert missing.returncode != 0

    ok = _run_cli(
        [
            "observe",
            "warm-launch",
            "--report-dir",
            str(report_dir),
            "--app-bundle",
            str(app),
            "--runtime-root",
            str(runtime),
            "--action-started-at",
            "100.0",
            "--ui-ready-at",
            "101.5",
        ]
    )
    assert "ORPHEUS_OBSERVE_READY" in ok.stdout
    assert ok.returncode == 0


def test_observe_flushes_ready_marker_before_observer_runs(
    cleanup_evidence: list[Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ready marker must flush immediately so piped consumers see it before observe ends."""
    import importlib.util

    from tools.release_qualification.metrics import BudgetDecision, ObserverSession

    prepare = _run_cli(["prepare", "--version", "1.0.0-obs-flush"])
    report_dir = _extract_report_dir(prepare.stdout)
    cleanup_evidence.append(report_dir)

    app = tmp_path / "Orpheus.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "Orpheus").write_text("x", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    _, dmg_rel, dmg_digest = _write_repo_relative_dmg(report_dir.name, b"qualified image")
    _write_artifacts_manifest(report_dir, dmg_rel=dmg_rel, dmg_digest=dmg_digest, app_source=app)

    spec = importlib.util.spec_from_file_location("release_qualification_cli_obs_flush", SCRIPT)
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    events: list[str] = []
    ready_print_kwargs: dict[str, object] = {}
    real_print = print

    def tracking_print(*args: object, **kwargs: object) -> None:
        text = " ".join(str(arg) for arg in args)
        if "ORPHEUS_OBSERVE_READY" in text:
            events.append("ready")
            ready_print_kwargs.clear()
            ready_print_kwargs.update(kwargs)
        real_print(*args, **kwargs)

    def tracking_run(_self: ObserverSession, *_args: object, **_kwargs: object) -> BudgetDecision:
        events.append("run")
        return BudgetDecision(passed=True, detail="mocked-before-timeout", metrics={})

    monkeypatch.setattr("builtins.print", tracking_print)
    monkeypatch.setattr(ObserverSession, "run", tracking_run)
    monkeypatch.setattr(cli.sys, "platform", "darwin")

    code = cli.main(
        [
            "observe",
            "shutdown",
            "--report-dir",
            str(report_dir),
            "--app-bundle",
            str(app),
            "--runtime-root",
            str(runtime),
            "--seconds",
            "60",
        ]
    )
    assert code == 0
    assert events[:2] == ["ready", "run"]
    assert ready_print_kwargs.get("flush") is True


def test_record_check_hashes_metrics_rejects_symlink_and_path_escape(
    cleanup_evidence: list[Path],
    tmp_path: Path,
) -> None:
    prepare = _run_cli(["prepare", "--version", "1.0.0-ev"])
    report_dir = _extract_report_dir(prepare.stdout)
    cleanup_evidence.append(report_dir)

    metrics = tmp_path / "idle.jsonl"
    metrics.write_text('{"pid":7}\n', encoding="utf-8")
    metrics_digest = hashlib.sha256(metrics.read_bytes()).hexdigest()
    evidence = tmp_path / "shot.png"
    evidence.write_bytes(b"png-bytes")

    result = _run_cli(
        [
            "record-check",
            "idle_shutdown",
            "--result",
            "pass",
            "--notes",
            "Idle quit cleared the process tree",
            "--evidence",
            str(evidence),
            "--metrics",
            str(metrics),
            "--report-dir",
            str(report_dir),
        ]
    )
    assert result.returncode == 0
    checklist = json.loads((report_dir / "checklist.json").read_text(encoding="utf-8"))
    item = checklist["checks"]["idle_shutdown"]
    assert item["metrics_sha256"] == metrics_digest
    copied_metrics = report_dir / item["metrics_path"]
    assert copied_metrics.is_file()
    assert hashlib.sha256(copied_metrics.read_bytes()).hexdigest() == metrics_digest

    link = tmp_path / "evil-link.png"
    link.symlink_to(evidence)
    symlink_reject = _run_cli(
        [
            "record-check",
            "visual_regression",
            "--result",
            "pass",
            "--notes",
            "Visual states captured",
            "--evidence",
            str(link),
            "--report-dir",
            str(report_dir),
        ]
    )
    assert symlink_reject.returncode == 1
    assert "symlink" in symlink_reject.stderr.lower()

    # Path escape via crafted relative destination must be rejected by containment.
    from tools.release_qualification.checklist import record_check

    outside = tmp_path / "outside.png"
    outside.write_bytes(b"x")
    with pytest.raises(ValueError, match="containment|escape|outside|symlink"):
        record_check(
            report_dir,
            check_id="unsigned_first_open",
            result="pass",
            notes="Opened via Gatekeeper Open Anyway",
            evidence=outside,
            dest_name="../escaped.png",
        )


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Requires macOS release inspection or POSIX fixture permissions",
)
def test_fixture_phases_prepare_evidence_and_manual_phases_stay_incomplete(
    cleanup_evidence: list[Path],
) -> None:
    import importlib.util

    prepare = _run_cli(["prepare", "--version", "1.0.0-handlers"])
    report_dir = _extract_report_dir(prepare.stdout)
    cleanup_evidence.append(report_dir)

    spec = importlib.util.spec_from_file_location("release_qualification_cli_h", SCRIPT)
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    handlers = cli.build_phase_handlers(report_dir)
    life = handlers["lifecycle_performance"]("lifecycle_performance", report_dir)
    fault = handlers["fault_persistence"]("fault_persistence", report_dir)
    cu = handlers["computer_use"]("computer_use", report_dir)
    fin = handlers["evidence_finalization"]("evidence_finalization", report_dir)

    assert life.state is PhaseState.PASSED
    assert (report_dir / "lifecycle-runtime.json").is_file()
    assert fault.state is PhaseState.PASSED
    assert cu.state is PhaseState.INCOMPLETE
    assert fin.state is PhaseState.INCOMPLETE


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Requires macOS release inspection or POSIX fixture permissions",
)
def test_fixture_phases_can_rerun_after_a_previous_attempt(
    cleanup_evidence: list[Path],
) -> None:
    """run --resume and a repeated run re-enter fixture phases on the same report dir."""
    import importlib.util

    prepare = _run_cli(["prepare", "--version", "1.0.0-rerun"])
    report_dir = _extract_report_dir(prepare.stdout)
    cleanup_evidence.append(report_dir)

    spec = importlib.util.spec_from_file_location("release_qualification_cli_rerun", SCRIPT)
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    handlers = cli.build_phase_handlers(report_dir)
    for _attempt in range(2):
        life = handlers["lifecycle_performance"]("lifecycle_performance", report_dir)
        fault = handlers["fault_persistence"]("fault_persistence", report_dir)
        assert life.state is PhaseState.PASSED
        assert fault.state is PhaseState.PASSED

    read_only = report_dir / "fixtures" / "scenarios" / "read-only-copy"
    assert not os.access(read_only, os.W_OK)


def test_bundle_validation_does_not_invoke_the_sidecar_builder_itself(
    cleanup_evidence: list[Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bundle_validation runs only the Tauri build and the bundled smoke."""
    import importlib.util

    prepare = _run_cli(["prepare", "--version", "1.0.0-no-dup-sidecar"])
    report_dir = _extract_report_dir(prepare.stdout)
    cleanup_evidence.append(report_dir)

    spec = importlib.util.spec_from_file_location("release_qualification_cli_nodup", SCRIPT)
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    app = tmp_path / "Orpheus.app"
    app.mkdir()
    app_digest = app_tree_digest(app)
    calls: list[str] = []

    def fake_run(cmd_spec, **_kwargs):
        from tools.release_qualification.commands import CommandResult

        calls.append(cmd_spec.label)
        log = report_dir / "commands" / f"{cmd_spec.label}.log"
        log.parent.mkdir(exist_ok=True)
        log.write_text(json.dumps({
            "app": str(app), "dmg": str(tmp_path / "Orpheus_1.0.0_aarch64.dmg"),
            "dmg_sha256": "b" * 64, "app_tree_sha256": app_digest,
        }) + "\n")
        return CommandResult(
            label=cmd_spec.label,
            argv=tuple(cmd_spec.argv),
            started_at="2026-01-01T00:00:00Z",
            ended_at="2026-01-01T00:00:01Z",
            duration_seconds=0.01,
            exit_code=0,
            timed_out=False,
            log_path=f"commands/{cmd_spec.label}.log",
        )

    monkeypatch.setattr(cli, "_locate_app_bundle", lambda: tmp_path / "Orpheus.app")
    monkeypatch.setattr(cli, "_locate_dmg", lambda _version: tmp_path / "Orpheus_1.0.0_aarch64.dmg")
    monkeypatch.setattr(
        cli,
        "inspect_app_bundle",
        lambda *_args, **_kwargs: type("R", (), {"ok": True, "sha256": "a" * 64})(),
    )
    monkeypatch.setattr(
        cli,
        "inspect_dmg",
        lambda *_args, **_kwargs: type("R", (), {"ok": True, "sha256": "b" * 64})(),
    )
    monkeypatch.setattr(cli, "sha256_file", lambda _path: "b" * 64)
    monkeypatch.setattr(
        cli,
        "_repo_relative_posix",
        lambda path: f"bundle/{Path(path).name}",
    )

    handlers = cli.build_phase_handlers(report_dir, run_command_fn=fake_run)
    side = handlers["sidecar_validation"]("sidecar_validation", report_dir)
    assert side.state is PhaseState.PASSED
    assert calls == ["build-sidecar", "sidecar-runtime-smoke"]

    calls.clear()
    bundle = handlers["bundle_validation"]("bundle_validation", report_dir)
    assert bundle.state is PhaseState.PASSED
    assert calls == ["tauri-desktop-build", "bundled-runtime-smoke"]
    assert "build-sidecar" not in calls
    assert "sidecar-runtime-smoke" not in calls


def test_no_publish_or_workflow_mutation_across_qualification_helpers() -> None:
    root = REPO_ROOT / "tools" / "release_qualification"
    files = [SCRIPT, *sorted(root.glob("*.py"))]
    forbidden = (
        "gh release",
        "git tag",
        "git push",
        "open https://github.com",
        "workflow_dispatch",
        "gh api",
    )
    for path in files:
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{path.name} contains forbidden {token!r}"
        assert "workflows/source-validation.yml').write" not in text
        assert 'workflows/source-validation.yml").write' not in text


def _seed_phases_ready_for_computer_use(report_dir: Path, *, artifact_hash: str) -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("release_qualification_cli_seed", SCRIPT)
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    fingerprints = cli._current_fingerprints(report_dir)

    payload = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
    for name in (
        "preflight",
        "source_validation",
        "sidecar_validation",
        "bundle_validation",
        "lifecycle_performance",
        "fault_persistence",
    ):
        payload["phases"][name] = {
            "name": name,
            "state": PhaseState.PASSED.value,
            "detail": "seeded",
            "updated_at": payload.get("created_at", ""),
        }
    payload["phases"]["computer_use"] = {
        "name": "computer_use",
        "state": PhaseState.INCOMPLETE.value,
        "detail": "awaiting manual checks",
        "updated_at": payload.get("created_at", ""),
    }
    payload["phases"]["evidence_finalization"] = {
        "name": "evidence_finalization",
        "state": PhaseState.INCOMPLETE.value,
        "detail": "blocked until computer_use passes",
        "updated_at": payload.get("created_at", ""),
    }
    payload["commit"] = fingerprints["commit"]
    payload["config_hash"] = fingerprints["config_hash"]
    payload["toolchain_fingerprint"] = fingerprints["toolchain_fingerprint"]
    assert json.loads((report_dir / "artifacts.json").read_text())["dmg_sha256"] == artifact_hash
    payload["artifact_hash"] = fingerprints["artifact_hash"]
    payload["release_state"] = ReleaseState.INCOMPLETE.value
    for scenario in OBSERVER_SCENARIOS:
        _write_observe_decision(report_dir, scenario, artifact_hash=fingerprints["artifact_hash"])
    (report_dir / "report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (report_dir / "report.md").write_text("# seeded\n", encoding="utf-8")


def _write_observe_decision(
    report_dir: Path, scenario: str, *, artifact_hash: str, passed: bool = True
) -> None:
    path = report_dir / "metrics" / f"{scenario}-decision.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"passed": passed, "detail": "seeded", "metrics": {},
                    "artifact_hash": artifact_hash}),
        encoding="utf-8",
    )


def _seed_all_checklist_passed(report_dir: Path, tmp_path: Path) -> None:
    checks: dict[str, object] = {}
    for check_id in CHECKLIST_IDS:
        fields = required_fields_for(check_id)
        evidence_path = None
        evidence_sha = None
        metrics_path = None
        metrics_sha = None
        if "evidence" in fields:
            src = tmp_path / f"{check_id}.png"
            src.write_bytes(f"img-{check_id}".encode())
            dest = report_dir / "screenshots" / f"{check_id}.png"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src.read_bytes())
            evidence_path = str(dest.relative_to(report_dir))
            evidence_sha = hashlib.sha256(dest.read_bytes()).hexdigest()
        if "metrics" in fields:
            src = tmp_path / f"{check_id}.jsonl"
            src.write_text('{"ok":true}\n', encoding="utf-8")
            dest = report_dir / "metrics" / f"{check_id}.jsonl"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            metrics_path = str(dest.relative_to(report_dir))
            metrics_sha = hashlib.sha256(dest.read_bytes()).hexdigest()
        checks[check_id] = {
            "check_id": check_id,
            "result": "pass",
            "notes": f"Verified {check_id} during manual checks",
            "evidence_path": evidence_path,
            "evidence_sha256": evidence_sha,
            "metrics_path": metrics_path,
            "metrics_sha256": metrics_sha,
        }
    (report_dir / "checklist.json").write_text(
        json.dumps({"checks": checks}, indent=2),
        encoding="utf-8",
    )


def _extract_report_dir(stdout: str) -> Path:
    for line in stdout.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_dir() and (path / "report.json").exists():
            return path
        # Allow prefixed lines like "Evidence directory: /path"
        for part in candidate.split():
            path = Path(part)
            if path.is_dir() and (path / SENTINEL_NAME).exists():
                return path
    raise AssertionError(f"could not find report dir in output:\n{stdout}")
