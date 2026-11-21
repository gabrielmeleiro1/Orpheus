"""Model deletion survives process death using an isolated cache and setup state."""

import os
from queue import Queue
import threading
import subprocess
import sys

import pytest

from tests.test_setup_model_management import (
    _link_shared_blob,
    requires_shared_blob_store,
    storage as storage_fixture,
)
from scripts import setup_runtime


storage = storage_fixture

_CHILD = """
from pathlib import Path
from types import SimpleNamespace
import sys
from fastapi.testclient import TestClient
from modules.runtime_profiles import RuntimeProfile
from modules.app.services import model_storage_service as storage
from modules.runtime_setup import hub_cache
from modules.web.server import create_app
from scripts import setup_runtime

RuntimeProfile.is_compatible = lambda self, **kwargs: True
mode, endpoint, boundary, occurrence = sys.argv[1:]
hits = 0
def pause():
    global hits
    hits += 1
    if hits != int(occurrence):
        return
    print('boundary', flush=True)
    sys.stdin.buffer.read(1)

if mode == 'delete':
    remove = storage.shutil.rmtree
    def delete(path, **kwargs):
        result = remove(path, **kwargs)
        if boundary == 'repo' and Path(path).parent == Path(hub_cache.hf_constants.HF_HUB_CACHE):
            pause()
        return result
    storage.shutil.rmtree = delete
    atomic_write = setup_runtime._atomic_write_json
    def write(path, payload):
        atomic_write(path, payload)
        if Path(path).name.startswith('.orpheus-delete-'):
            if boundary == 'manifest' and not payload['settings_committed']:
                pause()
            if boundary == 'flag' and payload['settings_committed']:
                pause()
    storage.setup_runtime._atomic_write_json = write
    rename = Path.rename
    def move(path, target):
        result = rename(path, target)
        if boundary == 'rename' and Path(target).name.startswith('.orpheus-delete-'):
            pause()
        return result
    Path.rename = move
    unlink = Path.unlink
    def retire(path, **kwargs):
        result = unlink(path, **kwargs)
        if boundary == 'state' and path == setup_runtime.default_state_path():
            pause()
        if boundary == 'retire' and path.name.startswith('.orpheus-delete-') and path.suffix == '.json':
            pause()
        return result
    Path.unlink = retire
    if boundary in {'before-sweep', 'after-sweep'}:
        from huggingface_hub.utils import _shared_blobs
        sweep = _shared_blobs.sweep_shared_blob
        def collect(*args, **kwargs):
            if boundary == 'before-sweep':
                pause()
            result = sweep(*args, **kwargs)
            if boundary == 'after-sweep':
                pause()
            return result
        _shared_blobs.sweep_shared_blob = collect
    with TestClient(create_app(controller=object())) as client:
        response = client.delete(endpoint)
        assert response.status_code == 200, response.text
else:
    if mode == 'fail-sweep':
        from huggingface_hub.utils import _shared_blobs
        def locked(*args, **kwargs):
            raise PermissionError('locked')
        _shared_blobs.sweep_shared_blob = locked
    app_state = SimpleNamespace()
    with setup_runtime.SetupLock(setup_runtime.default_state_path(), timeout_seconds=1):
        storage._recover_staged_models(
            on_uninstall_complete=lambda reset_id: storage.reset_setup_preferences(None, app_state, reset_id),
        )
"""


def _command(cache, mode, endpoint="", boundary="", occurrence=1):
    return [sys.executable, "-c", _CHILD, mode, endpoint, boundary, str(occurrence)], {
        **os.environ,
        "HF_HUB_CACHE": str(cache),
        "HF_HOME": str(cache.parent / "hf-home"),
    }


