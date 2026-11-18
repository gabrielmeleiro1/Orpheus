from pathlib import Path
import os
from types import SimpleNamespace

import pytest

from modules.storage import permissions


def _posix(monkeypatch, mode: int):
    changes = []
    platform = SimpleNamespace(
        name="posix",
        stat=lambda path: SimpleNamespace(st_mode=0o100000 | mode),
        chmod=lambda path, new_mode: changes.append(new_mode),
    )
    monkeypatch.setattr(permissions, "os", platform)
    return changes


@pytest.mark.parametrize(
    ("mode", "expected"),
    [(0o644, 0o600), (0o666, 0o600), (0o755, 0o600), (0o444, 0o400), (0o400, 0o400), (0o000, 0o000)],
)
def test_owner_only_hides_a_file_without_granting_the_owner_access(monkeypatch, mode, expected):
    changes = _posix(monkeypatch, mode)
    permissions.owner_only(Path("library.db"))
    assert changes == [expected]


def test_owner_only_keeps_directories_usable_by_the_owner(monkeypatch):
    changes = _posix(monkeypatch, 0o555)
    permissions.owner_only(Path("data"), directory=True)
    assert changes == [0o700]


def test_owner_only_ignores_a_missing_file(monkeypatch):
    def missing(path):
        raise FileNotFoundError(path)

    monkeypatch.setattr(permissions, "os", SimpleNamespace(name="posix", stat=missing, chmod=os.chmod))
    permissions.owner_only(Path("not-created-yet.db"))


def test_owner_only_leaves_windows_attributes_alone(monkeypatch):
    changes = []
    monkeypatch.setattr(permissions, "os", SimpleNamespace(name="nt", chmod=lambda *args: changes.append(args)))
    permissions.owner_only(Path("library.db"))
    assert changes == []
