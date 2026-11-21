"""Tests for safe evidence directories and report rendering."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tools.release_qualification.evidence import (
    DEFAULT_EVIDENCE_ROOT,
    EvidenceRoot,
    atomic_write_json,
    redact_secrets,
)
from tools.release_qualification.model import PhaseState, QualificationReport
from tools.release_qualification.report import render_markdown_report

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_default_evidence_root_is_under_artifacts_release_qualification():
    assert DEFAULT_EVIDENCE_ROOT == Path(".artifacts") / "release-qualification"
    root = EvidenceRoot.create(release_id="1.0.0-test", base=REPO_ROOT / DEFAULT_EVIDENCE_ROOT)
    resolved = root.path.resolve()
    assert (REPO_ROOT / ".artifacts" / "release-qualification").resolve() in resolved.parents or (
        resolved.parent == (REPO_ROOT / ".artifacts" / "release-qualification").resolve()
    )
    assert root.path.is_dir()
    root.cleanup()


def test_evidence_base_must_be_under_repo_artifacts_release_qualification(tmp_path: Path):
    allowed = (REPO_ROOT / DEFAULT_EVIDENCE_ROOT).resolve()
    with pytest.raises(ValueError, match="unsafe|reject|outside|artifacts"):
        EvidenceRoot.create(release_id="1.0.0-test", base=tmp_path / "release-qualification")
    with pytest.raises(ValueError, match="unsafe|reject|outside|artifacts"):
        EvidenceRoot.create(release_id="1.0.0-test", base=Path("/Users"))
    with pytest.raises(ValueError, match="unsafe|reject|outside|artifacts"):
        EvidenceRoot.create(release_id="1.0.0-test", base=REPO_ROOT / ".artifacts")
    # Exact sentinel tree root is allowed.
    root = EvidenceRoot.create(release_id="1.0.0-allowed-base", base=allowed)
    assert root.path.resolve().is_relative_to(allowed)
    root.cleanup()


def test_evidence_creation_rejects_unsafe_paths(tmp_path: Path):
    with pytest.raises(ValueError, match="unsafe|reject|outside"):
        EvidenceRoot.create(release_id="1.0.0-test", base=Path("/"))

    with pytest.raises(ValueError, match="unsafe|reject|outside|home|artifacts"):
        EvidenceRoot.create(release_id="1.0.0-test", base=Path.home())

    with pytest.raises(ValueError, match="unsafe|reject|outside|repository|artifacts"):
        EvidenceRoot.create(release_id="1.0.0-test", base=REPO_ROOT)

    allowed = REPO_ROOT / DEFAULT_EVIDENCE_ROOT
    with pytest.raises(ValueError, match="unsafe|reject|outside|traversal"):
        EvidenceRoot.create(
            release_id="1.0.0-test",
            base=allowed,
            release_dirname="../escape",
        )


def test_sentinel_contains_release_id_and_cleanup_requires_match(tmp_path: Path):
    base = REPO_ROOT / DEFAULT_EVIDENCE_ROOT
    root = EvidenceRoot.create(release_id="1.0.0-test-sentinel", base=base)
    sentinel = root.path / ".orpheus-release-qualification"
    assert sentinel.is_file()
    assert "1.0.0-test-sentinel" in sentinel.read_text(encoding="utf-8")

    mismatched = EvidenceRoot(
        path=root.path,
        release_id="other-id",
        expected_root=base.resolve(),
    )
    with pytest.raises(ValueError, match="sentinel"):
        mismatched.cleanup()

    sentinel.unlink()
    with pytest.raises(ValueError, match="sentinel"):
        root.cleanup()

    # Recreate a valid root to clean up the leftover directory for the suite.
    sentinel.write_text("1.0.0-test-sentinel\n", encoding="utf-8")
    root.cleanup()
    assert not root.path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory permissions")
def test_cleanup_removes_read_only_fixture_copies():
    root = EvidenceRoot.create(release_id="1.0.0-read-only", base=REPO_ROOT / DEFAULT_EVIDENCE_ROOT)
    read_only = root.path / "fixtures" / "scenarios" / "read-only-copy" / "data"
    read_only.mkdir(parents=True)
    (read_only / "audiobooks.db").write_bytes(b"db")
    for path in (read_only / "audiobooks.db", read_only, read_only.parent):
        path.chmod(0o500 if path.is_dir() else 0o400)

    root.cleanup()

    assert not root.path.exists()


def test_atomic_json_write_keeps_previous_report_when_a_write_fails(tmp_path: Path):
    target = tmp_path / "report.json"
    atomic_write_json(target, {"ok": True, "n": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True, "n": 1}

    # Serialization fails part-way through the payload.
    with pytest.raises(TypeError):
        atomic_write_json(target, {"ok": False, "z": object()})

    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True, "n": 1}
    assert [path.name for path in tmp_path.iterdir()] == ["report.json"]


def test_redact_secrets_removes_tokens_credentials_keys_and_username():
    username = os.environ.get("USER") or os.environ.get("USERNAME") or "testuser"
    text = (
        f"Authorization: Bearer super-secret-token\n"
        f"https://alice:hunter2@example.com/path\n"
        f"api_key=AKIA0123456789ABCDEF\n"
        f"home=/Users/{username}/Library/Application Support/Orpheus\n"
    )
    redacted = redact_secrets(text, username=username)

    assert "super-secret-token" not in redacted
    assert "hunter2" not in redacted
    assert "AKIA0123456789ABCDEF" not in redacted
    assert username not in redacted
    assert "Bearer" in redacted or "[REDACTED]" in redacted
    assert "[REDACTED]" in redacted


def test_username_redaction_is_boundary_and_path_aware():
    # Common short usernames must not corrupt unrelated substrings.
    assert redact_secrets("remaining", username="main") == "remaining"
    assert redact_secrets("domain", username="main") == "domain"
    assert "/Users/[REDACTED]/x" in redact_secrets("/Users/alice/x", username="alice")
    assert "/home/[REDACTED]/proj" in redact_secrets("/home/main/proj", username="main")
    # Bare token only when it is a standalone path segment / home prefix form.
    assert "Users/[REDACTED]" in redact_secrets(r"C:\Users\main\AppData", username="main")


def _stream_redact(parts: list[str], *, username: str) -> str:
    from tools.release_qualification.evidence import StreamingSecretRedactor

    redactor = StreamingSecretRedactor(username=username)
    out: list[str] = []
    for part in parts:
        out.append(redactor.feed(part))
    out.append(redactor.flush())
    return "".join(out)


@pytest.mark.parametrize(
    ("parts", "leaked"),
    [
        (["Bearer ", "super-secret-token trailing"], "super-secret-token"),
        (["https://alice:", "hunter2@example.com/path"], "hunter2"),
        (["api_key=", "Z" * 16 + " ok"], "Z" * 16),
        (["prefix AKIA", "0123456789ABCDEF suffix"], "AKIA0123456789ABCDEF"),
        (["/Users/", "splituser/project"], "splituser"),
    ],
)
def test_streaming_redactor_hides_secrets_split_across_chunks(
    parts: list[str], leaked: str
):
    username = "splituser"
    combined = _stream_redact(parts, username=username)
    assert leaked not in combined
    assert "[REDACTED]" in combined


def test_markdown_phase_table_is_deterministic():
    report = QualificationReport.new(release_id="1.0.0-test", commit="abc123")
    report.set_phase("preflight", PhaseState.PASSED, "ok")
    report.set_phase("computer_use", PhaseState.INCOMPLETE, "not recorded")

    first = render_markdown_report(report)
    second = render_markdown_report(report)

    assert first == second
    assert "| Phase | State | Detail |" in first
    assert "| preflight | passed | ok |" in first
    assert "| computer_use | incomplete | not recorded |" in first
    for phase in report.required_phases:
        assert f"| {phase} |" in first
