"""Check the assets and shell metadata that packaged desktop icons depend on."""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TAURI = ROOT / "desktop" / "src-tauri"
CONFIG = json.loads((TAURI / "tauri.conf.json").read_text(encoding="utf-8"))
INSTALLER = (ROOT / "desktop" / "windows-installer.iss").read_text(encoding="utf-8")


def test_bundle_has_native_icons_for_each_desktop_platform() -> None:
    icons = [TAURI / name for name in CONFIG["bundle"]["icon"]]
    assert {".ico", ".icns", ".png"} <= {icon.suffix for icon in icons}
    assert all(icon.is_file() and icon.stat().st_size > 0 for icon in icons)


def test_windows_icon_covers_taskbar_and_high_dpi_sizes() -> None:
    icon = next(name for name in CONFIG["bundle"]["icon"] if name.endswith(".ico"))
    data = (TAURI / icon).read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data)
    assert (reserved, kind) == (0, 1)
    sizes = []
    for index in range(count):
        width, height, _, _, _, depth, length, offset = struct.unpack_from(
            "<BBBBHHII", data, 6 + 16 * index
        )
        sizes.append(width or 256)
        assert width == height
        assert depth == 32
        assert length > 0 and offset + length <= len(data)
    assert {16, 24, 32, 48, 64, 256} <= set(sizes)
    # Tauri uses the first frame for its development window icon.
    assert sizes[0] == 32


def test_macos_icon_contains_retina_artwork() -> None:
    icon = next(name for name in CONFIG["bundle"]["icon"] if name.endswith(".icns"))
    data = (TAURI / icon).read_bytes()
    magic, length = struct.unpack_from(">4sI", data)
    assert magic == b"icns" and length == len(data)
    offset = 8
    layers = set()
    while offset < length:
        kind, size = struct.unpack_from(">4sI", data, offset)
        assert size > 8 and offset + size <= length
        layers.add(kind)
        offset += size
    assert offset == length
    assert b"ic10" in layers  # 1024px artwork for Retina Dock/Finder icons.


@pytest.mark.parametrize("location", ["autoprograms", "autodesktop"])
def test_installed_shortcut_has_matching_identity_and_packaged_icon(location: str) -> None:
    shortcut = next(
        line for line in INSTALLER.splitlines() if line.startswith(f'Name: "{{{location}}}')
    )
    fields = dict(re.findall(r'(\w+): "([^"]*)"', shortcut))
    assert fields["AppUserModelID"] == CONFIG["identifier"]
    assert fields["WorkingDir"] == "{app}"
    assert fields["Filename"] == r"{app}\Orpheus.exe"
    icon_name = fields["IconFilename"].removeprefix("{app}\\")
    icon_file = next(
        line for line in INSTALLER.splitlines() if f'DestName: "{icon_name}"' in line
    )
    file_fields = dict(re.findall(r'(\w+): "([^"]*)"', icon_file))
    assert file_fields["DestDir"] == "{app}"
    source = ROOT / "desktop" / Path(file_fields["Source"].replace("\\", "/"))
    assert source.read_bytes() == (TAURI / "icons" / "icon.ico").read_bytes()
    assert f'UninstallDisplayIcon={fields["IconFilename"]}' in INSTALLER
