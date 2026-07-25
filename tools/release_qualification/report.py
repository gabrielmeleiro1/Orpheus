"""Deterministic human-readable release qualification reports."""

from __future__ import annotations

from .model import QualificationReport, ReleaseState


def render_markdown_report(report: QualificationReport) -> str:
    lines = [
        f"# Release qualification report: {report.release_id}",
        "",
        f"- Commit: `{report.commit}`",
        f"- Created: `{report.created_at}`",
        f"- Release state: `{report.release_state.value}`",
        "",
        "| Phase | State | Detail |",
        "| --- | --- | --- |",
    ]
    for name in report.required_phases:
        phase = report.phases[name]
        detail = phase.detail.replace("|", "\\|")
        lines.append(f"| {name} | {phase.state.value} | {detail} |")
    lines.append("")
    if report.release_state is ReleaseState.PASSED:
        lines.append("Candidate is releasable.")
    else:
        lines.append("Candidate is not releasable.")
    lines.append("")
    return "\n".join(lines)
