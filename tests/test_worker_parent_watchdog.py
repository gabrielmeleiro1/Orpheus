"""Pool workers exit when the backend that spawned them dies."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import psutil
import pytest

from modules.pipeline import worker_runtime

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_watchdog_exits_worker_once_parent_is_gone(monkeypatch):
    exited = threading.Event()
    codes: list[int] = []

    class GoneParent:
        def join(self):
            return None

    def fake_exit(code):
        codes.append(code)
        exited.set()

    monkeypatch.setattr(worker_runtime.multiprocessing, "parent_process", lambda: GoneParent())
    monkeypatch.setattr(worker_runtime.os, "_exit", fake_exit)

    worker_runtime._init_worker(available_vram_gb=1.0)

    assert exited.wait(timeout=5)
    assert codes == [worker_runtime._PARENT_GONE_EXIT_CODE]


def test_thread_backed_workers_start_no_watchdog(monkeypatch):
    monkeypatch.setattr(worker_runtime.multiprocessing, "parent_process", lambda: None)
    before = {thread.name for thread in threading.enumerate()}

    worker_runtime._init_worker(available_vram_gb=1.0)

    started = {thread.name for thread in threading.enumerate()} - before
    assert "worker-parent-watchdog" not in started


_PARENT_SCRIPT = textwrap.dedent(
    """
    import json, os, sys, threading, time
    sys.path.insert(0, sys.argv[1])
    import psutil
    from modules.pipeline.pool_manager import SharedWorkerPoolManager

    manager = SharedWorkerPoolManager(max_workers=2)
    threading.Thread(
        target=lambda: manager.run_parallel(
            chapters=[60, 60], process_func=time.sleep, config_fingerprint=("watchdog",),
        ),
        daemon=True,
    ).start()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        workers = [
            child.pid for child in psutil.Process().children(recursive=True)
            if "multiprocessing" in " ".join(child.cmdline())
        ]
        if len(workers) >= 2:
            break
        time.sleep(0.1)
    # A busy but live parent must not trip the watchdog.
    busy_until = time.monotonic() + 2.0
    while time.monotonic() < busy_until:
        pass
    alive_while_busy = [pid for pid in workers if psutil.pid_exists(pid)]
    print(json.dumps({"workers": workers, "alive_while_busy": alive_while_busy}), flush=True)
    os._exit(9)
    """
)


@pytest.mark.slow
def test_pool_workers_do_not_outlive_a_killed_backend(tmp_path):
    # time.sleep is called as time.sleep(chapter, idx, None) and raises in the
    # worker; the pool still spawns workers, which is all this test needs.
    out_file = tmp_path / "workers.json"
    with out_file.open("w", encoding="utf-8") as out:
        parent = subprocess.Popen(
            [sys.executable, "-c", _PARENT_SCRIPT, str(REPO_ROOT)],
            stdout=out, stderr=subprocess.DEVNULL, cwd=REPO_ROOT,
        )
        assert parent.wait(timeout=120) == 9
    report = json.loads(out_file.read_text(encoding="utf-8").strip().splitlines()[-1])
    workers = report["workers"]
    assert workers, "the backend stand-in never started pool workers"
    assert report["alive_while_busy"] == workers
    deadline = time.monotonic() + 15
    alive = workers
    while alive and time.monotonic() < deadline:
        time.sleep(0.2)
        alive = [pid for pid in workers if psutil.pid_exists(pid)]
    for pid in alive:
        os.kill(pid, 9)
    assert alive == []
