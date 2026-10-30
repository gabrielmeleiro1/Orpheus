"""Stable manual checklist for release qualification."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from uuid import uuid4

from .evidence import atomic_write_json, atomic_write_text

CHECKLIST_IDS: tuple[str, ...] = (
    "unsigned_first_open",
    "cold_launch",
    "warm_launch",
    "all_tabs_pointer",
    "all_tabs_keyboard",
    "file_picker_or_drop",
    "conversion_progress",
    "library_playback",
    "large_library_search_scroll",
    "idle_shutdown",
    "startup_shutdown",
    "active_operation_shutdown",
    "backend_recovery",
    "sleep_wake",
    "resize_scaling",
    "reduced_motion",
    "focus_and_labels",
    "visual_regression",
    "known_limitations_reviewed",
)

_METRICS_REQUIRED = frozenset(
    {
        "cold_launch",
        "warm_launch",
        "idle_shutdown",
        "startup_shutdown",
        "active_operation_shutdown",
        "large_library_search_scroll",
    }
)
_EVIDENCE_REQUIRED = frozenset(
    {
        "visual_regression",
        "unsigned_first_open",
        "conversion_progress",
        "library_playback",
    }
)


@dataclass(frozen=True)
class ChecklistRecord:
    check_id: str
    result: str
    notes: str
    evidence_path: str | None = None
    evidence_sha256: str | None = None
    metrics_path: str | None = None
    metrics_sha256: str | None = None


def required_fields_for(check_id: str) -> frozenset[str]:
    if check_id not in CHECKLIST_IDS:
        raise ValueError(f"unknown checklist id: {check_id}")
    fields = {"notes"}
    if check_id in _METRICS_REQUIRED:
        fields.add("metrics")
    if check_id in _EVIDENCE_REQUIRED:
        fields.add("evidence")
    return frozenset(fields)


def render_manual_checklist_markdown(*, release_id: str) -> str:
    lines = [
        f"# Manual checklist: {release_id}",
        "",
        "Record each item with `record-check`. Only `pass` counts toward release.",
        "",
    ]
    for check_id in CHECKLIST_IDS:
        req = ", ".join(sorted(required_fields_for(check_id)))
        lines.append(f"- [ ] `{check_id}` (requires: {req})")
    lines.append("")
    return "\n".join(lines)


def load_checklist(report_dir: Path) -> dict[str, object]:
    path = Path(report_dir) / "checklist.json"
    if not path.is_file():
        return {"checks": {check_id: None for check_id in CHECKLIST_IDS}}
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("invalid checklist payload")
    checks = payload.get("checks", {})
    if not isinstance(checks, dict):
        raise ValueError("invalid checklist checks")
    return payload


def materialize_under_report(
    report_dir: Path,
    source: Path,
    *,
    relative_dest: str,
) -> tuple[Path, str]:
    """Copy a regular file into report_dir with containment, symlink, and collision safety.

    Returns ``(destination, sha256_hex)``.
    """
    report_root = Path(report_dir).resolve()
    source_path = Path(source)
    if source_path.is_symlink() or os.path.islink(str(source_path)):
        raise ValueError("symlink evidence sources are rejected")
    if not source_path.exists():
        raise ValueError(f"evidence file not found: {source_path}")
    mode = source_path.lstat().st_mode
    if not stat.S_ISREG(mode):
        raise ValueError(f"evidence must be a regular file: {source_path}")

    rel = Path(relative_dest)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError("evidence destination escapes report containment")
    dest = (report_root / rel).resolve()
    if not dest.is_relative_to(report_root):
        raise ValueError("evidence destination escapes report containment")
    if dest.exists():
        stem = dest.stem
        suffix = dest.suffix
        dest = dest.with_name(f"{stem}-{uuid4().hex[:8]}{suffix}")
        dest = dest.resolve()
        if not dest.is_relative_to(report_root):
            raise ValueError("evidence destination escapes report containment")

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, dest)
    if dest.is_symlink() or not dest.is_file():
        dest.unlink(missing_ok=True)
        raise ValueError("copied evidence must be a regular file under report_dir")
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    return dest, digest


def record_check(
    report_dir: Path,
    *,
    check_id: str,
    result: str,
    notes: str,
    evidence: Path | None = None,
    metrics: Path | None = None,
    dest_name: str | None = None,
    metrics_dest_name: str | None = None,
) -> ChecklistRecord:
    if check_id not in CHECKLIST_IDS:
        raise ValueError(f"unknown checklist id: {check_id}")
    if result not in {"pass", "fail", "incomplete"}:
        raise ValueError("result must be pass, fail, or incomplete")

    required = required_fields_for(check_id)
    cleaned_notes = (notes or "").strip()
    if result == "pass":
        if "notes" in required and not cleaned_notes:
            raise ValueError("pass requires non-empty notes")
        if "metrics" in required and metrics is None:
            raise ValueError("pass requires metrics evidence")
        if "evidence" in required and evidence is None:
            raise ValueError("pass requires evidence file")

    report_dir = Path(report_dir)
    evidence_path: str | None = None
    evidence_sha: str | None = None
    metrics_path: str | None = None
    metrics_sha: str | None = None

    if evidence is not None:
        source = Path(evidence)
        suffix = source.suffix or ".bin"
        relative = dest_name if dest_name is not None else f"screenshots/{check_id}{suffix}"
        dest, evidence_sha = materialize_under_report(
            report_dir,
            source,
            relative_dest=relative,
        )
        evidence_path = str(dest.relative_to(report_dir.resolve()))

    if metrics is not None:
        source = Path(metrics)
        relative = (
            metrics_dest_name
            if metrics_dest_name is not None
            else f"metrics/{check_id}-{Path(source.name).name}"
        )
        dest, metrics_sha = materialize_under_report(
            report_dir,
            source,
            relative_dest=relative,
        )
        metrics_path = str(dest.relative_to(report_dir.resolve()))

    record = ChecklistRecord(
        check_id=check_id,
        result=result,
        notes=cleaned_notes,
        evidence_path=evidence_path,
        evidence_sha256=evidence_sha,
        metrics_path=metrics_path,
        metrics_sha256=metrics_sha,
    )
    payload = load_checklist(report_dir)
    checks = dict(payload.get("checks") or {})
    checks[check_id] = {
        "check_id": record.check_id,
        "result": record.result,
        "notes": record.notes,
        "evidence_path": record.evidence_path,
        "evidence_sha256": record.evidence_sha256,
        "metrics_path": record.metrics_path,
        "metrics_sha256": record.metrics_sha256,
    }
    payload["checks"] = checks
    atomic_write_json(report_dir / "checklist.json", payload)
    atomic_write_text(
        report_dir / "manual-checklist.md",
        _render_progress_markdown(report_dir.name, checks),
    )
    return record


def checklist_finalize_gate(
    payload: Mapping[str, object],
    *,
    report_dir: Path | None,
) -> tuple[bool, str, int]:
    """Return ``(blocked, reason, exit_code)`` where exit 1=fail, 2=incomplete."""
    checks = payload.get("checks") or {}
    if not isinstance(checks, dict):
        return True, "invalid checklist", 2

    saw_failure = False
    incomplete_reason = ""
    for check_id in CHECKLIST_IDS:
        item = checks.get(check_id)
        if item is None:
            incomplete_reason = incomplete_reason or f"missing checklist item: {check_id}"
            continue
        if not isinstance(item, dict):
            return True, f"invalid checklist item: {check_id}", 2
        result = str(item.get("result", "incomplete"))
        if result == "fail":
            saw_failure = True
            incomplete_reason = incomplete_reason or (
                f"checklist item not passed: {check_id} ({result})"
            )
            continue
        if result != "pass":
            incomplete_reason = incomplete_reason or (
                f"checklist item not passed: {check_id} ({result})"
            )
            continue

        required = required_fields_for(check_id)
        notes = str(item.get("notes") or "").strip()
        if "notes" in required and not notes:
            incomplete_reason = incomplete_reason or f"missing notes for passed item: {check_id}"
            continue

        if report_dir is not None:
            try:
                _revalidate_passed_item(report_dir, check_id, item, required)
            except ValueError as exc:
                return True, str(exc), 1

    if saw_failure:
        return True, incomplete_reason or "checklist contains failures", 1
    if incomplete_reason:
        return True, incomplete_reason, 2
    return False, "ok", 0


def _revalidate_passed_item(
    report_dir: Path,
    check_id: str,
    item: Mapping[str, object],
    required: frozenset[str],
) -> None:
    report_root = Path(report_dir).resolve()
    if "evidence" in required:
        rel = item.get("evidence_path")
        digest = item.get("evidence_sha256")
        if not rel or not digest:
            raise ValueError(f"passed item missing evidence hash: {check_id}")
        path = (report_root / str(rel)).resolve()
        if not path.is_relative_to(report_root):
            raise ValueError(f"evidence path escapes report_dir: {check_id}")
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"evidence missing or not a regular file: {check_id}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != str(digest):
            raise ValueError(f"evidence hash mismatch: {check_id}")

    if "metrics" in required:
        rel = item.get("metrics_path")
        digest = item.get("metrics_sha256")
        if not rel or not digest:
            raise ValueError(f"passed item missing metrics hash: {check_id}")
        path = (report_root / str(rel)).resolve()
        if not path.is_relative_to(report_root):
            raise ValueError(f"metrics path escapes report_dir: {check_id}")
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"metrics missing or not a regular file: {check_id}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != str(digest):
            raise ValueError(f"metrics hash mismatch: {check_id}")


def _render_progress_markdown(release_id: str, checks: Mapping[str, object]) -> str:
    lines = [
        f"# Manual checklist: {release_id}",
        "",
    ]
    for check_id in CHECKLIST_IDS:
        item = checks.get(check_id)
        if isinstance(item, dict) and item.get("result") == "pass":
            mark = "x"
        else:
            mark = " "
        state = ""
        if isinstance(item, dict):
            state = f" — {item.get('result', 'incomplete')}"
        lines.append(f"- [{mark}] `{check_id}`{state}")
    lines.append("")
    return "\n".join(lines)
