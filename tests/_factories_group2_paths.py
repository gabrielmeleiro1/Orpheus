"""Shared fixtures for paths regressions."""

from pathlib import Path
import os
import sys
import tempfile

import pytest


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    for key, relative in (
        ("HF_HOME", "huggingface"), ("HF_HUB_CACHE", "huggingface/hub"),
        ("HUGGINGFACE_HUB_CACHE", "huggingface/hub"),
        ("PROGRAMDATA", "ProgramData"), ("XDG_CACHE_HOME", "cache"),
        ("AUDIOBOOK_DATA_DIR", "data"), ("AUDIOBOOK_OUTPUT_DIR", "output"),
        ("AUDIOBOOK_TEMP_DIR", "temp"), ("AUDIOBOOK_DB_PATH", "data/audiobooks.db"),
    ):
        monkeypatch.setenv(key, str(tmp_path / relative))
    monkeypatch.delenv("ORPHEUS_RUNTIME_PROFILE", raising=False)
    for key in ("ESPEAK_DATA_PATH", "PHONEMIZER_ESPEAK_DATA_PATH", "PHONEMIZER_ESPEAK_LIBRARY"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def backend():
    from apps.launchers import desktop_backend

    return desktop_backend


@pytest.fixture
def speech_paths(monkeypatch, backend):
    loader = pytest.importorskip("espeakng_loader")
    wrapper = pytest.importorskip("phonemizer.backend.espeak.wrapper").EspeakWrapper
    with tempfile.TemporaryDirectory(prefix="op-") as directory:
        root = Path(directory).resolve()
        monkeypatch.setattr(backend.tempfile, "gettempdir", lambda: str(root))
        monkeypatch.setattr(backend, "_espeak_runtime_base", lambda: root / "cache")
        monkeypatch.setenv("PROGRAMDATA", str(root / "ProgramData"))
        monkeypatch.setattr(wrapper, "_ESPEAK_LIBRARY", None)
        monkeypatch.setattr(wrapper, "_ESPEAK_DATA_PATH", None)
        monkeypatch.setattr(sys, "_MEIPASS", str(root / "bundle"), raising=False)
        library = root / "libespeak-ng.dylib"
        library.write_bytes(b"library")
        # Restore the installed package getters after the launcher retargets them.
        monkeypatch.setattr(loader, "get_library_path", lambda: str(library))
        monkeypatch.setattr(loader, "get_data_path", loader.get_data_path)
        yield root, loader, wrapper, library
