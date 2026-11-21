"""Tests for the bounded command runner and resumable phase engine."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from tools.release_qualification.commands import CommandResult, CommandSpec, run_command
from tools.release_qualification.engine import (
    PHASE_REGISTRY,
    PhaseEngine,
    ResumeRejectedError,
)
from tools.release_qualification.evidence import DEFAULT_EVIDENCE_ROOT, EvidenceRoot, redact_secrets
from tools.release_qualification.model import PhaseState, ReleaseState

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_BASE = REPO_ROOT / DEFAULT_EVIDENCE_ROOT


@pytest.mark.skipif(os.name == "nt", reason="Release command runner requires POSIX process groups")
def test_command_result_argv_elements_are_sanitized(tmp_path: Path):
    log_dir = tmp_path / "commands"
    secret = "super-secret-token"
    # Synthetic name that cannot occur in the interpreter or checkout path.
    username = "qualuser7f3a"
    spec = CommandSpec(
        label="sanitize-argv",
        argv=[
            sys.executable,
            "-c",
            "print('ok')",
            f"Bearer {secret}",
            "https://alice:hunter2@example.com/path",
            f"/Users/{username}/secret-file",
        ],
        cwd=str(tmp_path),
    )
    result = run_command(spec, log_dir=log_dir, username=username)
    assert result.exit_code == 0
    joined = " ".join(result.argv)
    assert secret not in joined
    assert "hunter2" not in joined
    assert username not in joined
    assert result.argv[-1] == "/Users/[REDACTED]/secret-file"
    assert all(isinstance(part, str) for part in result.argv)
    assert len(result.argv) == len(spec.argv)


def test_bounded_log_enforces_utf8_byte_budget_not_characters(tmp_path: Path):
    from tools.release_qualification.commands import _BoundedLog

    path = tmp_path / "utf8.log"
    # Each '€' is 3 UTF-8 bytes. A 10-character budget would wrongly keep all 10
    # code points (30 bytes); a real byte budget must stop earlier.
    bounded = _BoundedLog(path, max_bytes=10)
    bounded.write("€" * 10, stream="stdout")
    bounded.close()
    raw = path.read_bytes()
    # Truncation marker may add bytes after the budgeted payload.
    payload, marker, _rest = raw.partition(b"\n[")
    assert len(payload) <= 10
    assert marker.startswith(b"\n[")
    assert b"truncated" in raw


def test_bounded_log_budget_edge_keeps_valid_utf8_without_exceeding_budget(tmp_path: Path):
    from tools.release_qualification.commands import _BoundedLog

    path = tmp_path / "utf8-valid.log"
    # max_bytes=10 with 3-byte '€' yields data[:10] == 3 full chars + leading 0xe2.
    # Trimming only continuation bytes would keep that incomplete leading byte.
    bounded = _BoundedLog(path, max_bytes=10)
    bounded.write("€" * 10, stream="stdout")
    bounded.close()
    raw = path.read_bytes()
    payload, marker, _rest = raw.partition(b"\n[")
    assert len(payload) <= 10
    assert marker.startswith(b"\n[")
    payload.decode("utf-8")  # must not raise
    assert not payload.endswith(b"\xe2")


def test_command_spec_requires_argv_array_not_shell_string():
    with pytest.raises(TypeError, match="argv|sequence|list"):
        CommandSpec(label="bad", argv="echo hello", cwd=".")  # type: ignore[arg-type]

    spec = CommandSpec(label="ok", argv=[sys.executable, "-c", "print(1)"], cwd=".")
    assert isinstance(spec.argv, tuple)


@pytest.mark.skipif(os.name == "nt", reason="Release command runner requires POSIX process groups")
def test_run_command_uses_explicit_cwd_and_sanitized_env(tmp_path: Path):
    log_dir = tmp_path / "commands"
    marker = tmp_path / "workdir"
    marker.mkdir()
    script = (
        "import os, pathlib, sys;"
        f"pathlib.Path({str(marker / 'cwd.txt')!r}).write_text(os.getcwd());"
        f"pathlib.Path({str(marker / 'secret.txt')!r}).write_text(os.environ.get('SECRET','missing'));"
        f"pathlib.Path({str(marker / 'path.txt')!r}).write_text(os.environ.get('PATH',''));"
        "print('hello')"
    )
    spec = CommandSpec(
        label="env-check",
        argv=[sys.executable, "-c", script],
        cwd=str(marker),
        env={"SECRET": "should-not-appear", "PATH": "/usr/bin"},
    )
    result = run_command(spec, log_dir=log_dir, env_allowlist=("PATH",))

    assert result.exit_code == 0
    assert result.timed_out is False
    assert Path(marker / "cwd.txt").read_text() == str(marker.resolve())
    assert Path(marker / "secret.txt").read_text() == "missing"
    assert Path(marker / "path.txt").read_text() == "/usr/bin"
    assert result.log_path.endswith(".log")
    log_text = (log_dir / Path(result.log_path).name).read_text(encoding="utf-8")
    assert "hello" in log_text
    assert "should-not-appear" not in log_text


@pytest.mark.skipif(os.name == "nt", reason="Release command runner requires POSIX process groups")
def test_stdout_and_stderr_stream_to_bounded_log_files(tmp_path: Path):
    log_dir = tmp_path / "commands"
    # Emit more than the bound; runner must truncate rather than grow without limit.
    script = (
        "import sys;"
        "sys.stdout.write('A' * 5000);"
        "sys.stderr.write('B' * 5000);"
    )
    spec = CommandSpec(
        label="bounded-log",
        argv=[sys.executable, "-c", script],
        cwd=str(tmp_path),
    )
    result = run_command(spec, log_dir=log_dir, max_log_bytes=1024)
    log_file = log_dir / Path(result.log_path).name
    assert log_file.stat().st_size <= 2048 + 256  # stdout+stderr bounds plus markers
    content = log_file.read_text(encoding="utf-8", errors="replace")
    assert "truncated" in content.lower()


@pytest.mark.skipif(os.name == "nt", reason="Release command runner requires POSIX process groups")
def test_timeout_terminates_process_group_and_records_failure(tmp_path: Path):
    log_dir = tmp_path / "commands"
    script = "import time; time.sleep(30)"
    spec = CommandSpec(
        label="timeout",
        argv=[sys.executable, "-c", script],
        cwd=str(tmp_path),
        timeout_seconds=0.5,
    )
    started = time.monotonic()
    result = run_command(spec, log_dir=log_dir)
    elapsed = time.monotonic() - started

    assert result.timed_out is True
    assert result.exit_code != 0
    assert elapsed < 10
    assert isinstance(result, CommandResult)
    assert result.label == "timeout"
    assert result.started_at
    assert result.ended_at
    assert result.duration_seconds >= 0


@pytest.mark.skipif(os.name == "nt", reason="Release command runner requires POSIX process groups")
def test_timeout_kills_a_command_that_ignores_sigterm_after_the_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from tools.release_qualification import commands

    monkeypatch.setattr(commands, "TERMINATE_GRACE_SECONDS", 0.5)
    script = (
        "import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "while True:\n    print('still here', flush=True); time.sleep(0.05)"
    )
    spec = CommandSpec(
        label="ignores-term",
        argv=[sys.executable, "-c", script],
        cwd=str(tmp_path),
        timeout_seconds=0.5,
    )
    started = time.monotonic()
    result = run_command(spec, log_dir=tmp_path / "commands")

    assert result.timed_out is True
    assert result.exit_code != 0
    assert time.monotonic() - started < 5


@pytest.mark.skipif(os.name == "nt", reason="Release command runner requires POSIX process groups")
def test_post_exit_drain_collects_late_pipe_data(tmp_path: Path):
    """After the child exits, keep draining briefly so delayed pipe bytes are not dropped.

    Reproduces the deadline race: the watched process exits quickly while a
    forked writer still holds the pipe, then the command deadline expires with
    an empty select. A single extra 0.1s wait is not enough for the late write.
    """
    log_dir = tmp_path / "commands"
    marker = "late-pipe-data-marker"
    script = f"""
