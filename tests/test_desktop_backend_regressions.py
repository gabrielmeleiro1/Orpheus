"""Regression coverage for desktop backend."""

from pathlib import Path
import json
import os
import sqlite3
import sys

import pytest

from tests._factories_group2_paths import isolated_paths  # noqa: F401  (autouse fixture)


@pytest.mark.parametrize("platform_name,path,safe", [
    ("darwin", "a" * 159, True), ("darwin", "a" * 160, False),
    ("win32", "a" * 159, True), ("win32", "a" * 160, False),
    ("darwin", "é" * 79, True), ("darwin", "é" * 80, False),
    ("win32", "é", False), ("linux", "é", True),
])
def test_speech_path_limits_use_filesystem_bytes_and_windows_ascii(backend, platform_name, path, safe):
    assert backend._is_native_safe_espeak_path(Path(path), platform=platform_name) is safe


@pytest.mark.parametrize("frozen", [False, True], ids=["source", "frozen"])
@pytest.mark.parametrize("platform_name", ["darwin", "win32"])
@pytest.mark.parametrize("unsafe", [False, True], ids=["short", "long"])
def test_speech_configuration_preserves_safe_source_and_retargets_frozen_or_long_paths(
    backend, speech_paths, monkeypatch, frozen, platform_name, unsafe,
):
    root, loader, wrapper, library = speech_paths
    source = root / ("nested" * 28 if unsafe else "short") / "espeak-ng-data"
    source.mkdir(parents=True)
    (source / "phontab").write_bytes(b"phonemes")
    (source / "voices").mkdir()
    (source / "voices/en").write_bytes(b"voice")
    monkeypatch.setattr(loader, "get_data_path", lambda: str(source))
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(sys, "platform", platform_name)

    backend.configure_espeak_runtime()

    resolved = Path(loader.get_data_path())
    if not frozen and not unsafe:
        assert resolved == source
        assert "ESPEAK_DATA_PATH" not in os.environ
        assert wrapper._ESPEAK_LIBRARY is None
        return
    assert backend._is_native_safe_espeak_path(resolved)
    assert resolved.is_relative_to(root)
    assert not resolved.is_symlink()
    assert (resolved / "phontab").read_bytes() == b"phonemes"
    assert (resolved / "voices/en").read_bytes() == b"voice"
    assert (source / "phontab").read_bytes() == b"phonemes"
    assert (resolved != source) is unsafe
    assert os.environ["ESPEAK_DATA_PATH"] == str(resolved)
    assert os.environ["PHONEMIZER_ESPEAK_DATA_PATH"] == str(resolved)
    assert os.environ["PHONEMIZER_ESPEAK_LIBRARY"] == str(library)
    assert wrapper._ESPEAK_LIBRARY == str(library)
    assert wrapper._ESPEAK_DATA_PATH == str(resolved)
    backend.configure_espeak_runtime()
    assert Path(loader.get_data_path()) == resolved


@pytest.mark.parametrize("platform_name,relocated", [("darwin", False), ("win32", True)])
def test_short_unicode_speech_paths_are_copied_only_on_windows(
    backend, speech_paths, monkeypatch, platform_name, relocated,
):
    root, _, _, _ = speech_paths
    source = root / "voix-é" / "espeak-ng-data"
    source.mkdir(parents=True)
    (source / "phontab").write_bytes(b"phonemes")
    monkeypatch.setattr(sys, "platform", platform_name)
    result = backend._ensure_short_espeak_data_path(source)
    assert (result != source) is relocated
    assert result.is_relative_to(root)
    assert (result / "phontab").read_bytes() == b"phonemes"
    assert backend._is_native_safe_espeak_path(result)


@pytest.mark.parametrize("profile", ["base", "mlx", "cuda"])
def test_frozen_runtime_identity_comes_from_marker_beside_executable(
    backend, tmp_path, monkeypatch, profile,
):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "backend"))
    monkeypatch.setenv("ORPHEUS_RUNTIME_PROFILE", "invalid")
    (tmp_path / "runtime-profile.json").write_text(
        json.dumps({"profile": f" {profile.upper()} "}), encoding="utf-8",
    )
    assert backend._runtime_profile_identity() == profile


@pytest.mark.parametrize("payload", [None, "{", "[]", '{"profile":"unknown"}'])
def test_frozen_runtime_requires_valid_marker_even_with_environment_profile(
    backend, tmp_path, monkeypatch, payload,
):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "backend"))
    monkeypatch.setenv("ORPHEUS_RUNTIME_PROFILE", "base")
    if payload is not None:
        (tmp_path / "runtime-profile.json").write_text(payload, encoding="utf-8")
    with pytest.raises(RuntimeError, match="runtime profile marker"):
        backend._runtime_profile_identity()


def test_source_runtime_identity_defaults_to_development_and_accepts_explicit_profile(backend, monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert backend._runtime_profile_identity() == "development"
    monkeypatch.setenv("ORPHEUS_RUNTIME_PROFILE", " CUDA ")
    assert backend._runtime_profile_identity() == "cuda"


@pytest.mark.parametrize("reference", ["absolute", "relative", "symlink"])
def test_library_lock_uses_canonical_database_path_independently_of_data_directory(
    backend, tmp_path, monkeypatch, capsys, reference,
):
    database = tmp_path / "library.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE books (id INTEGER PRIMARY KEY)")
    selected = database
    if reference == "relative":
        selected = Path("library.sqlite")
    elif reference == "symlink":
        selected = tmp_path / "alias.sqlite"
        try:
            selected.symlink_to(database)
        except OSError as exc:
            if os.name != "nt":
                raise
            pytest.skip(f"Symlinks unavailable: {exc}")
    monkeypatch.setenv("AUDIOBOOK_DB_PATH", str(selected))
    monkeypatch.setenv("AUDIOBOOK_DATA_DIR", str(tmp_path / "unrelated-data"))
    monkeypatch.setattr(backend, "configure_espeak_runtime", lambda: None)
    monkeypatch.setattr(backend.multiprocessing, "freeze_support", lambda: None)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    lock = Path(f"{os.path.normcase(str(database.resolve()))}.lock")

    with backend._open_process_lock(lock, blocking=False):
        assert backend.main([]) == 1
        assert "already running with this library" in capsys.readouterr().err

    assert lock.is_file()
    assert not (tmp_path / "unrelated-data/audiobooks.db.lock").exists()
    with backend._open_process_lock(lock, blocking=False) as reopened:
        assert not reopened.closed
