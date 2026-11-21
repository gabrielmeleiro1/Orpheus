#!/usr/bin/env python3
"""Local macOS release qualification entry point.

Never publishes, tags, pushes, opens GitHub, or modifies workflows.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.release_qualification.artifact import (  # noqa: E402
    build_bundle_commands,
    build_sidecar_commands,
    inspect_app_bundle,
    inspect_dmg,
    sha256_file,
)
from tools.release_qualification.checklist import (  # noqa: E402
    CHECKLIST_IDS,
    checklist_finalize_gate,
    load_checklist,
    record_check,
    render_manual_checklist_markdown,
)
from tools.release_qualification.commands import CommandResult, run_command  # noqa: E402
from tools.release_qualification.engine import (  # noqa: E402
    PHASE_REGISTRY,
    PhaseEngine,
    PhaseOutcome,
    ResumeRejectedError,
    persist_report,
    report_from_payload,
)
from tools.release_qualification.evidence import (  # noqa: E402
    DEFAULT_EVIDENCE_ROOT,
    EvidenceRoot,
    SENTINEL_NAME,
    atomic_write_json,
    atomic_write_text,
)
from tools.release_qualification.fixtures import FixtureRoot  # noqa: E402
from tools.release_qualification.matrix import build_validation_matrix  # noqa: E402
from tools.release_qualification.metrics import (  # noqa: E402
    OBSERVER_SCENARIOS,
    ObserverSession,
    decision_path,
    observe_decisions_gate,
)
from tools.release_qualification.model import (  # noqa: E402
    PhaseState,
    QualificationReport,
    ReleaseState,
    load_qualification_config,
)
from tools.release_qualification.preflight import (  # noqa: E402
    PreflightError,
    collect_preflight,
    compute_toolchain_fingerprint,
)
from tools.release_qualification.runtime import (  # noqa: E402
    app_tree_digest, artifact_fingerprint,
)

DEFAULT_CONFIG = REPO_ROOT / "config" / "release_qualification.toml"
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INCOMPLETE = 2

CommandRunner = Callable[..., CommandResult]

# Atomic marker written only after staged release inputs verify.
FINALIZATION_COMPLETE_MARKER = ".finalization-complete"


def build_phase_handlers(
    report_dir: Path,
    *,
    run_command_fn: CommandRunner = run_command,
) -> dict[str, object]:
    """Compose phase handlers from existing qualification modules.

    Lifecycle/fault phases only prepare isolated fixtures; performance budgets are
    measured by ``observe`` and enforced at ``finalize``, manual checks by ``record-check``.
    ``computer_use`` and ``evidence_finalization`` stay incomplete until finalize.
    """

    def preflight(name: str, path: Path) -> PhaseOutcome:  # noqa: ARG001
        try:
            config = load_qualification_config(path / "config" / "release_qualification.toml")
            facts = collect_preflight(
                repo_root=REPO_ROOT,
                config=config,
                config_path=path / "config" / "release_qualification.toml",
                release_version=_release_version_from_report(path),
            )
        except PreflightError as exc:
            return PhaseOutcome(PhaseState.FAILED, str(exc))
        atomic_write_json(path / "preflight.json", facts.as_allowlisted_dict())
        return PhaseOutcome(PhaseState.PASSED, "preflight ok")

    def source_validation(name: str, path: Path) -> PhaseOutcome:  # noqa: ARG001
        matrix = build_validation_matrix(repo_root=REPO_ROOT)
        log_dir = path / "commands"
        results: list[tuple[str, int]] = []
        for spec in matrix.commands:
            result = run_command_fn(spec, log_dir=log_dir)
            results.append((result.label, result.exit_code))
            if result.exit_code != 0:
                decision = matrix.evaluate_results(results)
                return PhaseOutcome(
                    PhaseState.FAILED,
                    f"matrix failed: {', '.join(decision.failed_labels)}",
                )
        return PhaseOutcome(PhaseState.PASSED, "source validation ok")

    def sidecar_validation(name: str, path: Path) -> PhaseOutcome:  # noqa: ARG001
        commands = build_sidecar_commands(repo_root=REPO_ROOT)
        log_dir = path / "commands"
        for spec in commands:
            result = run_command_fn(spec, log_dir=log_dir)
            if result.exit_code != 0:
                return PhaseOutcome(PhaseState.FAILED, f"{spec.label} failed")
        return PhaseOutcome(PhaseState.PASSED, "sidecar validation ok")

    def bundle_validation(name: str, path: Path) -> PhaseOutcome:  # noqa: ARG001
        # `tauri build` rebuilds the sidecar via beforeBuildCommand, so the copy smoked
        # in sidecar_validation is not the shipped one; bundled-runtime-smoke covers that.
        commands = build_bundle_commands(repo_root=REPO_ROOT)
        log_dir = path / "commands"
        smoke_evidence = None
        for spec in commands:
            result = run_command_fn(spec, log_dir=log_dir)
            if result.exit_code != 0:
                return PhaseOutcome(PhaseState.FAILED, f"{spec.label} failed")
            if spec.label == "bundled-runtime-smoke":
                try:
                    log_path = Path(result.log_path)
                    if not log_path.is_absolute():
                        log_path = path / log_path
                    for line in log_path.read_text(encoding="utf-8").splitlines():
                        try:
                            candidate = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(candidate, dict) and "dmg_sha256" in candidate:
                            smoke_evidence = candidate
                except OSError as exc:
                    return PhaseOutcome(PhaseState.FAILED, f"missing bundled smoke evidence: {exc}")
        config = load_qualification_config(path / "config" / "release_qualification.toml")
        app = _locate_app_bundle()
        dmg = _locate_dmg(_release_version_from_report(path))
        try:
            inspect_app_bundle(
                app,
                config=config,
                release_version=_release_version_from_report(path),
            )
            inspect_dmg(
                dmg,
                product_name=config.product_name,
                release_version=_release_version_from_report(path),
            )
            if smoke_evidence is None:
                raise ValueError("missing bundled smoke digests")
            artifact_hash = str(smoke_evidence.get("dmg_sha256", ""))
            app_hash = str(smoke_evidence.get("app_tree_sha256", ""))
            if sha256_file(dmg) != artifact_hash:
                raise ValueError("DMG changed after bundled smoke")
            if app_tree_digest(app) != app_hash:
                raise ValueError("app bundle changed after bundled smoke")
            smoked_app = Path(str(smoke_evidence.get("app", "")))
            smoked_dmg = Path(str(smoke_evidence.get("dmg", "")))
            if not smoked_app.is_absolute():
                smoked_app = REPO_ROOT / smoked_app
            if not smoked_dmg.is_absolute():
                smoked_dmg = REPO_ROOT / smoked_dmg
            if smoked_app.resolve() != app.resolve() or smoked_dmg.resolve() != dmg.resolve():
                raise ValueError("bundled smoke artifact paths differ")
        except Exception as exc:  # noqa: BLE001 - phase boundary
            return PhaseOutcome(PhaseState.FAILED, str(exc))
        atomic_write_json(
            path / "artifacts.json",
            {
                "app": _repo_relative_posix(app),
                "dmg": _repo_relative_posix(dmg),
                "dmg_sha256": artifact_hash,
                "app_tree_sha256": app_hash,
                "artifact_hash": artifact_fingerprint(artifact_hash, app_hash),
            },
        )
        return PhaseOutcome(PhaseState.PASSED, "bundle validation ok")

    def lifecycle_performance(name: str, path: Path) -> PhaseOutcome:  # noqa: ARG001
        evidence = EvidenceRoot(
            path=path,
            release_id=_release_id_from_report(path),
            expected_root=(REPO_ROOT / DEFAULT_EVIDENCE_ROOT).resolve(),
        )
        fixtures = FixtureRoot.create(evidence)
        fixtures.discard("runtimes/lifecycle")
        runtime = fixtures.create_runtime("lifecycle")
        atomic_write_json(path / "lifecycle-runtime.json", {"root": str(runtime.root)})
        return PhaseOutcome(
            PhaseState.PASSED,
            "lifecycle fixtures prepared; finalize requires passing observe decisions",
        )

    def fault_persistence(name: str, path: Path) -> PhaseOutcome:  # noqa: ARG001
        evidence = EvidenceRoot(
            path=path,
            release_id=_release_id_from_report(path),
            expected_root=(REPO_ROOT / DEFAULT_EVIDENCE_ROOT).resolve(),
        )
        fixtures = FixtureRoot.create(evidence)
        scenarios = ("corrupt-db", "missing-output", "read-only-copy")
        for relative in ("runtimes/fault-pristine", *(f"scenarios/{name}" for name in scenarios)):
            fixtures.discard(relative)
        pristine = fixtures.create_runtime("fault-pristine")
        from tools.release_qualification.fixtures import (
            create_scenario_copy,
            generate_large_library,
        )

        release_id = _release_id_from_report(path)
        generate_large_library(
            pristine,
            seed=1,
            count=25,
            expected_qualification_root=path,
            release_id=release_id,
        )
        for scenario in scenarios:
            create_scenario_copy(
                pristine,
                fixtures.scenarios_root,
                scenario,
                expected_qualification_root=path,
                release_id=release_id,
                backups_root=fixtures.backups_root,
            )
        return PhaseOutcome(
            PhaseState.PASSED,
            "fault fixtures prepared; measurements require observe/checklist",
        )

    def computer_use(name: str, path: Path) -> PhaseOutcome:  # noqa: ARG001
        return PhaseOutcome(PhaseState.INCOMPLETE, "awaiting manual checklist")

    def evidence_finalization(name: str, path: Path) -> PhaseOutcome:  # noqa: ARG001
        return PhaseOutcome(
            PhaseState.INCOMPLETE,
            "blocked until computer_use checklist passes; use finalize",
        )

    return {
        "preflight": preflight,
        "source_validation": source_validation,
        "sidecar_validation": sidecar_validation,
        "bundle_validation": bundle_validation,
        "lifecycle_performance": lifecycle_performance,
        "fault_persistence": fault_persistence,
        "computer_use": computer_use,
        "evidence_finalization": evidence_finalization,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "prepare":
        return cmd_prepare(args)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "observe":
        return cmd_observe(args)
    if args.command == "record-check":
        return cmd_record_check(args)
    if args.command == "finalize":
        return cmd_finalize(args)
    if args.command == "status":
        return cmd_status(args)
    parser.error(f"unknown command: {args.command}")
    return EXIT_FAIL


def cmd_prepare(args: argparse.Namespace) -> int:
    version = str(args.version).strip()
    if not version:
        print("version must be non-empty", file=sys.stderr)
        return EXIT_FAIL
    config_path = Path(args.config) if getattr(args, "config", None) else DEFAULT_CONFIG
    config = load_qualification_config(config_path)

    release_id = version
    if args.final:
        release_id = f"{version}-final"
    evidence = EvidenceRoot.create(
        release_id=release_id,
        base=REPO_ROOT / DEFAULT_EVIDENCE_ROOT,
    )
    report_dir = evidence.path
    config_dir = report_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(config_path, config_dir / "release_qualification.toml")

    commit = _git_commit()
    report = QualificationReport.new(release_id=release_id, commit=commit)
    # prepare leaves every required phase incomplete/not started → incomplete release.
    persist_report(
        report_dir,
        report,
        config_hash=sha256_file(config_dir / "release_qualification.toml"),
        toolchain_fingerprint=compute_toolchain_fingerprint(),
        artifact_hash="",
    )
    atomic_write_text(
        report_dir / "manual-checklist.md",
        render_manual_checklist_markdown(release_id=release_id),
    )
    atomic_write_json(
        report_dir / "checklist.json",
        {"checks": {check_id: None for check_id in CHECKLIST_IDS}},
    )
    atomic_write_json(
        report_dir / "prepare.json",
        {
            "version": version,
            "final": bool(args.final),
            "product_name": config.product_name,
            "bundle_id": config.bundle_id,
            "built": False,
        },
    )
    print(str(report_dir))
    return EXIT_INCOMPLETE


def cmd_run(args: argparse.Namespace) -> int:
    report_dir = Path(args.report_dir).resolve()
    _assert_report_dir(report_dir)
    payload = _load_report_payload(report_dir)
    try:
        fingerprints = _current_fingerprints(report_dir)
    except ResumeRejectedError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAIL

    handlers = build_phase_handlers(report_dir)
    engine = PhaseEngine(
        report_dir=report_dir,
        release_id=str(payload["release_id"]),
        commit=fingerprints["commit"],
        config_hash=fingerprints["config_hash"],
        toolchain_fingerprint=fingerprints["toolchain_fingerprint"],
        artifact_hash=fingerprints["artifact_hash"],
        phase_handlers=handlers,  # type: ignore[arg-type]
    )
    try:
        if args.resume:
            report = engine.resume()
        else:
            report = engine.run()
    except ResumeRejectedError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAIL
    return _exit_for_release_state(report.release_state)


def cmd_observe(args: argparse.Namespace) -> int:
    if sys.platform != "darwin":
        print(
            "observe requires macOS app bundles and macOS process sampling; "
            f"unsupported platform: {sys.platform}",
            file=sys.stderr,
        )
        return EXIT_FAIL
    report_dir = Path(args.report_dir).resolve()
    _assert_report_dir(report_dir)
    config = load_qualification_config(report_dir / "config" / "release_qualification.toml")
    app_bundle = Path(args.app_bundle) if args.app_bundle else _locate_app_bundle()
    try:
        artifacts = json.loads((report_dir / "artifacts.json").read_text(encoding="utf-8"))
        if not isinstance(artifacts, dict):
            raise ValueError("invalid artifacts.json")
        if app_tree_digest(app_bundle) != artifacts.get("app_tree_sha256"):
            raise ValueError("app bundle changed since qualification")
        artifact_hash = _current_artifact_hash(report_dir)
    except (OSError, ValueError, ResumeRejectedError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAIL
    runtime_root = Path(args.runtime_root) if args.runtime_root else report_dir / "fixtures"
    session = ObserverSession(
        scenario=args.scenario,
        report_dir=report_dir,
        budgets=config.budgets,
        app_bundle=app_bundle,
        runtime_root=runtime_root,
    )
    print(session.ready_marker(), flush=True)
    try:
        decision = session.run(
            seconds=args.seconds,
            minutes=args.minutes,
            action_started_at=args.action_started_at,
            ui_ready_at=args.ui_ready_at,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAIL
    atomic_write_json(
        decision_path(report_dir, args.scenario),
        {
            "passed": decision.passed,
            "detail": decision.detail,
            "metrics": decision.metrics,
            "artifact_hash": artifact_hash,
        },
    )
    print(f"budget_passed={decision.passed} detail={decision.detail}")
    return EXIT_PASS if decision.passed else EXIT_FAIL


def cmd_record_check(args: argparse.Namespace) -> int:
    report_dir = Path(args.report_dir).resolve()
    _assert_report_dir(report_dir)
    try:
        record_check(
            report_dir,
            check_id=args.check_id,
            result=args.result,
            notes=args.notes or "",
            evidence=Path(args.evidence) if args.evidence else None,
            metrics=Path(args.metrics) if args.metrics else None,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAIL
    return EXIT_PASS


def cmd_finalize(args: argparse.Namespace) -> int:
    """Promote checklist-complete runs through computer_use + evidence_finalization."""
    report_dir = Path(args.report_dir).resolve()
    _assert_report_dir(report_dir)
    payload = _load_report_payload(report_dir)
    report = report_from_payload(payload)

    blocked, reason, gate_code = checklist_finalize_gate(
        load_checklist(report_dir),
        report_dir=report_dir,
    )
    if blocked:
        print(f"finalize refused: {reason}", file=sys.stderr)
        return gate_code

    for name in PHASE_REGISTRY:
        if name in {"computer_use", "evidence_finalization"}:
            continue
        state = report.phases[name].state
        if state is PhaseState.FAILED:
            print(f"finalize refused: phase failed: {name}", file=sys.stderr)
            return EXIT_FAIL
        if state is not PhaseState.PASSED:
            print(f"finalize refused: phase incomplete: {name}", file=sys.stderr)
            return EXIT_INCOMPLETE

    try:
        fingerprints = _current_fingerprints(report_dir)
        _assert_saved_fingerprints(payload, fingerprints)
    except ResumeRejectedError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_FAIL

    blocked, reason, gate_code = observe_decisions_gate(
        report_dir,
        artifact_hash=fingerprints["artifact_hash"],
    )
    if blocked:
        print(f"finalize refused: {reason}", file=sys.stderr)
        return gate_code

    precheck = validate_finalization_inputs(report_dir)
    if precheck.state is not PhaseState.PASSED:
        print(f"finalize refused: {precheck.detail}", file=sys.stderr)
        return EXIT_FAIL if precheck.state is PhaseState.FAILED else EXIT_INCOMPLETE

    # Drop stale portable release inputs before retry so a prior crash cannot linger.
    _clear_stale_finalization_outputs(report_dir)

    report.set_phase(
        "lifecycle_performance",
        PhaseState.PASSED,
        f"observed budgets passed: {', '.join(OBSERVER_SCENARIOS)}",
    )
    report.set_phase(
        "computer_use",
        PhaseState.PASSED,
        "all required manual checklist records passed",
    )
    report.set_phase(
        "evidence_finalization",
        PhaseState.PASSED,
        "evidence finalization ok",
    )
    persist_report(
        report_dir,
        report,
        config_hash=fingerprints["config_hash"],
        toolchain_fingerprint=fingerprints["toolchain_fingerprint"],
        artifact_hash=fingerprints["artifact_hash"],
    )

    if report.release_state is not ReleaseState.PASSED:
        print(
            f"finalize refused: release state is {report.release_state.value}",
            file=sys.stderr,
        )
        return EXIT_INCOMPLETE if report.release_state is ReleaseState.INCOMPLETE else EXIT_FAIL

    try:
        outcome = write_and_verify_checksums(report_dir)
        if outcome.state is not PhaseState.PASSED:
            raise RuntimeError(outcome.detail)
    except Exception as exc:  # noqa: BLE001 - finalize must stay crash-consistent
        _invalidate_failed_finalization(
            report_dir,
            report,
            fingerprints=fingerprints,
            detail=str(exc),
        )
        print(f"finalize refused: {exc}", file=sys.stderr)
        return EXIT_FAIL

    print("finalize ok")
    return EXIT_PASS


def validate_finalization_inputs(report_dir: Path) -> PhaseOutcome:
    """Require the exact source DMG, report files, and portable artifact manifest."""
    report_dir = Path(report_dir)
    artifacts_path = report_dir / "artifacts.json"
    if not artifacts_path.is_file():
        return PhaseOutcome(PhaseState.FAILED, "missing artifacts.json manifest")

    try:
        artifacts = json.loads(artifacts_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return PhaseOutcome(PhaseState.FAILED, f"invalid artifacts.json: {exc}")
    if not isinstance(artifacts, dict):
        return PhaseOutcome(PhaseState.FAILED, "invalid artifacts.json")

    try:
        dmg = _resolve_source_dmg(artifacts)
    except ValueError as exc:
        return PhaseOutcome(PhaseState.FAILED, str(exc))
    if not dmg.is_file() or dmg.is_symlink():
        return PhaseOutcome(PhaseState.FAILED, f"exact DMG missing: {artifacts.get('dmg')}")
    expected_digest = str(artifacts.get("dmg_sha256", ""))
    actual_digest = sha256_file(dmg)
    if not expected_digest or actual_digest != expected_digest:
        return PhaseOutcome(
            PhaseState.FAILED,
            "DMG hash mismatch against artifacts.json",
        )

    missing = [
        name
        for name in ("report.json", "report.md", "artifacts.json")
        if not (report_dir / name).is_file()
    ]
    if missing:
        return PhaseOutcome(
            PhaseState.FAILED,
            f"missing finalization files: {', '.join(missing)}",
        )
    return PhaseOutcome(PhaseState.PASSED, "finalization inputs ok")


def write_and_verify_checksums(report_dir: Path) -> PhaseOutcome:
    """Stage portable release inputs, write SHA256SUMS, and verify via shasum + rehash."""
    report_dir = Path(report_dir)
    artifacts = json.loads((report_dir / "artifacts.json").read_text(encoding="utf-8"))
    try:
        source_dmg = _resolve_source_dmg(artifacts)
    except ValueError as exc:
        return PhaseOutcome(PhaseState.FAILED, str(exc))
    expected_digest = str(artifacts.get("dmg_sha256", ""))
    source_digest = sha256_file(source_dmg)
    if not expected_digest or source_digest != expected_digest:
        return PhaseOutcome(PhaseState.FAILED, "source DMG hash mismatch before staging")

    staged_dmg = report_dir / source_dmg.name
    if staged_dmg.exists() or staged_dmg.is_symlink():
        staged_dmg.unlink()
    shutil.copyfile(source_dmg, staged_dmg)
    if staged_dmg.is_symlink() or not staged_dmg.is_file():
        return PhaseOutcome(PhaseState.FAILED, "staged DMG is not a regular file")
    staged_digest = sha256_file(staged_dmg)
    if staged_digest != source_digest:
        return PhaseOutcome(PhaseState.FAILED, "staged DMG hash mismatch against source")

    version = _release_version_from_report(report_dir)
    final_md_name = f"release-qualification-{version}.md"
    final_md = report_dir / final_md_name
    report_md = report_dir / "report.md"
    if not report_md.is_file():
        return PhaseOutcome(PhaseState.FAILED, "missing report.md for immutable release copy")
    atomic_write_text(final_md, report_md.read_text(encoding="utf-8"))
    if final_md.is_symlink() or not final_md.is_file():
        return PhaseOutcome(PhaseState.FAILED, "immutable release report is not a regular file")

    entries: list[tuple[str, str]] = [
        (staged_digest, staged_dmg.name),
        (sha256_file(final_md), final_md.name),
    ]
    sums_path = report_dir / "SHA256SUMS"
    atomic_write_text(sums_path, "".join(f"{digest}  {name}\n" for digest, name in entries))
    if not sums_path.is_file():
        return PhaseOutcome(PhaseState.FAILED, "SHA256SUMS was not written")

    try:
        verify_sha256sums(sums_path, report_dir=report_dir)
        _verify_sha256sums_with_shasum(report_dir)
    except ValueError as exc:
        return PhaseOutcome(PhaseState.FAILED, str(exc))

    atomic_write_text(report_dir / FINALIZATION_COMPLETE_MARKER, "finalization-complete\n")
    if not (report_dir / FINALIZATION_COMPLETE_MARKER).is_file():
        return PhaseOutcome(PhaseState.FAILED, "finalization marker was not written")
    return PhaseOutcome(PhaseState.PASSED, "evidence finalization ok")


def verify_sha256sums(
    sums_path: Path,
    *,
    report_dir: Path,
) -> None:
    """Independently re-hash every basename listed in SHA256SUMS under report_dir."""
    if not sums_path.is_file():
        raise ValueError("SHA256SUMS missing")
    text = sums_path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError("SHA256SUMS empty")

    artifacts = json.loads((report_dir / "artifacts.json").read_text(encoding="utf-8"))
    source_dmg = _resolve_source_dmg(artifacts)
    version = _release_version_from_report(report_dir)
    required_names = {source_dmg.name, f"release-qualification-{version}.md"}
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        digest, sep, name = line.partition("  ")
        if not sep or not digest or not name:
            raise ValueError(f"invalid SHA256SUMS line: {raw!r}")
        if "/" in name or "\\" in name or name != Path(name).name:
            raise ValueError(f"SHA256SUMS entry must be basename-only: {name!r}")
        if name in seen:
            raise ValueError(f"duplicate SHA256SUMS entry: {name}")
        seen.add(name)
        target = (report_dir / name).resolve()
        if not target.is_relative_to(report_dir.resolve()):
            raise ValueError(f"SHA256SUMS path escapes report_dir: {name}")
        if not target.is_file() or target.is_symlink():
            raise ValueError(f"SHA256SUMS target missing: {name}")
        if sha256_file(target) != digest:
            raise ValueError(f"SHA256SUMS mismatch: {name}")

    missing = sorted(required_names - seen)
    if missing:
        raise ValueError(f"SHA256SUMS missing required entries: {', '.join(missing)}")
    extra = sorted(seen - required_names)
    if extra:
        raise ValueError(f"SHA256SUMS has unexpected entries: {', '.join(extra)}")


def cmd_status(args: argparse.Namespace) -> int:
    report_dir = Path(args.report_dir).resolve()
    _assert_report_dir(report_dir)
    payload = _load_report_payload(report_dir)
    state = str(payload.get("release_state", ReleaseState.INCOMPLETE.value))
    print(f"release_state={state}")
    for name in PHASE_REGISTRY:
        phase = (payload.get("phases") or {}).get(name, {})
        print(f"{name}={phase.get('state', 'not_started')}")
    if state == ReleaseState.PASSED.value:
        try:
            _assert_finalization_integrity(report_dir)
        except ValueError as exc:
            print(f"integrity failed: {exc}", file=sys.stderr)
            return EXIT_FAIL
        return EXIT_PASS
    if state == ReleaseState.FAILED.value:
        return EXIT_FAIL
    return EXIT_INCOMPLETE


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release_qualification.py",
        description="Local macOS release qualification (never publishes).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="Create evidence directory and config snapshot")
    prepare.add_argument("--version", required=True)
    prepare.add_argument("--final", action="store_true")
    prepare.add_argument("--config", default=str(DEFAULT_CONFIG))

    run = sub.add_parser("run", help="Run automated gates up to the manual checklist")
    run.add_argument("--report-dir", required=True)
    run.add_argument("--resume", action="store_true")

    observe = sub.add_parser("observe", help="Sample process budgets for a scenario")
    observe.add_argument("scenario", choices=OBSERVER_SCENARIOS)
    observe.add_argument("--report-dir", required=True)
    observe.add_argument("--seconds", type=float, default=None)
    observe.add_argument("--minutes", type=float, default=None)
    observe.add_argument("--app-bundle", default=None)
    observe.add_argument("--runtime-root", default=None)
    observe.add_argument(
        "--action-started-at",
        type=float,
        default=None,
        help=(
            "Monotonic timestamp when the external action started "
            "(warm-launch start, or shutdown close request)"
        ),
    )
    observe.add_argument(
        "--ui-ready-at",
        type=float,
        default=None,
        help="Monotonic timestamp when the UI reported ready",
    )

    record = sub.add_parser("record-check", help="Record a checklist result")
    record.add_argument("check_id", choices=CHECKLIST_IDS)
    record.add_argument("--result", required=True, choices=("pass", "fail", "incomplete"))
    record.add_argument("--notes", default="")
    record.add_argument("--evidence", default=None)
    record.add_argument("--metrics", default=None)
    record.add_argument("--report-dir", required=True)

    finalize = sub.add_parser("finalize", help="Finalize a fully passed qualification")
    finalize.add_argument("--report-dir", required=True)

    status = sub.add_parser("status", help="Show qualification status")
    status.add_argument("--report-dir", required=True)
    return parser


def _load_report_payload(report_dir: Path) -> dict:
    path = report_dir / "report.json"
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("invalid report.json")
    return payload


def _current_fingerprints(report_dir: Path) -> dict[str, str]:
    """Compute fresh resume fingerprints from the live workspace and artifacts."""
    config_path = report_dir / "config" / "release_qualification.toml"
    if not config_path.is_file():
        raise ResumeRejectedError("resume rejected: missing config snapshot")
    artifact_hash = _current_artifact_hash(report_dir)
    return {
        "commit": _git_commit(),
        "config_hash": sha256_file(config_path),
        "toolchain_fingerprint": compute_toolchain_fingerprint(),
        "artifact_hash": artifact_hash,
    }


def _current_artifact_hash(report_dir: Path) -> str:
    artifacts_path = report_dir / "artifacts.json"
    if not artifacts_path.is_file():
        return ""
    try:
        data = json.loads(artifacts_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ResumeRejectedError(f"resume rejected: invalid artifacts.json: {exc}") from exc
    if not isinstance(data, dict):
        raise ResumeRejectedError("resume rejected: invalid artifacts.json")
    try:
        dmg = _resolve_source_dmg(data)
    except ValueError as exc:
        raise ResumeRejectedError(f"resume rejected: {exc}") from exc
    if not dmg.is_file():
        # Manifest exists but DMG is gone → treat as changed/missing artifact.
        raise ResumeRejectedError("resume rejected: DMG artifact missing")
    dmg_hash = sha256_file(dmg)
    if "app_tree_sha256" not in data:
        raise ResumeRejectedError("resume rejected: missing app bundle fingerprint")
    try:
        app_hash = app_tree_digest(_resolve_repo_relative(data.get("app", "")))
    except (OSError, ValueError) as exc:
        raise ResumeRejectedError(f"resume rejected: {exc}") from exc
    if dmg_hash != data.get("dmg_sha256") or app_hash != data.get("app_tree_sha256"):
        raise ResumeRejectedError("resume rejected: qualified artifacts changed")
    return artifact_fingerprint(dmg_hash, app_hash)


def _assert_saved_fingerprints(
    payload: Mapping[str, object],
    current: Mapping[str, str],
) -> None:
    for key in ("commit", "config_hash", "toolchain_fingerprint", "artifact_hash"):
        saved = str(payload.get(key, ""))
        if saved != current[key]:
            raise ResumeRejectedError(
                f"resume rejected: {key} changed ({saved!r} != {current[key]!r})"
            )


def _assert_report_dir(report_dir: Path) -> None:
    allowed = (REPO_ROOT / DEFAULT_EVIDENCE_ROOT).resolve()
    resolved = report_dir.resolve()
    if not resolved.is_relative_to(allowed):
        raise SystemExit("report-dir must be under .artifacts/release-qualification")
    if not (resolved / SENTINEL_NAME).is_file():
        raise SystemExit("report-dir missing qualification sentinel")
    if not (resolved / "report.json").is_file():
        raise SystemExit("report-dir missing report.json")


def _exit_for_release_state(state: ReleaseState) -> int:
    if state is ReleaseState.PASSED:
        return EXIT_PASS
    if state is ReleaseState.FAILED:
        return EXIT_FAIL
    return EXIT_INCOMPLETE


def _git_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return "unknown"
    return completed.stdout.strip()


def _repo_relative_posix(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(f"path escapes repository root: {path}") from exc


def _resolve_repo_relative(raw: object) -> Path:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("missing repo-relative path")
    candidate = Path(text)
    if candidate.is_absolute():
        raise ValueError(f"absolute path not allowed in artifacts.json: {text}")
    if ".." in candidate.parts:
        raise ValueError(f"path traversal not allowed in artifacts.json: {text}")
    resolved = (REPO_ROOT / candidate).resolve()
    if not resolved.is_relative_to(REPO_ROOT.resolve()):
        raise ValueError(f"path escapes repository root: {text}")
    return resolved


def _resolve_source_dmg(artifacts: Mapping[str, object]) -> Path:
    app_raw = artifacts.get("app", "")
    if app_raw not in (None, ""):
        _resolve_repo_relative(app_raw)  # validate portability even if unused here
    return _resolve_repo_relative(artifacts.get("dmg", ""))


def _clear_stale_finalization_outputs(report_dir: Path) -> None:
    marker = report_dir / FINALIZATION_COMPLETE_MARKER
    if marker.exists() or marker.is_symlink():
        marker.unlink()
    sums = report_dir / "SHA256SUMS"
    if sums.exists() or sums.is_symlink():
        sums.unlink()


def _invalidate_failed_finalization(
    report_dir: Path,
    report: QualificationReport,
    *,
    fingerprints: Mapping[str, str],
    detail: str,
) -> None:
    marker = report_dir / FINALIZATION_COMPLETE_MARKER
    if marker.exists() or marker.is_symlink():
        marker.unlink()
    report.set_phase("evidence_finalization", PhaseState.FAILED, detail)
    persist_report(
        report_dir,
        report,
        config_hash=fingerprints["config_hash"],
        toolchain_fingerprint=fingerprints["toolchain_fingerprint"],
        artifact_hash=fingerprints["artifact_hash"],
    )


def _verify_sha256sums_with_shasum(report_dir: Path) -> None:
    completed = subprocess.run(
        ["shasum", "-a", "256", "-c", "SHA256SUMS"],
        cwd=str(report_dir),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "shasum -c failed").strip()
        raise ValueError(f"shasum verification failed: {detail}")


def _assert_finalization_integrity(report_dir: Path) -> None:
    marker = report_dir / FINALIZATION_COMPLETE_MARKER
    if not marker.is_file() or marker.is_symlink():
        raise ValueError("finalization-complete marker missing")
    sums_path = report_dir / "SHA256SUMS"
    verify_sha256sums(sums_path, report_dir=report_dir)
    _verify_sha256sums_with_shasum(report_dir)


def _release_version_from_report(report_dir: Path) -> str:
    prepare = report_dir / "prepare.json"
    if prepare.is_file():
        data = json.loads(prepare.read_text(encoding="utf-8"))
        return str(data.get("version", ""))
    payload = _load_report_payload(report_dir)
    return str(payload.get("release_id", "")).removesuffix("-final")


def _release_id_from_report(report_dir: Path) -> str:
    payload = _load_report_payload(report_dir)
    return str(payload["release_id"])


def _locate_app_bundle() -> Path:
    candidate = (
        REPO_ROOT
        / "desktop"
        / "src-tauri"
        / "target"
        / "release"
        / "bundle"
        / "macos"
        / "Orpheus.app"
    )
    return candidate


def _locate_dmg(version: str) -> Path:
    dmg_dir = REPO_ROOT / "desktop" / "src-tauri" / "target" / "release" / "bundle" / "dmg"
    return dmg_dir / f"Orpheus_{version}_aarch64.dmg"


if __name__ == "__main__":
    sys.exit(main())
