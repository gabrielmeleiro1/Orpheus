"""Regression coverage for runtime setup state."""

from types import SimpleNamespace

import pytest

from scripts import setup_runtime

from tests._factories_group2_paths import isolated_paths  # noqa: F401  (autouse fixture)


@pytest.mark.parametrize("system,os_name,environment,relative", [
    ("darwin", "posix", {}, "home/Library/Application Support/com.orpheus.desktop/runtime-setup.json"),
    ("win32", "nt", {}, "home/AppData/Roaming/com.orpheus.desktop/runtime-setup.json"),
    ("win32", "nt", {"APPDATA": "roaming", "LOCALAPPDATA": "local"}, "roaming/com.orpheus.desktop/runtime-setup.json"),
    ("linux", "posix", {}, "home/.local/share/com.orpheus.desktop/runtime-setup.json"),
    ("linux", "posix", {"XDG_DATA_HOME": "xdg"}, "xdg/com.orpheus.desktop/runtime-setup.json"),
])
def test_setup_state_defaults_follow_operating_system(
    tmp_path, monkeypatch, system, os_name, environment, relative,
):
    monkeypatch.setattr("modules.runtime_setup.state.sys", SimpleNamespace(platform=system))
    monkeypatch.setattr("modules.runtime_setup.state.os", SimpleNamespace(name=os_name))
    environment = {key: str(tmp_path / value) for key, value in environment.items()}
    # The desktop shell's Tauri app data directory, never the Windows install folder.
    assert setup_runtime.default_state_path(environ=environment) == tmp_path / relative
    assert not (tmp_path / relative).exists()


@pytest.mark.parametrize("system,os_name", [("darwin", "posix"), ("win32", "nt"), ("linux", "posix")])
def test_desktop_data_override_places_setup_state_beside_data(tmp_path, monkeypatch, system, os_name):
    monkeypatch.setattr("modules.runtime_setup.state.sys", SimpleNamespace(platform=system))
    monkeypatch.setattr("modules.runtime_setup.state.os", SimpleNamespace(name=os_name))
    state = setup_runtime.default_state_path(environ={
        "AUDIOBOOK_DATA_DIR": "  desktop/data  ",
        "LOCALAPPDATA": str(tmp_path / "unused-local"),
        "XDG_DATA_HOME": str(tmp_path / "unused-xdg"),
    })
    assert state == tmp_path.resolve() / "desktop/runtime-setup.json"
    assert not state.parent.exists()


def _write_lock_owner(lock_path, *, pid, created_at):
    import json

    lock_path.mkdir()
    (lock_path / "owner.json").write_text(
        json.dumps({"pid": pid, "created_at": created_at, "token": "previous-launch"}),
        encoding="utf-8",
    )


def test_lock_whose_owner_pid_was_reused_is_recovered(tmp_path):
    import os

    import psutil

    from modules.runtime_setup import state

    state_path = tmp_path / "runtime-setup.json"
    lock_path = state_path.with_name(f"{state_path.name}.lock")
    # A crashed launch recorded the lock before this live process started.
    _write_lock_owner(lock_path, pid=os.getpid(), created_at=psutil.Process().create_time() - 60)

    with state.SetupLock(state_path, timeout_seconds=0.5):
        assert lock_path.is_dir()
    assert not lock_path.exists()


def test_lock_recorded_after_its_live_owner_started_stays_held(tmp_path):
    import os
    import time

    from modules.runtime_setup import state

    state_path = tmp_path / "runtime-setup.json"
    lock_path = state_path.with_name(f"{state_path.name}.lock")
    _write_lock_owner(lock_path, pid=os.getpid(), created_at=time.time())

    with pytest.raises(state.SetupExecutionError, match="already running"):
        with state.SetupLock(state_path, timeout_seconds=0.05):
            pytest.fail("A live owner's lock must remain held")
    assert lock_path.is_dir()
