from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest


class _ProbeService:
    def __init__(
        self,
        *,
        mlx_available: bool = True,
        cuda_available: bool = True,
        package_tts_available: bool = True,
    ):
        self.mlx_available = mlx_available
        self.cuda_available = cuda_available
        self.package_tts_available = package_tts_available
        self.calls: list[str] = []

    def probe_mlx_tts_runtime(self):
        self.calls.append("mlx-tts")
        return {
            "available": self.mlx_available,
            "error": None if self.mlx_available else "missing mlx",
        }

    def probe_cleaner_backend(self, backend: str):
        self.calls.append(f"cleaner-{backend}")
        available = self.mlx_available if backend == "mlx" else self.cuda_available
        return {"available": available, "error": None if available else f"missing {backend}"}

    def probe_package_tts_runtime(self):
        self.calls.append("package-tts")
        return {
            "available": self.package_tts_available,
            "error": None if self.package_tts_available else "missing kokoro",
        }


def _compatible(monkeypatch: pytest.MonkeyPatch, profile_id: str) -> None:
    import scripts.setup_runtime as setup_runtime

    profile = setup_runtime.get_runtime_profile(profile_id)
    monkeypatch.setattr(type(profile), "is_compatible", lambda self: True)


def test_build_plan_uses_fixed_argument_lists(monkeypatch: pytest.MonkeyPatch):
    import scripts.setup_runtime as setup_runtime

    _compatible(monkeypatch, "mlx")
    plan = setup_runtime.build_install_plan("mlx", ("tts-mlx",))

    assert plan.pip_command[:5] == (
        sys.executable,
        "-m",
        "pip",
        "install",
        "-r",
    )
    assert plan.pip_command[5] == str(
        (setup_runtime.REPO_ROOT / "requirements" / "mlx.txt").resolve()
    )
    assert plan.model_command == (
        sys.executable,
        str((setup_runtime.REPO_ROOT / "scripts" / "download_models.py").resolve()),
        "--group",
        "tts-mlx",
        "--json",
    )


def test_tauri_supervised_stages_inherit_existing_process_containment():
    import scripts.setup_runtime as setup_runtime

    assert setup_runtime._stage_process_group_kwargs(
        environ={setup_runtime.TAURI_SUPERVISED_ENV: "1"}
    ) == {}


