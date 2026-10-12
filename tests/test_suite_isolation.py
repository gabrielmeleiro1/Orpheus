"""Keep default user storage inside each test's temporary directory."""

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from huggingface_hub import constants
from huggingface_hub.utils import _cache_manager
import pytest

from modules.runtime_setup.state import default_state_path
from tests import conftest


def test_default_home_and_setup_storage_are_temporary(_isolated_user_paths, monkeypatch):
    root = _isolated_user_paths
    monkeypatch.delenv("AUDIOBOOK_DATA_DIR", raising=False)

    assert Path.home().is_relative_to(root)
    assert default_state_path().is_relative_to(root)
    for name in (
        "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "XDG_DATA_HOME",
        "XDG_CACHE_HOME", "HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE",
        "HF_TOKEN_PATH", "HF_STORED_TOKENS_PATH",
    ):
        assert Path(os.environ[name]).is_relative_to(root), name


def test_preimported_hub_cache_and_token_paths_are_temporary(_isolated_user_paths):
    for name in (
        "HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "HF_ASSETS_CACHE",
        "HUGGINGFACE_ASSETS_CACHE", "HF_TOKEN_PATH", "HF_STORED_TOKENS_PATH",
    ):
        assert Path(getattr(constants, name)).is_relative_to(_isolated_user_paths), name
    assert Path(_cache_manager.HF_HUB_CACHE).is_relative_to(_isolated_user_paths)


def test_subprocess_inherits_temporary_home_and_hub_paths(_isolated_user_paths):
    result = subprocess.run(
        [sys.executable, "-c", "\n".join((
            "from pathlib import Path",
            "import sys",
            "from huggingface_hub import constants",
            "root = Path(sys.argv[1])",
            "assert Path.home().is_relative_to(root)",
            "assert Path(constants.HF_HUB_CACHE).is_relative_to(root)",
            "assert Path(constants.HF_TOKEN_PATH).is_relative_to(root)",
        )), str(_isolated_user_paths)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_isolation_replaces_cached_paths_and_removes_inherited_tokens(tmp_path, monkeypatch):
    inherited = tmp_path / "inherited"
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(inherited))
    monkeypatch.setattr(constants, "HF_TOKEN_PATH", str(inherited / "token"))
    monkeypatch.setattr(_cache_manager, "HF_HUB_CACHE", str(inherited))
    for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HF_OIDC_RESOURCE", "HF_OIDC_ID_TOKEN"):
        monkeypatch.setenv(name, "inherited-value")

    isolated = tmp_path / "isolated"
    conftest._isolate_user_paths(monkeypatch, isolated)

    assert Path(constants.HF_HUB_CACHE).is_relative_to(isolated)
    assert Path(constants.HF_TOKEN_PATH).is_relative_to(isolated)
    assert Path(_cache_manager.HF_HUB_CACHE).is_relative_to(isolated)
    for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HF_OIDC_RESOURCE", "HF_OIDC_ID_TOKEN"):
        assert name not in os.environ


def test_application_paths_stay_temporary_after_changing_directory(tmp_path, monkeypatch):
    from modules.app.config import AppConfig
    from modules.storage.sqlite_repo import SQLiteRepository

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUDIOBOOK_DB_PATH", str(tmp_path / "inherited.db"))
    conftest._isolate_user_paths(monkeypatch, Path("isolated"))
    assert Path.cwd() == tmp_path
    for name in ("AUDIOBOOK_DATA_DIR", "AUDIOBOOK_OUTPUT_DIR", "AUDIOBOOK_TEMP_DIR"):
        assert Path(os.environ[name]).is_absolute()
        assert Path(os.environ[name]).is_relative_to(tmp_path / "isolated")

    working = tmp_path / "working"
    working.mkdir()
    env_file = working / ".env.local"
    env_file.write_text("AUDIOBOOK_DB_PATH=unisolated/library.db\n", encoding="utf-8")
    monkeypatch.chdir(working)
    config = AppConfig()
    repo = SQLiteRepository(config.db_path)
    try:
        assert repo.db_path.is_relative_to(tmp_path / "isolated")
        assert config.db_path.is_file()
        assert set(working.iterdir()) == {env_file}
    finally:
        repo.close()


@pytest.mark.parametrize("directory", ["data", "tests/data"])
@pytest.mark.parametrize("change", ["unchanged", "added", "modified", "deleted", "created-root"])
def test_session_reports_repository_storage_changes_without_removing_files(
    tmp_path, monkeypatch, capsys, directory, change,
):
    project = tmp_path / "project"
    root = project / directory
    database = root / "library.db"
    if change != "created-root":
        root.mkdir(parents=True)
        database.write_bytes(b"original")
    monkeypatch.setattr(conftest, "PROJECT_ROOT", str(project))
    monkeypatch.setattr(conftest, "_ORIGINAL_SETUP_ROOT", tmp_path / "user-setup")
    monkeypatch.setattr(conftest, "_session_exit_status", 0)
    session = SimpleNamespace(exitstatus=0)
    conftest.pytest_sessionstart(session)

    if change == "modified":
        stat = database.stat()
        database.write_bytes(b"modified")
        os.utime(database, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    elif change == "deleted":
        database.unlink()
    elif change in {"added", "created-root"}:
        root.mkdir(parents=True, exist_ok=True)
        (root / "library.db.lock").write_bytes(b"new")

    before_finish = conftest._setup_file_snapshot(root)
    conftest.pytest_sessionfinish(session, 0)

    assert conftest._setup_file_snapshot(root) == before_finish
    if change == "unchanged":
        assert session.exitstatus == 0
        assert not capsys.readouterr().err
    else:
        assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
        assert conftest._session_exit_status == pytest.ExitCode.TESTS_FAILED
        message = capsys.readouterr().err
        assert str(root) in message
        assert ("library.db.lock" if change in {"added", "created-root"} else "library.db") in message


@pytest.mark.parametrize("change", ["unchanged", "added", "modified", "created-root"])
def test_session_fails_only_when_user_setup_files_are_added_or_modified(
    tmp_path, monkeypatch, capsys, change,
):
    root = tmp_path / "original-setup"
    state = root / "nested" / "runtime-setup.json"
    if change != "created-root":
        state.parent.mkdir(parents=True)
        state.write_text("original", encoding="utf-8")
    monkeypatch.setattr(conftest, "_ORIGINAL_SETUP_ROOT", root)
    monkeypatch.setattr(conftest, "PROJECT_ROOT", str(tmp_path / "project"))
    monkeypatch.setattr(conftest, "_session_exit_status", 0)
    session = SimpleNamespace(exitstatus=0)
    conftest.pytest_sessionstart(session)

    if change == "modified":
        stat = state.stat()
        state.write_text("modified", encoding="utf-8")
        os.utime(state, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    elif change in {"added", "created-root"}:
        root.mkdir(exist_ok=True)
        (root / "backend-instance.json").write_text("new", encoding="utf-8")

    before_finish = conftest._setup_file_snapshot(root)
    conftest.pytest_sessionfinish(session, 0)

    assert conftest._setup_file_snapshot(root) == before_finish
    if change == "unchanged":
        assert session.exitstatus == 0
        assert not capsys.readouterr().err
    else:
        assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
        assert conftest._session_exit_status == pytest.ExitCode.TESTS_FAILED
        message = capsys.readouterr().err
        assert "[conftest] real user setup files added or modified" in message
        assert str(root) in message
        assert (str(state.relative_to(root)) if change == "modified" else "backend-instance.json") in message
