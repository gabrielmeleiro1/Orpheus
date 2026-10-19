"""A library that cannot be opened fails startup with its cause, not a crash loop."""

from __future__ import annotations

import os
import sqlite3
import stat
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from modules.storage.migrations import FutureSchemaError
from modules.storage.sqlite_repo import LibraryReadOnlyError, SQLiteRepository
from modules.web.server import create_app


@pytest.fixture
def read_only_library(tmp_path):
    path = tmp_path / "data" / "library.db"
    path.parent.mkdir()
    SQLiteRepository(path).close()
    before = sorted(entry.name for entry in path.parent.iterdir())
    os.chmod(path, stat.S_IREAD)
    try:
        yield path, before
    finally:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)


def test_read_only_library_raises_a_typed_actionable_error(read_only_library) -> None:
    path, before = read_only_library

    for _attempt in range(2):
        with pytest.raises(LibraryReadOnlyError, match="read-only") as error:
            SQLiteRepository(path)
        assert isinstance(error.value.__cause__, sqlite3.OperationalError)

    # Nothing was quarantined or recreated, so a retry sees the same library.
    after = sorted(entry.name for entry in path.parent.iterdir())
    assert not [name for name in after if ".corrupt-" in name or "quarantine" in name]
    assert "library.db" in after and before


def test_startup_records_the_library_open_failure() -> None:
    def broken_controller():
        raise FutureSchemaError("Please update Orpheus to open it.")

    app = create_app(controller_factory=broken_controller)

    with pytest.raises(FutureSchemaError):
        with TestClient(app):
            pass

    assert str(app.state.startup_error) == "Please update Orpheus to open it."


def test_launcher_ends_stderr_with_the_startup_cause(monkeypatch, capsys) -> None:
    from apps.launchers import desktop_backend

    monkeypatch.setattr(desktop_backend, "configure_espeak_runtime", Mock())
    monkeypatch.setattr(desktop_backend.multiprocessing, "freeze_support", Mock())
    monkeypatch.setattr(desktop_backend, "_runtime_profile_identity", lambda: "base")
    app = SimpleNamespace(state=SimpleNamespace(startup_error=FutureSchemaError("Please update Orpheus to open it.")))
    monkeypatch.setattr(desktop_backend, "create_app", Mock(return_value=app))
    monkeypatch.setattr(desktop_backend, "_build_desktop_server", Mock(return_value=Mock(ready_emitted=False)))
    monkeypatch.setattr(desktop_backend, "_start_desktop_parent_watchdog", Mock(return_value=None))

    assert desktop_backend.main([]) == 1
    last_line = capsys.readouterr().err.strip().splitlines()[-1]
    assert last_line == "failed to start desktop backend: Please update Orpheus to open it."


def test_launcher_leaves_the_named_cause_prefix_off_an_unnamed_failure(monkeypatch, capsys) -> None:
    from apps.launchers import desktop_backend

    monkeypatch.setattr(desktop_backend, "configure_espeak_runtime", Mock())
    monkeypatch.setattr(desktop_backend.multiprocessing, "freeze_support", Mock())
    monkeypatch.setattr(desktop_backend, "_runtime_profile_identity", lambda: "base")
    app = SimpleNamespace(state=SimpleNamespace(startup_error=None))
    monkeypatch.setattr(desktop_backend, "create_app", Mock(return_value=app))
    monkeypatch.setattr(desktop_backend, "_build_desktop_server", Mock(return_value=Mock(ready_emitted=False)))
    monkeypatch.setattr(desktop_backend, "_start_desktop_parent_watchdog", Mock(return_value=None))

    assert desktop_backend.main([]) == 1
    last_line = capsys.readouterr().err.strip().splitlines()[-1]
    # The shell shows a prefixed line alone and drops the stderr tail above it.
    assert not last_line.startswith("failed to start desktop backend:")
