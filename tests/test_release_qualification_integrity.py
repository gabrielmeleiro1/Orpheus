from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from scripts import release_qualification as cli
from scripts import desktop_runtime_smoke
from tools.release_qualification import runtime
from tools.release_qualification.evidence import SENTINEL_NAME
from tools.release_qualification.fixtures import RuntimePaths, _preserve_pristine_backup
from tools.release_qualification.model import PhaseState
from modules.storage.sqlite_repo import SQLiteRepository


def test_pristine_backup_preserves_committed_wal_rows_while_source_repository_is_open(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = RuntimePaths(
        root=source_root,
        data_dir=source_root / "data",
        output_dir=source_root / "output",
        temp_dir=source_root / "temp",
        db_path=source_root / "data/library.db",
    )
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / SENTINEL_NAME).write_text("test\n")
    repo = SQLiteRepository(source.db_path, output_dir=source.output_dir, temp_dir=source.temp_dir)
    try:
        with repo._connection() as connection:
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("CREATE TABLE committed_values (value TEXT)")
            connection.execute("INSERT INTO committed_values VALUES ('kept')")
        backup = _preserve_pristine_backup(
            source,
            backups,
            expected_qualification_root=tmp_path,
            release_id="test",
        )
        with sqlite3.connect(backup) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert "committed_values" in tables, "backup lost a table committed in the WAL"
            assert connection.execute("SELECT value FROM committed_values").fetchall() == [
                ("kept",)
            ]
        assert backup.stem == hashlib.sha256(backup.read_bytes()).hexdigest()
    finally:
        repo.close()


@pytest.fixture
def bundle_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    report = tmp_path / "report"
    (report / "config").mkdir(parents=True)
    (report / "config/release_qualification.toml").write_bytes(cli.DEFAULT_CONFIG.read_bytes())
    (report / "report.json").write_text(
        json.dumps({"release_id": "1.0.0", "release_version": "1.0.0"})
    )
    app = tmp_path / "Orpheus.app"
    app.mkdir()
    (app / "executable").write_bytes(b"qualified application")
    dmg = tmp_path / "Orpheus.dmg"
    dmg.write_bytes(b"qualified image")
    monkeypatch.setattr(cli, "_locate_app_bundle", lambda: app)
    monkeypatch.setattr(cli, "_locate_dmg", lambda _: dmg)
    monkeypatch.setattr(cli, "inspect_app_bundle", lambda *a, **k: None)
    monkeypatch.setattr(cli, "inspect_dmg", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_resolve_source_dmg", lambda _: dmg)
    return report, app, dmg


def _bundle_runner(report, app, dmg):
    def run(spec, **kwargs):
        log = report / "commands" / f"{spec.label}.log"
        log.parent.mkdir(exist_ok=True)
        log.write_text(
            json.dumps(
                {
                    "app": str(app),
                    "dmg": str(dmg),
                    "dmg_sha256": hashlib.sha256(dmg.read_bytes()).hexdigest(),
                    "app_tree_sha256": runtime.sidecar_tree_digest(app),
                }
            )
            + "\n"
        )
        return SimpleNamespace(exit_code=0, label=spec.label, log_path=str(log))

    return run


def test_bundle_validation_rejects_dmg_replacement_after_bundled_smoke(
    bundle_workspace, monkeypatch
):
    report, app, dmg = bundle_workspace
    runner = _bundle_runner(report, app, dmg)

    def replace_after_smoke(spec, **kwargs):
        result = runner(spec, **kwargs)
        if spec.label == "bundled-runtime-smoke":
            dmg.write_bytes(b"replaced after smoke")
        return result

    outcome = cli.build_phase_handlers(report, run_command_fn=replace_after_smoke)[
        "bundle_validation"
    ]("bundle_validation", report)
    assert outcome.state is PhaseState.FAILED, "replacement DMG was certified"
    assert "DMG changed" in outcome.detail
    assert not (report / "artifacts.json").exists()


def test_observation_rejects_app_bundle_changed_since_qualification(
    bundle_workspace, monkeypatch, capsys
):
    report, app, dmg = bundle_workspace
    outcome = cli.build_phase_handlers(report, run_command_fn=_bundle_runner(report, app, dmg))[
        "bundle_validation"
    ]("bundle_validation", report)
    assert outcome.state is PhaseState.PASSED
    (app / "executable").write_bytes(b"changed application")
    monkeypatch.setattr(cli, "_assert_report_dir", lambda _: None)
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    sessions = []

    class Session:
        def __init__(self, **kwargs):
            sessions.append(kwargs)

        def ready_marker(self):
            return "ready"

        def run(self, **kwargs):
            return SimpleNamespace(passed=True, detail="ok", metrics={})

    monkeypatch.setattr(cli, "ObserverSession", Session)
    args = argparse.Namespace(
        report_dir=str(report),
        app_bundle=str(app),
        runtime_root=None,
        scenario="startup",
        seconds=1,
        minutes=None,
        action_started_at=None,
        ui_ready_at=None,
    )
    assert cli.cmd_observe(args) == cli.EXIT_FAIL, "changed app bundle was observed as qualified"
    assert not sessions
    assert "app bundle changed" in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="POSIX process sessions")
