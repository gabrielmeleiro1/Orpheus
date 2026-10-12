"""Deterministic setup-lock exclusion regression on synthetic directories only."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest


@pytest.fixture
def setup_state():
    from modules.runtime_setup import state

    return state


def test_stale_recovery_cannot_steal_new_live_owner(tmp_path, monkeypatch, setup_state):
    state = setup_state
    state_path = tmp_path / "runtime-setup.json"
    lock_path = state_path.with_name(state_path.name + ".lock")
    lock_path.mkdir()
    dead_pid = 987654321
    (lock_path / "owner.json").write_text(json.dumps({
        "pid": dead_pid, "created_at": 0, "token": "synthetic-dead-owner",
    }))

    old_owner_observed = threading.Event()
    allow_recovery = threading.Event()
    first_entered = threading.Event()
    release_first = threading.Event()
    failures = []
    real_pid_running = state._pid_is_running

    def controlled_liveness(pid):
        if pid != dead_pid:
            return real_pid_running(pid)
        if threading.current_thread().name == "delayed-recovery":
            old_owner_observed.set()
            if not allow_recovery.wait(3):
                raise AssertionError("Test did not release the delayed recovery")
        return False

    monkeypatch.setattr(state, "_pid_is_running", controlled_liveness)

    def first_contender():
        try:
            with state.SetupLock(state_path, timeout_seconds=2):
                first_entered.set()
                if not release_first.wait(3):
                    raise AssertionError("Test did not release the first lock owner")
        except BaseException as error:
            failures.append(error)

    first = threading.Thread(name="delayed-recovery", target=first_contender)
    first.start()
    overlapping_owners = False
    try:
        assert old_owner_observed.wait(2), "First contender never observed the dead owner"
        try:
            with state.SetupLock(state_path, timeout_seconds=0.15):
                allow_recovery.set()
                overlapping_owners = first_entered.wait(1)
                release_first.set()
                first.join(timeout=3)
        except state.SetupExecutionError:
            # An atomic recovery guard correctly keeps this contender out.
            allow_recovery.set()
            assert first_entered.wait(2)
    finally:
        allow_recovery.set()
        release_first.set()
        first.join(timeout=4)
    assert not first.is_alive(), "Regression leaked a worker thread"
    assert not failures, repr(failures)
    assert not overlapping_owners, "Stale recovery stole a live replacement lock: both owners entered"


@pytest.mark.parametrize("hold_directory", [False, True])
def test_cross_process_owner_crash_releases_setup_gate(tmp_path, setup_state, hold_directory):
    from modules.subprocess_utils import background_process_kwargs

    state_path = tmp_path / "runtime-setup.json"
    ready = tmp_path / "ready"
    code = """
from pathlib import Path
import sys
import time
from modules.runtime_setup.state import SetupLock
lock = SetupLock(Path(sys.argv[1]), timeout_seconds=2)
guard = lock if sys.argv[3] == 'True' else lock._metadata_gate(time.monotonic() + 2)
with guard:
    Path(sys.argv[2]).write_text('ready', encoding='utf-8')
    time.sleep(60)
"""
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", code, str(state_path), str(ready), str(hold_directory)],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **background_process_kwargs(),
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "Child did not acquire its synthetic setup lock"
        with pytest.raises(setup_state.SetupExecutionError, match="already running"):
            with setup_state.SetupLock(state_path, timeout_seconds=0.1):
                pytest.fail("Another process must not enter while the owner is alive")
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=5)
    with setup_state.SetupLock(state_path, timeout_seconds=2):
        assert state_path.with_name(state_path.name + ".lock").is_dir()


def test_failed_owner_publication_releases_gate(tmp_path, monkeypatch, setup_state):
    import errno

    writer = setup_state._atomic_write_json

    def disk_full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "synthetic disk full")

    monkeypatch.setattr(setup_state, "_atomic_write_json", disk_full)
    state_path = tmp_path / "runtime-setup.json"
    with pytest.raises(setup_state.SetupExecutionError, match="synthetic disk full"):
        with setup_state.SetupLock(state_path, timeout_seconds=0.1):
            pytest.fail("Publication must not succeed on a full disk")
    monkeypatch.setattr(setup_state, "_atomic_write_json", writer)
    with setup_state.SetupLock(state_path, timeout_seconds=0.1):
        assert state_path.with_name(state_path.name + ".lock").is_dir()


def test_release_gate_timeout_keeps_ownership_for_retry(tmp_path, setup_state):
    lock = setup_state.SetupLock(tmp_path / "runtime-setup.json", timeout_seconds=0.05)
    lock.__enter__()
    try:
        with lock._metadata_gate(time.monotonic() + 0.1):
            started = time.monotonic()
            with pytest.raises(setup_state.SetupExecutionError, match="already running"):
                lock.__exit__(None, None, None)
            assert time.monotonic() - started < 1
            assert lock.path.exists()
    finally:
        lock.__exit__(None, None, None)
    assert not lock.path.exists()
