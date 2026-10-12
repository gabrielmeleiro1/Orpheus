"""Regression coverage for mlx compat."""

import sys

import pytest

from tests._factories_group2_paths import isolated_paths  # noqa: F401  (autouse fixture)


@pytest.mark.parametrize("layout", ["meipass", "meipass-internal", "executable-internal"])
def test_frozen_speech_loader_finds_macos_bundle_resource_layouts(speech_paths, monkeypatch, layout):
    from modules.tts.mlx_compat import patch_espeakng_loader_paths

    root, loader, _, _ = speech_paths
    executable = root / "app/backend"
    meipass = root / "bundle"
    base = {"meipass": meipass, "meipass-internal": meipass / "_internal",
            "executable-internal": executable.parent / "_internal"}[layout]
    package = base / "espeakng_loader"
    data = package / "espeak-ng-data"
    data.mkdir(parents=True)
    (data / "phontab").write_bytes(b"phonemes")
    (package / "libespeak-ng.dylib").write_bytes(b"library")
    monkeypatch.setattr(loader, "get_data_path", lambda: str(root / "absent"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(sys, "platform", "darwin")

    patch_espeakng_loader_paths()

    assert loader.get_library_path() == str(package / "libespeak-ng.dylib")
    assert loader.get_data_path() == str(data)