def test_bundled_smoke_timeout_terminates_backend(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    stopped = tmp_path / "stopped"
    pid_file = tmp_path / "backend.pid"
    backend = tmp_path / "backend.py"
    backend.write_text(
        "import os, signal, time\nfrom pathlib import Path\n"
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        f'def stop(*args):\n    Path({str(stopped)!r}).write_text("stopped")\n    raise SystemExit\n'
        "signal.signal(signal.SIGTERM, stop)\nwhile True: time.sleep(0.05)\n"
    )
    (scripts / "desktop_runtime_smoke.py").write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(backend)!r}], start_new_session=False)\n"
        "time.sleep(30)\n"
    )
    app = tmp_path / "Orpheus.app"
    sidecar = app / runtime.SIDECAR_RELATIVE_PATH
    sidecar.mkdir(parents=True)
    (sidecar / "orpheus-desktop-backend").write_bytes(b"backend")
    dmg = tmp_path / "image.dmg"
    dmg.write_bytes(b"image")
    runner = getattr(runtime, "run_bundled_smoke", subprocess.run)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            runtime.qualify_bundled_runtime(
                app=app,
                dmg=dmg,
                repo_root=tmp_path,
                run=lambda argv, **kwargs: runner(argv, **dict(kwargs, timeout=2)),
                mount_dmg=lambda *a: tmp_path,
                detach_dmg=lambda *a: None,
            )
        deadline = time.monotonic() + 2
        while not stopped.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert stopped.exists(), "backend survived the bundled smoke timeout"
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_resume_rejects_manifest_without_app_fingerprint(bundle_workspace):
    report, app, dmg = bundle_workspace
    (report / "artifacts.json").write_text(
        json.dumps(
            {
                "app": app.name,
                "dmg": dmg.name,
                "dmg_sha256": cli.sha256_file(dmg),
            }
        )
    )
    with pytest.raises(cli.ResumeRejectedError, match="missing app bundle fingerprint"):
        cli._current_artifact_hash(report)


def test_bundle_validation_accepts_repo_relative_smoke_paths(bundle_workspace):
    report, app, dmg = bundle_workspace
    runner = _bundle_runner(report, app, dmg)

    def relative_evidence(spec, **kwargs):
        result = runner(spec, **kwargs)
        payload = json.loads(Path(result.log_path).read_text())
        payload["app"], payload["dmg"] = app.name, dmg.name
        with open(result.log_path, "w") as log:
            json.dump(payload, log)
        return result

    outcome = cli.build_phase_handlers(report, run_command_fn=relative_evidence)[
        "bundle_validation"
    ]("bundle_validation", report)
    assert outcome.state is PhaseState.PASSED


def test_app_fingerprint_ignores_timestamps_and_tracks_file_contents(tmp_path):
    app = tmp_path / "Orpheus.app"
    app.mkdir()
    executable = app / "executable"
    executable.write_bytes(b"original")
    original = runtime.app_tree_digest(app)
    os.utime(executable, (100, 100))
    assert runtime.app_tree_digest(app) == original
    executable.write_bytes(b"changed")
    assert runtime.app_tree_digest(app) != original


@pytest.fixture
def mounted_bundle_workspace(tmp_path):
    app = tmp_path / "built/Orpheus.app"
    sidecar = app / runtime.SIDECAR_RELATIVE_PATH
    sidecar.mkdir(parents=True)
    (sidecar / "orpheus-desktop-backend").write_bytes(b"backend")
    frontend = Path("Contents/MacOS/main")
    (app / frontend).parent.mkdir()
    (app / frontend).write_bytes(b"qualified frontend")
    mounted = tmp_path / "mounted"
    shipped_app = mounted / app.name
    shutil.copytree(app, shipped_app)
    dmg = tmp_path / "image.dmg"
    dmg.write_bytes(b"image")
    return app, dmg, mounted, shipped_app, frontend


