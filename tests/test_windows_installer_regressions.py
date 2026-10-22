"""Regression coverage for windows installer."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def _installer_sections():
    sections = {}
    current = None
    for raw in (ROOT / "desktop/windows-installer.iss").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections[current] = []
        elif current and line and not line.startswith(";"):
            sections[current].append(line)
    return sections


def test_windows_install_and_uninstall_allowlists_exclude_persistent_data():
    sections = _installer_sections()
    files = [dict(re.findall(r'(\w+):\s*"([^"]*)"', line)) for line in sections["Files"]]
    assert files == [
        {"Source": r"src-tauri\target\release\orpheus_desktop.exe",
         "DestDir": "{app}", "DestName": "Orpheus.exe.new"},
        {"Source": r"src-tauri\icons\icon.ico", "DestDir": "{app}", "DestName": "Orpheus.ico"},
        {"Source": r"src-tauri\binaries\orpheus-desktop-backend\*",
         "DestDir": r"{app}\binaries\orpheus-desktop-backend.new"},
        {"Source": r"..\LICENSE", "DestDir": "{app}"},
        {"Source": r"..\THIRD_PARTY_LICENSES.md", "DestDir": "{app}"},
    ]
    assert [line.split("Flags: ", 1)[1] for line in sections["Files"]] == [
        "ignoreversion", "ignoreversion", "ignoreversion recursesubdirs createallsubdirs",
        "ignoreversion", "ignoreversion",
    ]
    assert sections["UninstallDelete"] == [
        r'Type: files; Name: "{app}\Orpheus.exe"',
        r'Type: files; Name: "{app}\Orpheus.exe.new"',
        r'Type: files; Name: "{app}\Orpheus.exe.old"',
        r'Type: files; Name: "{app}\.orpheus-install-pending"',
        r'Type: filesandordirs; Name: "{app}\binaries\orpheus-desktop-backend"',
        r'Type: filesandordirs; Name: "{app}\binaries\orpheus-desktop-backend.new"',
        r'Type: filesandordirs; Name: "{app}\binaries\orpheus-desktop-backend.old"',
    ]
    assert "InstallDelete" not in sections
    assert "DefaultDirName={localappdata}\\Orpheus" in sections["Setup"]
    # These allowlists leave setup state, library data, output, and model caches unowned.
    for retained in ("runtime-setup.json", "audiobooks.db", "output", "huggingface"):
        assert all(retained not in line.lower()
                   for name in ("Files", "UninstallDelete") for line in sections[name])


def test_windows_installers_share_version_independent_publication_mutexes():
    settings = dict(line.split("=", 1) for line in _installer_sections()["Setup"])
    # AppId identifies uninstall records; SetupMutex protects the fixed .new/.old
    # backend trees from concurrent installers, including different app versions.
    assert settings["SetupMutex"].split(",") == [
        "OrpheusSetupMutex", r"Global\OrpheusSetupMutex",
    ]
