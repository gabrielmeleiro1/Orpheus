"""Tests for qualification process sampling and performance budget evaluation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.release_qualification.macos_processes import (
    ProcessIdentity,
    ProcessSample,
    attribute_orpheus_tree,
    parse_ps_output,
    terminate_pid_if_safe,
)
from tools.release_qualification.metrics import (
    CycleResult,
    ObserverSession,
    detect_cycle_survivors,
    evaluate_idle_cpu,
    evaluate_launch,
    evaluate_shutdown,
    evaluate_soak_memory,
    summarize_launch_cycles,
    write_jsonl_samples,
)
from tools.release_qualification.model import PerformanceBudgets

# Numeric columns first; args is the final catch-all so spaced helper paths stay intact.
PS_FIXTURE = """\
 1001  1000  12.0  204800 /Applications/Orpheus.app/Contents/MacOS/Orpheus
 1002  1001   3.5   51200 /Applications/Orpheus.app/Contents/Frameworks/Orpheus Helper (Renderer).app/Contents/MacOS/Orpheus Helper
 1003  1001   1.2   40960 /Applications/Orpheus.app/Contents/Resources/binaries/orpheus-desktop-backend/orpheus-desktop-backend
 1004  1003   0.8   10240 /Applications/Orpheus.app/Contents/Resources/binaries/orpheus-desktop-backend/setup-helper
 2001     1   0.1    4096 /Applications/Unrelated Orpheus Notes.app/Contents/MacOS/Orpheus Notes
 2002     1   5.0   81920 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome
 2003  2002   2.0   20480 /tmp/fake-orpheus-imposter
 2004     1   0.2    2048 /Volumes/My Disk/Orpheus.app/Contents/MacOS/Orpheus