def test_direct_cli_stages_keep_independent_process_tree_supervision():
    import scripts.setup_runtime as setup_runtime

    kwargs = setup_runtime._stage_process_group_kwargs(environ={})
    if os.name == "posix":
        assert kwargs == {"start_new_session": True}
    elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        assert kwargs == {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        assert kwargs == {}


@pytest.mark.parametrize(
    ("profile_id", "groups"),
    [
        ("mlx; rm -rf /", ()),
        ("mlx", ("tts-mlx; rm -rf /",)),
        ("mlx", ("https://example.test/model",)),
        ("mlx", ("../../requirements.txt",)),
        ("unknown", ()),
    ],
)
def test_plan_rejects_unknown_or_untrusted_values(
    monkeypatch: pytest.MonkeyPatch,
    profile_id: str,
    groups: tuple[str, ...],
):
    import scripts.setup_runtime as setup_runtime

    _compatible(monkeypatch, "mlx")
    with pytest.raises(setup_runtime.SetupValidationError):
        setup_runtime.build_install_plan(profile_id, groups)


def test_incompatible_profile_is_rejected_before_subprocess_or_state_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    import scripts.setup_runtime as setup_runtime

    profile = setup_runtime.get_runtime_profile("mlx")
    monkeypatch.setattr(type(profile), "is_compatible", lambda self: False)
    monkeypatch.setattr(
        type(profile),
        "incompatibility_reason",
        lambda self: "test host is unavailable",
    )
    calls: list[tuple[str, ...]] = []
    state_path = tmp_path / "runtime-setup.json"
    with pytest.raises(setup_runtime.SetupValidationError, match="unavailable"):
        setup_runtime.run_setup(
            profile_id="mlx",
            model_groups=(),
            state_path=state_path,
            command_runner=lambda command, **kwargs: calls.append(tuple(command)),
        )

    assert calls == []
    assert not state_path.exists()


def test_dry_run_is_non_mutating_and_returns_exact_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    import scripts.setup_runtime as setup_runtime

    _compatible(monkeypatch, "mlx")
    calls: list[tuple[str, ...]] = []
    state_path = tmp_path / "runtime-setup.json"

    result = setup_runtime.run_setup(
        profile_id="mlx",
        model_groups=("tts-mlx", "cleaner-mlx"),
        dry_run=True,
        state_path=state_path,
        command_runner=lambda command, **kwargs: calls.append(tuple(command)),
    )

    assert calls == []
    assert not state_path.exists()
    assert result["dry_run"] is True
    assert result["commands"][0][:5] == [sys.executable, "-m", "pip", "install", "-r"]
    assert result["commands"][1][-5:] == [
        "--group",
        "tts-mlx",
        "--group",
        "cleaner-mlx",
        "--json",
    ]


def test_cuda_install_no_longer_requires_resolved_pytorch_risk_acceptance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    import scripts.setup_runtime as setup_runtime

    _compatible(monkeypatch, "cuda")
    plan = setup_runtime.build_install_plan("cuda")

    assert plan.accepted_risks == ()


def test_successful_install_uses_shell_false_and_writes_atomic_restart_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    import scripts.setup_runtime as setup_runtime

    _compatible(monkeypatch, "cuda")
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def runner(command, **kwargs):
        calls.append((tuple(command), kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    state_path = tmp_path / "nested" / "runtime-setup.json"
    result = setup_runtime.run_setup(
        profile_id="cuda",
        model_groups=("tts-cpu-cuda",),
        accept_pytorch_risk=True,
        state_path=state_path,
        command_runner=runner,
    )

    assert result["restart_required"] is True
    assert [call[0] for call in calls] == [
        setup_runtime.build_install_plan(
            "cuda", ("tts-cpu-cuda",), accept_pytorch_risk=True
        ).pip_command,
        setup_runtime.build_install_plan(
            "cuda", ("tts-cpu-cuda",), accept_pytorch_risk=True
        ).model_command,
    ]
    assert all(call[1]["shell"] is False for call in calls)
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload == {
        "accepted_risks": [],
        "installed_at": payload["installed_at"],
        "installation_id": payload["installation_id"],
        "installed_during_backend_instance_id": None,
        "installed_without_backend_instance": True,
        "model_groups": ["tts-cpu-cuda"],
        "profile": "cuda",
        "restart_required": True,
        "schema_version": setup_runtime.STATE_SCHEMA_VERSION,
    }
    assert list(state_path.parent.glob("*.tmp")) == []


@pytest.mark.parametrize(
    ("profile_id", "model_groups", "expected_progress"),
    [
        (
            "base",
            (),
            [
                ("plan", None, 5),
                ("stage_started", "Dependency installation", 10),
                ("stage_complete", "Dependency installation", 50),
                ("stage_started", "Verification", 90),
                ("stage_complete", "Verification", 90),
            ],
        ),
        (
            "mlx",
            ("tts-mlx",),
            [
                ("plan", None, 5),
                ("stage_started", "Dependency installation", 10),
                ("stage_complete", "Dependency installation", 50),
                ("stage_started", "Model download", 55),
                ("stage_complete", "Model download", 80),
                ("stage_started", "Verification", 90),
                ("stage_complete", "Verification", 90),
            ],
        ),
    ],
)
def test_setup_progress_uses_monotonic_milestones_for_base_and_model_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    profile_id: str,
    model_groups: tuple[str, ...],
    expected_progress: list[tuple[str, str | None, int]],
):
    import scripts.setup_runtime as setup_runtime

    _compatible(monkeypatch, profile_id)
    events: list[dict[str, object]] = []

    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    setup_runtime.run_setup(
        profile_id=profile_id,
        model_groups=model_groups,
        state_path=tmp_path / f"{profile_id}.json",
        command_runner=runner,
        emit=events.append,
    )

    assert [
        (event["event"], event.get("stage"), event.get("percent"))
        for event in events
    ] == expected_progress
    percents = [event["percent"] for event in events]
    assert all(isinstance(percent, int) and 0 <= percent <= 100 for percent in percents)
    assert percents == sorted(percents)


def test_failed_install_does_not_replace_existing_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    import scripts.setup_runtime as setup_runtime

    _compatible(monkeypatch, "mlx")
    state_path = tmp_path / "runtime-setup.json"
    state_path.write_text('{"profile":"base"}', encoding="utf-8")

    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="install failed")

    with pytest.raises(setup_runtime.SetupExecutionError, match="Dependency installation failed"):
        setup_runtime.run_setup(
            profile_id="mlx",
            model_groups=(),
            state_path=state_path,
            command_runner=runner,
        )
    assert state_path.read_text(encoding="utf-8") == '{"profile":"base"}'


def test_status_runs_fresh_probe_and_does_not_trust_installed_state(
    tmp_path: Path,
):
    import scripts.setup_runtime as setup_runtime

    state_path = tmp_path / "runtime-setup.json"
    setup_runtime.write_setup_state(
        state_path,
        profile_id="mlx",
        model_groups=("tts-mlx",),
        accepted_risks=(),
    )
    probe = _ProbeService(mlx_available=False)

    status = setup_runtime.get_setup_status(state_path=state_path, probe_service=probe)

    assert status["configured"] is True
    assert status["profile"] == "mlx"
    assert status["ready"] is False
    assert status["restart_required"] is True
    assert status["probe"]["available"] is False
    assert probe.calls == ["mlx-tts"]


def test_setup_state_cache_key_tracks_identity_profile_and_model_groups(tmp_path: Path):
    import scripts.setup_runtime as setup_runtime

    state_path = tmp_path / "runtime-setup.json"
    setup_runtime.write_setup_state(
        state_path,
        profile_id="mlx",
        model_groups=("tts-mlx",),
        accepted_risks=(),
    )

    first = setup_runtime.get_setup_state_cache_key(state_path=state_path)
    setup_runtime.write_setup_state(
        state_path,
        profile_id="mlx",
        model_groups=("cleaner-mlx",),
        accepted_risks=(),
    )
    second = setup_runtime.get_setup_state_cache_key(state_path=state_path)

    assert first != second
    assert "mlx" in first
    assert ("tts-mlx",) in first
    assert ("cleaner-mlx",) in second


@pytest.mark.parametrize(
    ("profile", "groups", "expected_calls", "available"),
    [
        ("mlx", ("tts-mlx",), ["mlx-tts"], True),
        ("mlx", ("cleaner-mlx",), ["cleaner-mlx"], True),
        ("mlx", (), [], True),
        ("cuda", ("tts-cpu-cuda",), ["package-tts"], False),
        ("cuda", ("cleaner-cuda",), ["cleaner-cuda"], True),
    ],
)
def test_status_probes_only_selected_model_groups(
    tmp_path: Path,
    profile: str,
    groups: tuple[str, ...],
    expected_calls: list[str],
    available: bool,
):
    import scripts.setup_runtime as setup_runtime

    state_path = tmp_path / f"{profile}.json"
    setup_runtime.write_setup_state(
        state_path,
        profile_id=profile,
        model_groups=groups,
        accepted_risks=(),
    )
    probe = _ProbeService(package_tts_available=False)

    status = setup_runtime.get_setup_status(state_path=state_path, probe_service=probe)

    assert probe.calls == expected_calls
    assert status["ready"] is available


def test_base_status_is_ready_without_optional_runtime_probe(tmp_path: Path):
    import scripts.setup_runtime as setup_runtime

    state_path = tmp_path / "runtime-setup.json"
    setup_runtime.write_setup_state(
        state_path,
        profile_id="base",
        model_groups=(),
        accepted_risks=(),
    )
    probe = _ProbeService()

    status = setup_runtime.get_setup_status(state_path=state_path, probe_service=probe)

    assert status["ready"] is True
    assert status["probe"] == {"available": True, "checks": []}
    assert probe.calls == []


def test_same_backend_process_cannot_clear_restart_required(
    tmp_path: Path,
):
    import scripts.setup_runtime as setup_runtime

    state_path = tmp_path / "runtime-setup.json"
    backend_id = "1" * 32
    setup_runtime.persist_backend_instance(backend_id, state_path=state_path)
    setup_runtime.write_setup_state(
        state_path,
        profile_id="base",
        model_groups=(),
        accepted_risks=(),
        backend_instance_id=backend_id,
    )
    acknowledged = setup_runtime.acknowledge_backend_startup(backend_id, state_path=state_path)

    status = setup_runtime.get_setup_status(
        state_path=state_path,
        probe_service=_ProbeService(),
    )
    assert acknowledged is False
    assert status["restart_required"] is True


def test_new_backend_process_clears_restart_required(
    tmp_path: Path,
):
    import scripts.setup_runtime as setup_runtime

    state_path = tmp_path / "runtime-setup.json"
    old_backend_id = "1" * 32
    new_backend_id = "2" * 32
    setup_runtime.persist_backend_instance(old_backend_id, state_path=state_path)
    setup_runtime.write_setup_state(
        state_path,
        profile_id="base",
        model_groups=(),
        accepted_risks=(),
        backend_instance_id=old_backend_id,
    )
    setup_runtime.persist_backend_instance(new_backend_id, state_path=state_path)
    acknowledged = setup_runtime.acknowledge_backend_startup(new_backend_id, state_path=state_path)

    assert acknowledged is True
    assert (
        setup_runtime.get_setup_status(
            state_path=state_path,
            probe_service=_ProbeService(),
        )["restart_required"]
        is False
    )


def test_missing_install_time_backend_allows_first_real_backend(tmp_path: Path):
    import scripts.setup_runtime as setup_runtime

    state_path = tmp_path / "runtime-setup.json"
    setup_runtime.write_setup_state(
        state_path,
        profile_id="base",
        model_groups=(),
        accepted_risks=(),
    )
    backend_id = "3" * 32
    setup_runtime.persist_backend_instance(backend_id, state_path=state_path)
    assert setup_runtime.acknowledge_backend_startup(backend_id, state_path=state_path) is True
    assert setup_runtime.read_setup_state(state_path)["restart_required"] is False


def test_stale_acknowledgement_never_overwrites_new_install(tmp_path: Path):
    import scripts.setup_runtime as setup_runtime

    state_path = tmp_path / "runtime-setup.json"
    old_state = setup_runtime.write_setup_state(
        state_path,
        profile_id="base",
        model_groups=(),
        accepted_risks=(),
    )
    new_state = setup_runtime.write_setup_state(
        state_path,
        profile_id="base",
        model_groups=(),
        accepted_risks=(),
    )

    assert (
        setup_runtime._compare_and_swap_restart_ack(
            state_path,
            expected_installation_id=old_state["installation_id"],
        )
        is False
    )
    current = setup_runtime.read_setup_state(state_path)
    assert current["installation_id"] == new_state["installation_id"]
    assert current["restart_required"] is True


def test_pid_is_running_reports_own_process_alive_and_exited_child_dead():
    import scripts.setup_runtime as setup_runtime

    # Guards the lock-owner liveness probe. On Windows the naive
    # os.kill(pid, 0) probe broadcast a console Ctrl+C (signal 0 is
    # CTRL_C_EVENT) and misreported live owners as dead when the
    # broadcast failed, letting a concurrent setup steal a held lock.
    assert setup_runtime._pid_is_running(os.getpid()) is True
    assert setup_runtime._pid_is_running(-1) is False
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert setup_runtime._pid_is_running(child.pid) is False


def test_concurrent_setup_is_excluded_before_second_pip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    import scripts.setup_runtime as setup_runtime

    _compatible(monkeypatch, "mlx")
    state_path = tmp_path / "runtime-setup.json"
    entered = threading.Event()
    release = threading.Event()
    first_calls: list[list[str]] = []
    second_calls: list[list[str]] = []

    def first_runner(command, **kwargs):
        first_calls.append(command)
        entered.set()
        assert release.wait(timeout=5)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    thread = threading.Thread(
        target=lambda: setup_runtime.run_setup(
            profile_id="mlx",
            state_path=state_path,
            command_runner=first_runner,
        )
    )
    thread.start()
    assert entered.wait(timeout=5)
    with pytest.raises(setup_runtime.SetupExecutionError, match="already running"):
        setup_runtime.run_setup(
            profile_id="mlx",
            state_path=state_path,
            command_runner=lambda command, **kwargs: second_calls.append(command),
            lock_timeout_seconds=0.05,
        )
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(first_calls) == 1
    assert second_calls == []


def test_non_finite_lock_timeout_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import scripts.setup_runtime as setup_runtime

    _compatible(monkeypatch, "mlx")
    with pytest.raises(setup_runtime.SetupValidationError, match="finite"):
        setup_runtime.run_setup(
            profile_id="mlx",
            state_path=tmp_path / "runtime-setup.json",
            lock_timeout_seconds=float("nan"),
        )


@pytest.mark.parametrize("owner_contents", [None, "{corrupt"])
def test_old_ownerless_or_corrupt_lock_is_recovered(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    owner_contents: str | None,
):
    import scripts.setup_runtime as setup_runtime

    state_path = tmp_path / "runtime-setup.json"
    lock_path = state_path.with_name(f"{state_path.name}.lock")
    lock_path.mkdir()
    if owner_contents is not None:
        (lock_path / "owner.json").write_text(owner_contents, encoding="utf-8")
    old = 1_000_000_000
    for path in (lock_path, *lock_path.iterdir()):
        path.touch()
        os.utime(path, (old, old))
    monkeypatch.setattr(setup_runtime.time, "time", lambda: old + 1000)

    with setup_runtime.SetupLock(state_path, timeout_seconds=0.1):
        assert lock_path.is_dir()
    assert not lock_path.exists()


def test_recent_ambiguous_lock_fails_closed(tmp_path: Path):
    import scripts.setup_runtime as setup_runtime

    state_path = tmp_path / "runtime-setup.json"
    lock_path = state_path.with_name(f"{state_path.name}.lock")
    lock_path.mkdir()

    with pytest.raises(setup_runtime.SetupExecutionError, match="already running"):
        with setup_runtime.SetupLock(state_path, timeout_seconds=0.01):
            pass
    assert lock_path.exists()


def test_recent_lock_owned_by_dead_process_is_recovered(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    import scripts.setup_runtime as setup_runtime

    state_path = tmp_path / "runtime-setup.json"
    lock_path = state_path.with_name(f"{state_path.name}.lock")
    lock_path.mkdir()
    (lock_path / "owner.json").write_text(
        json.dumps({"pid": 424242, "created_at": time.time(), "token": "dead"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(setup_runtime, "_pid_is_running", lambda pid: False)

    with setup_runtime.SetupLock(state_path, timeout_seconds=0.1):
        assert lock_path.is_dir()
    assert not lock_path.exists()


def test_corrupt_backend_instance_blocks_setup_before_pip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    import scripts.setup_runtime as setup_runtime

    _compatible(monkeypatch, "mlx")
    state_path = tmp_path / "runtime-setup.json"
    setup_runtime._backend_instance_path(state_path).write_text("{bad", encoding="utf-8")
    calls = []
    with pytest.raises(setup_runtime.SetupExecutionError, match="corrupt"):
        setup_runtime.run_setup(
            profile_id="mlx",
            state_path=state_path,
            command_runner=lambda command, **kwargs: calls.append(command),
        )
    assert calls == []


def test_manual_restart_acknowledgement_cli_is_not_exposed(
    capsys: pytest.CaptureFixture[str],
):
    import scripts.setup_runtime as setup_runtime

    exit_code = setup_runtime.main(["--acknowledge-restart", "--json"])

    event = json.loads(capsys.readouterr().err)
    assert exit_code == 2
    assert event["event"] == "error"
    assert event["terminal"] is True


def test_desktop_backend_lifespan_attempts_restart_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    from fastapi.testclient import TestClient
    from modules.web import server
    import scripts.setup_runtime as setup_runtime

    state_path = tmp_path / "runtime-setup.json"
    monkeypatch.setenv("AUDIOBOOK_DATA_DIR", str(tmp_path / "data"))
    setup_runtime.write_setup_state(
        state_path,
        profile_id="base",
        model_groups=(),
        accepted_risks=(),
        backend_instance_id="1" * 32,
    )

    with TestClient(server.create_app(controller=object(), desktop_mode=True)):
        pass

    assert setup_runtime.read_setup_state(state_path)["restart_required"] is False


def test_legacy_cuda_risk_acknowledgement_is_migrated_away(tmp_path: Path):
    import scripts.setup_runtime as setup_runtime

    state_path = tmp_path / "runtime-setup.json"
    setup_runtime.write_setup_state(
        state_path,
        profile_id="cuda",
        model_groups=(),
        accepted_risks=(),
    )
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["accepted_risks"] = [
        "PYSEC-2026-139/CVE-2026-4538,CVE-2025-3000"
    ]
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    status = setup_runtime.get_setup_status(
        state_path=state_path,
        probe_service=_ProbeService(),
    )

    assert status["configured"] is True
    assert status["risk"] is None
    assert setup_runtime.read_setup_state(state_path)["accepted_risks"] == []


def test_json_cli_errors_are_single_parseable_terminal_events(
    capsys: pytest.CaptureFixture[str],
):
    import scripts.setup_runtime as setup_runtime

    exit_code = setup_runtime.main(["--profile", "nope", "--json"])

    captured = capsys.readouterr()
    assert captured.out == ""
    event = json.loads(captured.err)
    assert event["event"] == "error"
    assert event["terminal"] is True
    assert event["error"] == "validation_error"
    assert "Unknown runtime profile" in event["message"]
    assert exit_code == 2


def test_json_cli_parse_errors_are_machine_readable(
    capsys: pytest.CaptureFixture[str],
):
    import scripts.setup_runtime as setup_runtime

    exit_code = setup_runtime.main(["--not-allowlisted", "value", "--json"])

    event = json.loads(capsys.readouterr().err)
    assert event["event"] == "error"
    assert event["terminal"] is True
    assert "unrecognized arguments" in event["message"]
    assert exit_code == 2


def test_subprocess_oserror_is_wrapped_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
):
    import scripts.setup_runtime as setup_runtime

    _compatible(monkeypatch, "mlx")

    def unavailable(*args, **kwargs):
        raise OSError("spawn unavailable")

    with pytest.raises(setup_runtime.SetupExecutionError, match="could not start"):
        setup_runtime.run_setup(
            profile_id="mlx",
            command_runner=unavailable,
        )


def test_probe_exception_is_wrapped_as_setup_execution_error(tmp_path: Path):
    import scripts.setup_runtime as setup_runtime

    class BrokenProbe(_ProbeService):
        def probe_mlx_tts_runtime(self):
            raise OSError("probe unavailable")

    state_path = tmp_path / "runtime-setup.json"
    setup_runtime.write_setup_state(
        state_path,
        profile_id="mlx",
        model_groups=("tts-mlx",),
        accepted_risks=(),
    )

    with pytest.raises(setup_runtime.SetupExecutionError, match="Runtime probe failed"):
        setup_runtime.get_setup_status(
            state_path=state_path,
            probe_service=BrokenProbe(),
        )


def test_package_tts_probe_uses_bounded_runtime_probe(monkeypatch: pytest.MonkeyPatch):
    from modules.app.services.runtime_capability_service import RuntimeCapabilityService

    service = RuntimeCapabilityService(cache_ttl_seconds=60)
    calls: list[str] = []

    def run_probe(script: str, timeout: float):
        calls.append(script)
        return {"available": True, "error": None}

    monkeypatch.setattr(service, "_run_probe", run_probe)

    assert service.probe_package_tts_runtime()["available"] is True
    assert service.probe_package_tts_runtime()["cache_hit"] is True
    assert calls == [service._TTS_PACKAGE_PROBE]


def test_state_io_errors_are_wrapped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import scripts.setup_runtime as setup_runtime

    state_path = tmp_path / "runtime-setup.json"
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda self, **kwargs: (_ for _ in ()).throw(OSError("read denied")),
    )
    with pytest.raises(setup_runtime.SetupExecutionError, match="Could not read"):
        setup_runtime.get_setup_status(state_path=state_path)


def test_state_write_errors_are_wrapped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import scripts.setup_runtime as setup_runtime

    monkeypatch.setattr(
        setup_runtime,
        "_atomic_write_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("write denied")),
    )

    with pytest.raises(setup_runtime.SetupExecutionError, match="Could not write"):
        setup_runtime.write_setup_state(
            tmp_path / "runtime-setup.json",
            profile_id="base",
            model_groups=(),
            accepted_risks=(),
        )


def test_json_execution_failure_is_one_terminal_error_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    import scripts.setup_runtime as setup_runtime

    monkeypatch.setattr(
        setup_runtime,
        "get_setup_status",
        lambda: (_ for _ in ()).throw(setup_runtime.SetupExecutionError("state unavailable")),
    )

    exit_code = setup_runtime.main(["--status", "--json"])

    captured = capsys.readouterr()
    events = [json.loads(line) for line in captured.err.splitlines()]
    assert exit_code == 1
    assert captured.out == ""
    assert len(events) == 1
    assert events[0]["event"] == "error"
    assert events[0]["terminal"] is True
    assert "Traceback" not in captured.err


def test_supervised_timeout_terminates_process(monkeypatch: pytest.MonkeyPatch):
    import scripts.setup_runtime as setup_runtime

    real_popen = subprocess.Popen
    processes = []

    def recording_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(setup_runtime.subprocess, "Popen", recording_popen)
    with pytest.raises(setup_runtime.SetupExecutionError, match="timed out"):
        setup_runtime._supervise_command(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stage="test stage",
            timeout_seconds=0.05,
        )
    assert processes and processes[0].poll() is not None


def test_windows_timeout_uses_bounded_taskkill_tree_command(monkeypatch: pytest.MonkeyPatch):
    import scripts.setup_runtime as setup_runtime

    class FakeProcess:
        pid = 4321

        def poll(self):
            return None

        def wait(self, timeout):
            return 0

        def terminate(self):
            raise AssertionError("taskkill should run first")

        def kill(self):
            raise AssertionError("taskkill succeeded")

    calls = []
    monkeypatch.setattr(setup_runtime.os, "name", "nt")
    monkeypatch.setattr(
        setup_runtime.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    setup_runtime._terminate_process_tree(FakeProcess())

    assert calls == [
        (
            ["taskkill", "/PID", "4321", "/T", "/F"],
            {"shell": False, "check": False, "capture_output": True, "timeout": 5},
        )
    ]


def test_diagnostics_are_bounded_and_redacted():
    import scripts.setup_runtime as setup_runtime

    diagnostic = (
        "x" * 5000
        + "\x00 token=super-secret sk-abcdefghijk "
        + str(Path.home() / "private" / "file.txt")
    )
    sanitized = setup_runtime._sanitize_diagnostic(diagnostic)

    assert len(sanitized) <= setup_runtime.DIAGNOSTIC_MAX_CHARS + 1
    assert "super-secret" not in sanitized
    assert "sk-abcdefghijk" not in sanitized
    assert str(Path.home()) not in sanitized
    assert "\x00" not in sanitized


def test_nonzero_subprocess_error_uses_bounded_redacted_stderr():
    import scripts.setup_runtime as setup_runtime

    code = "import sys; sys.stderr.write('z'*10000 + ' token=secret-value sk-abcdefghijk'); raise SystemExit(3)"
    with pytest.raises(setup_runtime.SetupExecutionError) as raised:
        setup_runtime._supervise_command(
            [sys.executable, "-c", code],
            stage="test stage",
            timeout_seconds=5,
        )
    message = str(raised.value)
    assert len(message) < setup_runtime.DIAGNOSTIC_MAX_CHARS + 200
    assert "secret-value" not in message
    assert "sk-abcdefghijk" not in message


def test_progress_json_dry_run_emits_plan_and_success_events(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    import scripts.setup_runtime as setup_runtime

    _compatible(monkeypatch, "mlx")
    exit_code = setup_runtime.main(
        ["--profile", "mlx", "--models", "tts-mlx", "--dry-run", "--progress-json"]
    )

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event["event"] for event in events] == ["plan", "complete"]
    assert [event["percent"] for event in events] == [5, 100]
    assert events[-1]["terminal"] is True
    assert exit_code == 0


def test_state_path_uses_desktop_data_parent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import scripts.setup_runtime as setup_runtime

    monkeypatch.setenv("AUDIOBOOK_DATA_DIR", str(tmp_path / "data"))
    assert setup_runtime.default_state_path() == tmp_path / "runtime-setup.json"


def test_write_setup_state_defaults_to_restart_required(tmp_path: Path):
    import scripts.setup_runtime as setup_runtime

    state_path = tmp_path / "runtime-setup.json"
    payload = setup_runtime.write_setup_state(
        state_path,
        profile_id="base",
        model_groups=(),
        accepted_risks=(),
    )
    assert payload["restart_required"] is True
    assert setup_runtime.read_setup_state(state_path)["restart_required"] is True


def test_write_setup_state_can_persist_without_restart_for_adopt(tmp_path: Path):
    import scripts.setup_runtime as setup_runtime

    state_path = tmp_path / "runtime-setup.json"
    payload = setup_runtime.write_setup_state(
        state_path,
        profile_id="base",
        model_groups=(),
        accepted_risks=(),
        restart_required=False,
    )
    assert payload["restart_required"] is False
    assert setup_runtime.read_setup_state(state_path)["restart_required"] is False
