"""Fixed-order resumable phase engine for local release qualification."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Union

from .evidence import atomic_write_json, atomic_write_text, redact_secrets
from .model import REQUIRED_PHASES, PhaseState, QualificationReport
from .report import render_markdown_report

PHASE_REGISTRY: tuple[str, ...] = REQUIRED_PHASES


@dataclass(frozen=True)
class PhaseOutcome:
    state: PhaseState
    detail: str = ""


PhaseHandler = Callable[[str, Path], Union[PhaseOutcome, PhaseState, None]]


class ResumeRejectedError(ValueError):
    """Raised when resume fingerprints do not match the saved report."""


@dataclass
class PhaseEngine:
    report_dir: Path
    release_id: str
    commit: str
    config_hash: str
    toolchain_fingerprint: str
    artifact_hash: str
    phase_handlers: Mapping[str, PhaseHandler]
    username: str | None = None

    def __post_init__(self) -> None:
        self.report_dir = Path(self.report_dir)
        if self.username is None:
            self.username = os.environ.get("USER") or os.environ.get("USERNAME")
        missing = [name for name in PHASE_REGISTRY if name not in self.phase_handlers]
        if missing:
            raise ValueError(f"missing phase handlers: {missing}")

    def run(self) -> QualificationReport:
        report = QualificationReport.new(release_id=self.release_id, commit=self.commit)
        self._persist(report)
        return self._execute(report, start_index=0)

    def resume(self) -> QualificationReport:
        payload = self._load_payload()
        self._assert_fingerprints(payload)
        report = self._report_from_payload(payload)
        start_index = self._resume_index(report)
        return self._execute(report, start_index=start_index)

    def _execute(self, report: QualificationReport, *, start_index: int) -> QualificationReport:
        for name in PHASE_REGISTRY[start_index:]:
            report.set_phase(name, PhaseState.RUNNING, "running")
            self._persist(report)
            try:
                raw = self.phase_handlers[name](name, self.report_dir)
                outcome = self._normalize_outcome(raw)
            except Exception as exc:  # noqa: BLE001 - phase boundary records all failures
                detail = redact_secrets(str(exc), username=self.username)
                report.set_phase(name, PhaseState.FAILED, detail)
                self._mark_downstream_blocked(report, blocked_by=name)
                self._persist(report)
                return report

            if outcome.state is PhaseState.PASSED:
                report.set_phase(name, PhaseState.PASSED, outcome.detail or "ok")
                self._persist(report)
                continue

            detail = outcome.detail or outcome.state.value
            report.set_phase(name, outcome.state, redact_secrets(detail, username=self.username))
            self._mark_downstream_blocked(report, blocked_by=name)
            self._persist(report)
            return report
        self._persist(report)
        return report

    @staticmethod
    def _normalize_outcome(raw: PhaseOutcome | PhaseState | None) -> PhaseOutcome:
        if raw is None:
            return PhaseOutcome(PhaseState.PASSED, "ok")
        if isinstance(raw, PhaseState):
            detail = "ok" if raw is PhaseState.PASSED else raw.value
            return PhaseOutcome(raw, detail)
        if isinstance(raw, PhaseOutcome):
            return raw
        raise TypeError(f"phase handler returned unsupported outcome: {type(raw)!r}")

    def _mark_downstream_blocked(self, report: QualificationReport, *, blocked_by: str) -> None:
        blocking = False
        reason = f"blocked by upstream phase: {blocked_by}"
        for name in PHASE_REGISTRY:
            if name == blocked_by:
                blocking = True
                continue
            if not blocking:
                continue
            report.set_phase(name, PhaseState.SKIPPED, reason)

    def _persist(self, report: QualificationReport) -> None:
        self._sync_artifact_hash_from_manifest()
        payload = {
            "release_id": report.release_id,
            "commit": report.commit,
            "created_at": report.created_at,
            "config_hash": self.config_hash,
            "toolchain_fingerprint": self.toolchain_fingerprint,
            "artifact_hash": self.artifact_hash,
            "release_state": report.release_state.value,
            "required_phases": list(report.required_phases),
            "phases": {
                name: {
                    "name": record.name,
                    "state": record.state.value,
                    "detail": redact_secrets(record.detail, username=self.username),
                    "updated_at": record.updated_at,
                }
                for name, record in report.phases.items()
            },
        }
        # Keep in-memory details redacted too once persisted.
        for name, record in list(report.phases.items()):
            redacted = redact_secrets(record.detail, username=self.username)
            if redacted != record.detail:
                report.phases[name] = type(record)(
                    name=record.name,
                    state=record.state,
                    detail=redacted,
                    updated_at=record.updated_at,
                )
                payload["phases"][name]["detail"] = redacted
        atomic_write_json(self.report_dir / "report.json", payload)
        markdown = render_markdown_report(report)
        atomic_write_text(self.report_dir / "report.md", markdown)

    def _load_payload(self) -> dict:
        path = self.report_dir / "report.json"
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)

    def _sync_artifact_hash_from_manifest(self) -> None:
        """Refresh artifact fingerprint when the bundle manifest is first written."""
        artifacts_path = self.report_dir / "artifacts.json"
        if not artifacts_path.is_file():
            return
        try:
            with artifacts_path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        digest = data.get("artifact_hash", data.get("dmg_sha256"))
        if isinstance(digest, str) and digest:
            self.artifact_hash = digest

    def _assert_fingerprints(self, payload: Mapping[str, object]) -> None:
        expected = {
            "release_id": self.release_id,
            "commit": self.commit,
            "config_hash": self.config_hash,
            "toolchain_fingerprint": self.toolchain_fingerprint,
            "artifact_hash": self.artifact_hash,
        }
        for key, value in expected.items():
            if payload.get(key) != value:
                raise ResumeRejectedError(
                    f"resume rejected: {key} changed ({payload.get(key)!r} != {value!r})"
                )

    def _report_from_payload(self, payload: Mapping[str, object]) -> QualificationReport:
        report = QualificationReport.new(
            release_id=str(payload["release_id"]),
            commit=str(payload["commit"]),
        )
        report.created_at = str(payload.get("created_at", report.created_at))
        phases = payload.get("phases", {})
        if not isinstance(phases, dict):
            raise ResumeRejectedError("resume rejected: invalid phases payload")
        for name in report.required_phases:
            raw = phases.get(name, {})
            if not isinstance(raw, dict):
                continue
            state = PhaseState(str(raw.get("state", PhaseState.NOT_STARTED.value)))
            detail = redact_secrets(str(raw.get("detail", "")), username=self.username)
            updated_at = str(raw.get("updated_at", report.created_at))
            report.phases[name] = type(report.phases[name])(
                name=name,
                state=state,
                detail=detail,
                updated_at=updated_at,
            )
        return report

    def _resume_index(self, report: QualificationReport) -> int:
        for index, name in enumerate(PHASE_REGISTRY):
            state = report.phases[name].state
            if state in {PhaseState.FAILED, PhaseState.INCOMPLETE, PhaseState.RUNNING}:
                return index
            if state is PhaseState.NOT_STARTED:
                return index
        return len(PHASE_REGISTRY)