def test_bundled_smoke_rejects_a_dmg_whose_app_differs_from_the_qualified_app(
    tmp_path, mounted_bundle_workspace
):
    app, dmg, mounted, shipped_app, frontend = mounted_bundle_workspace
    (shipped_app / frontend).write_bytes(b"different frontend")
    smoke = MagicMock()
    detach = MagicMock()

    with pytest.raises(runtime.ArtifactInspectionError, match="app bundle.*differ"):
        runtime.qualify_bundled_runtime(
            app=app, dmg=dmg, repo_root=tmp_path, run=smoke,
            mount_dmg=lambda *_: mounted, detach_dmg=detach,
        )

    smoke.assert_not_called()
    detach.assert_called_once_with(mounted)


def test_bundled_smoke_rejects_a_mounted_app_changed_during_smoke(
    tmp_path, mounted_bundle_workspace
):
    app, dmg, mounted, shipped_app, frontend = mounted_bundle_workspace
    original_digest = runtime.app_tree_digest(app)
    smoke = MagicMock(side_effect=lambda *a, **k: (shipped_app / frontend).write_bytes(b"changed"))
    detach = MagicMock()

    with pytest.raises(runtime.ArtifactInspectionError, match="changed during qualification"):
        runtime.qualify_bundled_runtime(
            app=app, dmg=dmg, repo_root=tmp_path, run=smoke,
            mount_dmg=lambda *_: mounted, detach_dmg=detach,
        )

    smoke.assert_called_once()
    detach.assert_called_once_with(mounted)
    assert runtime.app_tree_digest(app) == original_digest


@pytest.fixture
def windows_smoke_launcher(monkeypatch):
    monkeypatch.setattr(runtime, "os", SimpleNamespace(**dict(vars(os), name="nt")))
    process = MagicMock()
    process.wait.return_value = 0
    spawn = MagicMock(return_value=process)
    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    own = MagicMock()
    resume = MagicMock()
    cleanup = MagicMock()
    monkeypatch.setattr(desktop_runtime_smoke, "_own_windows_process_tree", own)
    monkeypatch.setattr(desktop_runtime_smoke, "_resume_windows_process", resume)
    monkeypatch.setattr(desktop_runtime_smoke, "stop_backend", cleanup)
    calls = MagicMock()
    for name, mock in (
        ("spawn", spawn), ("own", own), ("resume", resume),
        ("kill", process.kill), ("wait", process.wait), ("cleanup", cleanup),
    ):
        calls.attach_mock(mock, name)
    return process, spawn, own, resume, cleanup, calls


def test_bundled_smoke_launcher_suspends_until_job_assignment_and_resume(
    tmp_path, windows_smoke_launcher
):
    process, spawn, own, resume, cleanup, calls = windows_smoke_launcher
    result = runtime.run_bundled_smoke(["smoke"], cwd=tmp_path, timeout=17)

    assert spawn.call_args.kwargs["creationflags"] & 0x4, "smoke driver was not created suspended"
    assert spawn.call_args.kwargs["start_new_session"] is False
    assert spawn.call_args.kwargs["env"]["ORPHEUS_SMOKE_PROCESS_GROUP"] == "1"
    assert [call[0] for call in calls.mock_calls] == ["spawn", "own", "resume", "wait", "cleanup"]
    own.assert_called_once_with(process)
    resume.assert_called_once_with(process)
    process.wait.assert_called_once_with(timeout=17)
    cleanup.assert_called_once_with(process)
    assert result.returncode == 0


@pytest.mark.parametrize("failure", ["assignment", "resume"])
def test_bundled_smoke_launcher_kills_and_cleans_up_when_containment_fails(
    tmp_path, windows_smoke_launcher, failure
):
    process, spawn, own, resume, cleanup, calls = windows_smoke_launcher
    error = OSError(f"cannot {failure}")
    (own if failure == "assignment" else resume).side_effect = error

    with pytest.raises(OSError) as raised:
        runtime.run_bundled_smoke(["smoke"], cwd=tmp_path)

    assert raised.value is error
    expected = ["spawn", "own"] + (["resume"] if failure == "resume" else [])
    assert [call[0] for call in calls.mock_calls] == expected + ["kill", "wait", "cleanup"]
    own.assert_called_once_with(process)
    if failure == "assignment":
        resume.assert_not_called()
    else:
        resume.assert_called_once_with(process)
    process.kill.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=5)
    cleanup.assert_called_once_with(process)
