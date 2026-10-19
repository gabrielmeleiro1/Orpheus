"""Windows shell spelling must not alter the application's stored paths."""

from pathlib import PureWindowsPath
from types import SimpleNamespace

import pytest

from modules.app.services import library_service


@pytest.mark.parametrize("raw, expected", [
    (r"\\?\C:\audit\output", r"C:\audit\output"),
    (r"\\?\UNC\example.invalid\audit\output", r"\\example.invalid\audit\output"),
    (r"C:\audit\output", r"C:\audit\output"),
    (r"\\example.invalid\audit\output", r"\\example.invalid\audit\output"),
    (r"\\?\Volume{synthetic}\output", r"\\?\Volume{synthetic}\output"),
])
def test_folder_opener_translates_only_verbatim_drive_or_unc_prefix(monkeypatch, raw, expected):
    opened = []
    monkeypatch.setattr(library_service, "os", SimpleNamespace(
        name="nt", startfile=opened.append, path=SimpleNamespace(isdir=lambda _path: True),
    ))
    folder = PureWindowsPath(raw)
    library_service._default_folder_opener(folder)
    assert opened == [expected]
    assert str(folder) == raw