def _kill_deletion(cache, endpoint, boundary="repo", occurrence=1):
    command, env = _command(cache, "delete", endpoint, boundary, occurrence)
    with subprocess.Popen(
        command,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as child:
        try:
            ready = Queue()
            reader = threading.Thread(
                target=lambda: ready.put(child.stdout.readline()), daemon=True
            )
            reader.start()
            assert ready.get(timeout=20).strip() == "boundary", child.stderr.read()
            reader.join(timeout=1)
        finally:
            child.kill()
            child.wait(timeout=10)


def _recover(cache, *, fail=False):
    command, env = _command(cache, "fail-sweep" if fail else "recover")
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("kind", ["tts", "unselected", "runtime"])
@requires_shared_blob_store
def test_repo_removal_keeps_enough_evidence_to_complete_deletion(storage, kind):
    cache, state_path, repos = storage
    if kind == "unselected":
        setup_runtime.write_setup_state(
            state_path, profile_id="base", model_groups=()
        )
    before = state_path.read_bytes()
    group = "tts-cpu-cuda" if kind == "tts" else "cleaner-cuda"
    payloads = [
        _link_shared_blob(cache, repos[g], prefix + "0" * 62, b"weights")
        for g, prefix in [("tts-cpu-cuda", "e1"), ("cleaner-cuda", "e2")]
    ]
    shared = _link_shared_blob(cache, repos[group], "e3" + "0" * 62, b"shared")
    _link_shared_blob(cache, repos["unrelated"], shared.name, b"shared")
    endpoint = "/api/setup/runtime" if kind == "runtime" else f"/api/setup/models/{group}"
    _kill_deletion(cache, endpoint)
    assert list(cache.glob(".orpheus-delete-*.json")), "Removed repositories need recovery evidence"
    for _ in range(2):
        _recover(cache)
        assert not repos[group].exists()
        assert shared.read_bytes() == b"shared"
        assert repos["unrelated"].exists()
        assert not list(cache.glob(".orpheus-delete-*"))
        if kind == "runtime":
            assert not state_path.exists()
            assert all(not payload.exists() for payload in payloads)
        else:
            assert state_path.read_bytes() == before
            assert not payloads[0 if kind == "tts" else 1].exists()


@requires_shared_blob_store
def test_failed_recovery_sweep_keeps_candidates_for_a_fresh_restart(storage):
    cache, _, repos = storage
    payload = _link_shared_blob(cache, repos["cleaner-cuda"], "e4" + "0" * 62, b"weights")
    _kill_deletion(cache, "/api/setup/models/cleaner-cuda")
    _recover(cache, fail=True)
    assert payload.exists()
    assert list(cache.glob(".orpheus-delete-*.json"))
    for _ in range(2):
        _recover(cache)
        assert not payload.exists()
        assert not list(cache.glob(".orpheus-delete-*"))


@pytest.mark.parametrize("kind", ["tts", "unselected", "runtime"])
@pytest.mark.parametrize(
    "boundary", ["manifest", "rename", "flag", "before-sweep", "after-sweep", "retire"]
)
@requires_shared_blob_store
def test_deletion_boundaries_restore_or_finish_models_on_restart(storage, kind, boundary):
    cache, state_path, repos = storage
    if kind == "unselected":
        setup_runtime.write_setup_state(
            state_path, profile_id="base", model_groups=()
        )
    before = state_path.read_bytes()
    group = "tts-cpu-cuda" if kind == "tts" else "cleaner-cuda"
    payload = _link_shared_blob(cache, repos[group], "e5" + "0" * 62, b"weights")
    endpoint = "/api/setup/runtime" if kind == "runtime" else f"/api/setup/models/{group}"
    _kill_deletion(cache, endpoint, boundary)
    committed = boundary not in {"manifest", "rename"}
    for _ in range(2):
        _recover(cache)
        assert repos[group].exists() is not committed
        assert payload.exists() is not committed
        assert not list(cache.glob(".orpheus-delete-*"))
        if kind == "runtime" and committed:
            assert not state_path.exists()
        else:
            assert state_path.read_bytes() == before


@pytest.mark.parametrize("configured", [True, False])
@requires_shared_blob_store
def test_visible_setup_state_removal_commits_uninstall_after_process_death(storage, configured):
    cache, state_path, repos = storage
    if not configured:
        state_path.unlink()
    _kill_deletion(cache, "/api/setup/runtime", "state")
    assert list(cache.glob(".orpheus-delete-*.json"))
    for _ in range(2):
        _recover(cache)
        assert not state_path.exists()
        assert repos["tts-cpu-cuda"].exists() is not configured
        assert repos["cleaner-cuda"].exists() is not configured
        assert repos["unrelated"].exists()
        assert not list(cache.glob(".orpheus-delete-*"))


@pytest.mark.parametrize(
    "boundary", ["manifest", "rename", "flag", "repo", "before-sweep", "after-sweep", "retire"]
)
@pytest.mark.parametrize("configured", [True, False])
@requires_shared_blob_store
def test_uninstall_recovers_after_each_repository_boundary(storage, boundary, configured):
    cache, state_path, repos = storage
    if not configured:
        state_path.unlink()
    before = state_path.read_bytes() if configured else None
    payloads = [
        _link_shared_blob(cache, repos[group], prefix + "0" * 62, b"weights")
        for group, prefix in [("tts-cpu-cuda", "f1"), ("cleaner-cuda", "f2")]
    ]
    _kill_deletion(cache, "/api/setup/runtime", boundary, occurrence=2)
    if boundary != "retire":
        assert list(cache.glob(".orpheus-delete-*.json"))
    committed = boundary not in {"manifest", "rename"}
    for _ in range(2):
        _recover(cache)
        for group in ("tts-cpu-cuda", "cleaner-cuda"):
            assert repos[group].exists() is not committed
        for payload in payloads:
            assert payload.exists() is not committed
        if committed:
            assert not state_path.exists()
        elif configured:
            assert state_path.read_bytes() == before
        else:
            assert not state_path.exists()
        assert repos["unrelated"].exists()
        assert not list(cache.glob(".orpheus-delete-*"))


@requires_shared_blob_store
def test_first_committed_flag_commits_every_unconfigured_uninstall_repo(storage):
    cache, state_path, repos = storage
    state_path.unlink()
    _kill_deletion(cache, "/api/setup/runtime", "flag", occurrence=1)
    for _ in range(2):
        _recover(cache)
        assert not state_path.exists()
        assert not repos["tts-cpu-cuda"].exists()
        assert not repos["cleaner-cuda"].exists()
        assert repos["unrelated"].exists()
        assert not list(cache.glob(".orpheus-delete-*"))