"""


def _budgets(**overrides: float | int) -> PerformanceBudgets:
    values: dict[str, float | int] = {
        "cold_launch_seconds": 15.0,
        "warm_launch_seconds": 5.0,
        "window_close_seconds": 1.0,
        "process_exit_seconds": 12.0,
        "idle_cpu_median_percent": 3.0,
        "idle_cpu_p95_percent": 8.0,
        "soak_memory_growth_mib": 100,
        "soak_memory_growth_percent": 10.0,
        "library_long_task_ms": 250,
        "candidate_launch_cycles": 25,
        "final_launch_cycles": 100,
    }
    values.update(overrides)
    return PerformanceBudgets(**values)  # type: ignore[arg-type]


def test_parse_ps_output_preserves_spaced_executable_paths() -> None:
    samples = parse_ps_output(PS_FIXTURE)
    assert len(samples) == 8
    first = samples[0]
    assert first.pid == 1001
    assert first.ppid == 1000
    assert first.pcpu == 12.0
    assert first.rss_kib == 204800
    assert first.exe_path == Path("/Applications/Orpheus.app/Contents/MacOS/Orpheus")

    helper = samples[1]
    assert helper.exe_path == Path(
        "/Applications/Orpheus.app/Contents/Frameworks/Orpheus Helper (Renderer).app/Contents/MacOS/Orpheus Helper"
    )
    spaced_volume = samples[7]
    assert spaced_volume.exe_path == Path("/Volumes/My Disk/Orpheus.app/Contents/MacOS/Orpheus")


def test_orpheus_tree_includes_helpers_backend_and_setup_excludes_unrelated() -> None:
    samples = parse_ps_output(PS_FIXTURE)
    tree = attribute_orpheus_tree(
        samples,
        app_bundle=Path("/Applications/Orpheus.app"),
        runtime_root=Path("/tmp/qualification-runtime"),
    )
    pids = {proc.pid for proc in tree}
    assert pids == {1001, 1002, 1003, 1004}
    assert 2001 not in pids
    assert 2002 not in pids
    assert 2003 not in pids


def test_orpheus_tree_attributes_bundle_under_spaced_volume_path() -> None:
    samples = parse_ps_output(PS_FIXTURE)
    tree = attribute_orpheus_tree(
        samples,
        app_bundle=Path("/Volumes/My Disk/Orpheus.app"),
        runtime_root=Path("/tmp/qualification-runtime"),
    )
    assert {proc.pid for proc in tree} == {2004}


def test_cpu_median_p95_and_rss_totals() -> None:
    cpu_series = [1.0, 2.0, 3.0, 4.0, 10.0]
    decision = evaluate_idle_cpu(cpu_series, budgets=_budgets())
    assert decision.metrics["cpu_median_percent"] == 3.0
    assert decision.metrics["cpu_p95_percent"] == pytest.approx(10.0)
    assert decision.passed is False  # p95 10 > 8

    samples = parse_ps_output(PS_FIXTURE)
    tree = attribute_orpheus_tree(
        samples,
        app_bundle=Path("/Applications/Orpheus.app"),
        runtime_root=Path("/tmp/qualification-runtime"),
    )
    rss_total = sum(proc.rss_kib for proc in tree)
    assert rss_total == 204800 + 51200 + 40960 + 10240


def test_launch_deadlines_use_provided_monotonic_timestamps() -> None:
    decision = evaluate_launch(
        started_at=100.0,
        ready_at=103.5,
        budgets=_budgets(),
        mode="warm",
    )
    assert decision.metrics["elapsed_seconds"] == pytest.approx(3.5)
    assert decision.passed is True


def test_fast_window_close_fails_when_backend_remains_alive() -> None:
    decision = evaluate_shutdown(
        close_requested_at=10.0,
        window_closed_at=10.2,
        process_tree_exited_at=None,
        backend_alive=True,
        remaining_pids=(1003,),
        budgets=_budgets(),
        monotonic_now=22.0,
    )
    assert decision.passed is False
    assert decision.metrics["window_close_seconds"] == pytest.approx(0.2)
    assert "backend" in decision.detail.lower() or "alive" in decision.detail.lower()


def test_prior_cycle_processes_must_not_survive_into_next_cycle() -> None:
    prior = [
        ProcessIdentity(
            pid=1001,
            start_time="100",
            exe="/Applications/Orpheus.app/Contents/MacOS/Orpheus",
        ),
        ProcessIdentity(
            pid=1003,
            start_time="101",
            exe="/Applications/Orpheus.app/Contents/Resources/binaries/orpheus-desktop-backend/orpheus-desktop-backend",
        ),
    ]
    current = [
        ProcessIdentity(
            pid=1001,
            start_time="100",
            exe="/Applications/Orpheus.app/Contents/MacOS/Orpheus",
        ),
        ProcessIdentity(
            pid=1005,
            start_time="200",
            exe="/Applications/Orpheus.app/Contents/MacOS/Orpheus",
        ),
    ]
    reused = [
        ProcessIdentity(
            pid=1001,
            start_time="999",
            exe="/Applications/Orpheus.app/Contents/MacOS/Orpheus",
        ),
    ]
    found = detect_cycle_survivors(prior=prior, current=current)
    assert found == {1001}
    assert detect_cycle_survivors(prior=prior, current=reused) == set()


def test_cycle_summary_identifies_exact_failing_iteration() -> None:
    results = [CycleResult(iteration=i, passed=True, detail="ok") for i in range(1, 26)]
    results[12] = CycleResult(iteration=13, passed=False, detail="backend orphan")
    summary = summarize_launch_cycles(results, expected_count=25)
    assert summary.passed is False
    assert summary.failing_iteration == 13
    assert summary.detail == "backend orphan"

    final_results = [CycleResult(iteration=i, passed=True, detail="ok") for i in range(1, 101)]
    final_results[76] = CycleResult(iteration=77, passed=False, detail="slow exit")
    final_summary = summarize_launch_cycles(final_results, expected_count=100)
    assert final_summary.failing_iteration == 77


def test_soak_memory_uses_settled_baseline_and_trend_not_single_sample() -> None:
    rss_mib = [1500.0, 800.0, 1200.0] + [1000.0] * 10 + [1008.0, 1016.0, 1024.0, 1032.0, 1040.0]
    ok = evaluate_soak_memory(rss_mib, budgets=_budgets())
    assert ok.passed is True
    assert "baseline_rss_mib" in ok.metrics
    assert ok.metrics["baseline_rss_mib"] == pytest.approx(1000.0)
    assert ok.metrics["growth_mib"] == pytest.approx(40.0)

    growing = [1000.0] * 10 + [1000.0 + (20.0 * i) for i in range(1, 12)]
    bad = evaluate_soak_memory(growing, budgets=_budgets())
    assert bad.passed is False
    assert bad.metrics["growth_mib"] > 100.0


def test_jsonl_sample_writer_emits_one_object_per_line(tmp_path: Path) -> None:
    samples = parse_ps_output(PS_FIXTURE)[:2]
    path = tmp_path / "sample.jsonl"
    write_jsonl_samples(path, samples, extra={"scenario": "idle-dashboard"})
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["pid"] == 1001
    assert first["scenario"] == "idle-dashboard"


def test_terminate_requires_fresh_resampled_identity_inside_bundle_or_runtime(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "Orpheus.app"
    runtime = tmp_path / "runtime"
    (bundle / "Contents" / "MacOS").mkdir(parents=True)
    (runtime / "bin").mkdir(parents=True)
    safe_exe = bundle / "Contents" / "MacOS" / "Orpheus"
    safe_exe.write_text("x", encoding="utf-8")
    runtime_exe = runtime / "bin" / "helper"
    runtime_exe.write_text("x", encoding="utf-8")
    foreign = tmp_path / "other" / "binary"
    foreign.parent.mkdir()
    foreign.write_text("x", encoding="utf-8")

    killed: list[int] = []
    live: dict[int, ProcessSample] = {
        42: ProcessSample(
            pid=42,
            ppid=1,
            pcpu=0.0,
            rss_kib=1,
            comm="Orpheus",
            args=str(safe_exe),
            start_time="100",
        ),
        43: ProcessSample(
            pid=43,
            ppid=1,
            pcpu=0.0,
            rss_kib=1,
            comm="helper",
            args=str(runtime_exe),
            start_time="101",
        ),
        44: ProcessSample(
            pid=44,
            ppid=1,
            pcpu=0.0,
            rss_kib=1,
            comm="foreign",
            args=str(foreign),
            start_time="102",
        ),
        45: ProcessSample(
            pid=45,
            ppid=1,
            pcpu=0.0,
            rss_kib=1,
            comm="foreign",
            args=str(foreign),
            start_time="999",
        ),
    }

    def fake_kill(pid: int, sig: int) -> None:
        killed.append(pid)

    def read_process(pid: int) -> ProcessSample | None:
        return live.get(pid)

    assert (
        terminate_pid_if_safe(
            42,
            expected_exe_path=safe_exe,
            expected_start_time="100",
            app_bundle=bundle,
            runtime_root=runtime,
            kill=fake_kill,
            read_process=read_process,
        )
        is True
    )
    assert (
        terminate_pid_if_safe(
            43,
            expected_exe_path=runtime_exe,
            expected_start_time="101",
            app_bundle=bundle,
            runtime_root=runtime,
            kill=fake_kill,
            read_process=read_process,
        )
        is True
    )
    assert (
        terminate_pid_if_safe(
            44,
            expected_exe_path=foreign,
            expected_start_time="102",
            app_bundle=bundle,
            runtime_root=runtime,
            kill=fake_kill,
            read_process=read_process,
        )
        is False
    )
    assert (
        terminate_pid_if_safe(
            45,
            expected_exe_path=safe_exe,
            expected_start_time="100",
            app_bundle=bundle,
            runtime_root=runtime,
            kill=fake_kill,
            read_process=read_process,
        )
        is False
    )
    assert killed == [42, 43]


def test_observer_warm_launch_requires_external_action_ready_handshake(
    tmp_path: Path,
) -> None:
    (tmp_path / "Orpheus.app").mkdir()
    (tmp_path / "runtime").mkdir()
    session = ObserverSession(
        scenario="warm-launch",
        report_dir=tmp_path,
        budgets=_budgets(warm_launch_seconds=5.0),
        app_bundle=tmp_path / "Orpheus.app",
        runtime_root=tmp_path / "runtime",
        sampler=lambda: [],
    )
    with pytest.raises(ValueError, match="action_started_at|ui_ready_at|handshake"):
        session.run()

    decision = session.run(action_started_at=10.0, ui_ready_at=12.0)
    assert decision.passed is True
    assert decision.metrics["elapsed_seconds"] == pytest.approx(2.0)


def test_observer_idle_honors_monotonic_duration_with_cadence_and_jsonl(
    tmp_path: Path,
) -> None:
    clock = {"t": 0.0}
    sleeps: list[float] = []
    sample_n = {"n": 0}

    def mono() -> float:
        return clock["t"]

    def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        clock["t"] += seconds

    def sampler() -> list[ProcessSample]:
        sample_n["n"] += 1
        return [
            ProcessSample(
                pid=1,
                ppid=0,
                pcpu=1.0,
                rss_kib=1024,
                comm="Orpheus",
                args=str(tmp_path / "Orpheus.app" / "Contents" / "MacOS" / "Orpheus"),
                start_time="1",
            )
        ]

    (tmp_path / "Orpheus.app" / "Contents" / "MacOS").mkdir(parents=True)
    (tmp_path / "Orpheus.app" / "Contents" / "MacOS" / "Orpheus").write_text("x")
    (tmp_path / "runtime").mkdir()
    session = ObserverSession(
        scenario="idle-dashboard",
        report_dir=tmp_path,
        budgets=_budgets(),
        app_bundle=tmp_path / "Orpheus.app",
        runtime_root=tmp_path / "runtime",
        monotonic_clock=mono,
        sampler=sampler,
        sleep=sleeper,
        sample_interval_seconds=1.0,
    )
    decision = session.run(seconds=3.0)
    assert decision.passed is True
    assert sample_n["n"] >= 3
    assert sleeps
    assert clock["t"] >= 3.0
    lines = (
        (tmp_path / "metrics" / "idle-dashboard.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()
    )
    assert len(lines) >= 3
    assert "monotonic_ts" in json.loads(lines[0])


def test_observer_shutdown_fails_when_tree_already_empty(tmp_path: Path) -> None:
    (tmp_path / "Orpheus.app").mkdir()
    (tmp_path / "runtime").mkdir()
    session = ObserverSession(
        scenario="shutdown",
        report_dir=tmp_path,
        budgets=_budgets(),
        app_bundle=tmp_path / "Orpheus.app",
        runtime_root=tmp_path / "runtime",
        sampler=lambda: [],
    )
    decision = session.run()
    assert decision.passed is False
    assert "empty" in decision.detail.lower() or "already" in decision.detail.lower()


def _shutdown_fixture_paths(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    app = tmp_path / "Orpheus.app"
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    (macos / "Orpheus").write_text("x", encoding="utf-8")
    backend = (
        app
        / "Contents"
        / "Resources"
        / "binaries"
        / "orpheus-desktop-backend"
        / "orpheus-desktop-backend"
    )
    backend.parent.mkdir(parents=True)
    backend.write_text("x", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    return app, runtime, macos / "Orpheus", backend


def _proc(
    pid: int,
    ppid: int,
    exe: Path,
    *,
    pcpu: float = 1.0,
    rss_kib: int = 1024,
) -> ProcessSample:
    return ProcessSample(
        pid=pid,
        ppid=ppid,
        pcpu=pcpu,
        rss_kib=rss_kib,
        comm=exe.name,
        args=str(exe),
        start_time=str(pid),
    )


def test_observer_shutdown_waits_for_external_exit_then_passes(tmp_path: Path) -> None:
    """Ready marker handshake: keep sampling until Computer Use quit clears the tree."""
    app, runtime, ui_exe, backend_exe = _shutdown_fixture_paths(tmp_path)
    clock = {"t": 0.0}
    calls = {"n": 0}

    def mono() -> float:
        return clock["t"]

    def sleeper(seconds: float) -> None:
        clock["t"] += seconds

    def sampler() -> list[ProcessSample]:
        calls["n"] += 1
        if calls["n"] < 4:
            return [
                _proc(1001, 1, ui_exe),
                _proc(1003, 1001, backend_exe),
            ]
        return []

    session = ObserverSession(
        scenario="shutdown",
        report_dir=tmp_path,
        budgets=_budgets(window_close_seconds=1.0, process_exit_seconds=12.0),
        app_bundle=app,
        runtime_root=runtime,
        monotonic_clock=mono,
        sampler=sampler,
        sleep=sleeper,
        sample_interval_seconds=1.0,
    )
    decision = session.run()
    assert calls["n"] >= 4, "observer must remain active after ready, not one-shot fail"
    assert decision.passed is True, decision.detail
    # Close is measured from the last sample that still had UI, not observe start.
    # Shutdown polls at window_close/10 (0.1s here), so one cadence step separates
    # last-UI from empty-tree samples under this fixture.
    assert decision.metrics["process_exit_seconds"] == pytest.approx(0.1)
    assert decision.metrics["window_close_seconds"] == pytest.approx(0.1)
    lines = (
        (tmp_path / "metrics" / "shutdown.jsonl").read_text(encoding="utf-8").strip().splitlines()
    )
    assert len(lines) >= 4


def test_observer_shutdown_times_out_when_quit_never_happens(tmp_path: Path) -> None:
    app, runtime, ui_exe, backend_exe = _shutdown_fixture_paths(tmp_path)
    clock = {"t": 0.0}
    calls = {"n": 0}

    def mono() -> float:
        return clock["t"]

    def sleeper(seconds: float) -> None:
        clock["t"] += seconds

    def sampler() -> list[ProcessSample]:
        calls["n"] += 1
        return [
            _proc(1001, 1, ui_exe),
            _proc(1003, 1001, backend_exe),
        ]

    session = ObserverSession(
        scenario="shutdown",
        report_dir=tmp_path,
        budgets=_budgets(process_exit_seconds=3.0, window_close_seconds=1.0),
        app_bundle=app,
        runtime_root=runtime,
        monotonic_clock=mono,
        sampler=sampler,
        sleep=sleeper,
        sample_interval_seconds=1.0,
    )
    decision = session.run()
    assert decision.passed is False
    assert "timeout" in decision.detail.lower() or "did not" in decision.detail.lower()
    assert calls["n"] >= 3
    assert clock["t"] >= 3.0


def test_observer_shutdown_fails_when_backend_orphaned_after_ui_exit(
    tmp_path: Path,
) -> None:
    app, runtime, ui_exe, backend_exe = _shutdown_fixture_paths(tmp_path)
    clock = {"t": 0.0}
    calls = {"n": 0}

    def mono() -> float:
        return clock["t"]

    def sleeper(seconds: float) -> None:
        clock["t"] += seconds

    def sampler() -> list[ProcessSample]:
        calls["n"] += 1
        if calls["n"] == 1:
            return [
                _proc(1001, 1, ui_exe),
                _proc(1003, 1001, backend_exe),
            ]
        # External quit dismissed the UI but left the backend orphaned.
        return [_proc(1003, 1, backend_exe)]

    session = ObserverSession(
        scenario="shutdown",
        report_dir=tmp_path,
        budgets=_budgets(window_close_seconds=1.0, process_exit_seconds=2.0),
        app_bundle=app,
        runtime_root=runtime,
        monotonic_clock=mono,
        sampler=sampler,
        sleep=sleeper,
        sample_interval_seconds=1.0,
    )
    decision = session.run()
    assert decision.passed is False
    assert "backend" in decision.detail.lower() or "alive" in decision.detail.lower()
    assert calls["n"] >= 3


def test_observer_shutdown_resolves_fast_exit_despite_sampler_overhead(
    tmp_path: Path,
) -> None:
    """1.0s idle cadence + ~0.15s ps overhead cannot resolve a 1.0s window-close budget.

    Live false-fail: tree exits cleanly, but measured window_close_seconds≈1.156 because
    shutdown polling reused sample_interval_seconds=1.0 and each sampler call advances
    the clock. Shutdown transition cadence must be materially finer than the budget.
    """
    app, runtime, ui_exe, backend_exe = _shutdown_fixture_paths(tmp_path)
    clock = {"t": 0.0}
    sleeps: list[float] = []
    sampler_overhead = 0.15
    # Real exit well inside the 1.0s budget — invisible under 1.0s + overhead polling.
    exit_at = 0.25

    def mono() -> float:
        return clock["t"]

    def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        clock["t"] += seconds

    def sampler() -> list[ProcessSample]:
        clock["t"] += sampler_overhead
        if clock["t"] < exit_at:
            return [
                _proc(1001, 1, ui_exe),
                _proc(1003, 1001, backend_exe),
            ]
        return []

    session = ObserverSession(
        scenario="shutdown",
        report_dir=tmp_path,
        budgets=_budgets(window_close_seconds=1.0, process_exit_seconds=12.0),
        app_bundle=app,
        runtime_root=runtime,
        monotonic_clock=mono,
        sampler=sampler,
        sleep=sleeper,
        sample_interval_seconds=1.0,
    )
    decision = session.run()
    assert decision.passed is True, decision.detail
    assert float(decision.metrics["window_close_seconds"]) <= 1.0
    assert sleeps, "shutdown must poll with sleeps between samples"
    assert max(sleeps) < 1.0, "shutdown cadence must be finer than idle/soak interval"