import os, sys, time
sys.stdout.write("early\\n")
sys.stdout.flush()
if os.fork() == 0:
    time.sleep(0.35)
    sys.stdout.write({marker!r} + "\\n")
    sys.stdout.flush()
    os._exit(0)
"""
    spec = CommandSpec(
        label="late-drain",
        argv=[sys.executable, "-c", script],
        cwd=str(tmp_path),
        timeout_seconds=0.15,
    )
    result = run_command(spec, log_dir=log_dir, max_log_bytes=10_000)
    log_text = (log_dir / Path(result.log_path).name).read_text(encoding="utf-8")
    assert marker in log_text


@pytest.mark.skipif(os.name == "nt", reason="Release command runner requires POSIX process groups")
def test_timeout_kills_grandchild_in_same_process_group(tmp_path: Path):
    log_dir = tmp_path / "commands"
    pid_file = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys, time;"
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
        f"open({str(pid_file)!r}, 'w', encoding='utf-8').write(str(child.pid));"
        "time.sleep(60)"
    )
    spec = CommandSpec(
        label="timeout-grandchild",
        argv=[sys.executable, "-c", script],
        cwd=str(tmp_path),
        timeout_seconds=0.5,
    )
    result = run_command(spec, log_dir=log_dir)
    assert result.timed_out is True
    assert result.exit_code != 0
    # Allow the kernel a moment to reap the killed grandchild.
    deadline = time.monotonic() + 2.0
    grandchild_pid = None
    while time.monotonic() < deadline:
        if pid_file.is_file() and pid_file.read_text(encoding="utf-8").strip():
            grandchild_pid = int(pid_file.read_text(encoding="utf-8").strip())
            break
        time.sleep(0.05)
    assert grandchild_pid is not None
    alive = True
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            alive = False
            break
        time.sleep(0.05)
    assert alive is False


def test_failure_stops_downstream_phases_but_writes_report(tmp_path: Path):
    evidence = EvidenceRoot.create(release_id="1.0.0-engine", base=EVIDENCE_BASE)
    executed: list[str] = []

    def handler(phase_name: str, report_dir: Path) -> None:
        executed.append(phase_name)
        if phase_name == "source_validation":
            raise RuntimeError("contract failed")

    engine = PhaseEngine(
        report_dir=evidence.path,
        release_id="1.0.0-engine",
        commit="abc123",
        config_hash="cfg1",
        toolchain_fingerprint="tool1",
        artifact_hash="art1",
        phase_handlers={name: handler for name in PHASE_REGISTRY},
    )
    report = engine.run()

    assert "source_validation" in executed
    assert "sidecar_validation" not in executed
    assert report.phases["source_validation"].state is PhaseState.FAILED
    assert report.phases["sidecar_validation"].state is PhaseState.SKIPPED
    assert "blocked by upstream" in report.phases["sidecar_validation"].detail.lower()
    assert report.phases["computer_use"].state is PhaseState.SKIPPED
    assert report.release_state is ReleaseState.FAILED
    assert (evidence.path / "report.json").is_file()
    assert (evidence.path / "report.md").is_file()
    evidence.cleanup()


def test_phase_engine_persists_incomplete_and_skipped_outcomes(tmp_path: Path):
    from tools.release_qualification.engine import PhaseOutcome

    evidence = EvidenceRoot.create(release_id="1.0.0-outcomes", base=EVIDENCE_BASE)
    executed: list[str] = []

    def handler(phase_name: str, report_dir: Path):
        executed.append(phase_name)
        if phase_name == "computer_use":
            return PhaseOutcome(PhaseState.INCOMPLETE, "operator recording deferred")
        return None

    engine = PhaseEngine(
        report_dir=evidence.path,
        release_id="1.0.0-outcomes",
        commit="abc123",
        config_hash="cfg1",
        toolchain_fingerprint="tool1",
        artifact_hash="art1",
        phase_handlers={name: handler for name in PHASE_REGISTRY},
    )
    report = engine.run()
    assert report.phases["computer_use"].state is PhaseState.INCOMPLETE
    assert "deferred" in report.phases["computer_use"].detail
    assert report.phases["evidence_finalization"].state is PhaseState.SKIPPED
    assert "blocked by upstream" in report.phases["evidence_finalization"].detail.lower()
    assert report.release_state is ReleaseState.INCOMPLETE
    assert "evidence_finalization" not in executed
    payload = (evidence.path / "report.json").read_text(encoding="utf-8")
    assert '"incomplete"' in payload
    assert '"skipped"' in payload
    evidence.cleanup()


def test_resume_reruns_first_failed_or_incomplete_and_downstream(tmp_path: Path):
    evidence = EvidenceRoot.create(release_id="1.0.0-resume", base=EVIDENCE_BASE)
    executed: list[str] = []
    source_attempts = {"count": 0}

    def handler(phase_name: str, report_dir: Path) -> None:
        executed.append(phase_name)
        if phase_name == "source_validation":
            source_attempts["count"] += 1
            if source_attempts["count"] == 1:
                raise RuntimeError("first failure")

    engine = PhaseEngine(
        report_dir=evidence.path,
        release_id="1.0.0-resume",
        commit="abc123",
        config_hash="cfg1",
        toolchain_fingerprint="tool1",
        artifact_hash="art1",
        phase_handlers={name: handler for name in PHASE_REGISTRY},
    )
    first = engine.run()
    assert first.phases["source_validation"].state is PhaseState.FAILED

    executed.clear()
    resumed = engine.resume()
    assert executed[0] == "source_validation"
    assert "preflight" not in executed
    assert resumed.phases["source_validation"].state is PhaseState.PASSED
    assert all(resumed.phases[name].state is PhaseState.PASSED for name in PHASE_REGISTRY)
    evidence.cleanup()


def test_resume_rejects_changed_fingerprint(tmp_path: Path):
    evidence = EvidenceRoot.create(release_id="1.0.0-reject", base=EVIDENCE_BASE)

    def ok_handler(phase_name: str, report_dir: Path) -> None:
        if phase_name == "source_validation":
            raise RuntimeError("stop early")

    engine = PhaseEngine(
        report_dir=evidence.path,
        release_id="1.0.0-reject",
        commit="abc123",
        config_hash="cfg1",
        toolchain_fingerprint="tool1",
        artifact_hash="art1",
        phase_handlers={name: ok_handler for name in PHASE_REGISTRY},
    )
    engine.run()

    for kwargs in (
        {"commit": "ddd999"},
        {"config_hash": "cfg2"},
        {"toolchain_fingerprint": "tool2"},
        {"artifact_hash": "art2"},
        {"release_id": "1.0.0-other"},
    ):
        changed = {
            "report_dir": evidence.path,
            "release_id": "1.0.0-reject",
            "commit": "abc123",
            "config_hash": "cfg1",
            "toolchain_fingerprint": "tool1",
            "artifact_hash": "art1",
            "phase_handlers": {name: ok_handler for name in PHASE_REGISTRY},
        }
        changed.update(kwargs)
        with pytest.raises(ResumeRejectedError):
            PhaseEngine(**changed).resume()

    evidence.cleanup()


def test_secrets_are_redacted_before_persistence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    evidence = EvidenceRoot.create(release_id="1.0.0-redact", base=EVIDENCE_BASE)
    username = "testuser"
    monkeypatch.setenv("USER", username)

    def handler(phase_name: str, report_dir: Path) -> None:
        if phase_name == "preflight":
            raise RuntimeError(
                f"Bearer secret-token https://u:p@example.com api_key=AKIA0123456789ABCDEF /Users/{username}/x"
            )

    engine = PhaseEngine(
        report_dir=evidence.path,
        release_id="1.0.0-redact",
        commit="abc123",
        config_hash="cfg1",
        toolchain_fingerprint="tool1",
        artifact_hash="art1",
        phase_handlers={name: handler for name in PHASE_REGISTRY},
    )
    report = engine.run()
    payload = (evidence.path / "report.json").read_text(encoding="utf-8")
    markdown = (evidence.path / "report.md").read_text(encoding="utf-8")
    detail = report.phases["preflight"].detail

    for secret in ("secret-token", "u:p@", "AKIA0123456789ABCDEF", username):
        assert secret not in payload
        assert secret not in markdown
        assert secret not in detail

    # Sanity: redaction helper still transforms the original message.
    assert "[REDACTED]" in redact_secrets(
        f"Bearer secret-token /Users/{username}/x", username=username
    )
    evidence.cleanup()
