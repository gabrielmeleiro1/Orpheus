"""Tests for packaging the desktop backend sidecar for Tauri."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts import build_desktop_backend


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _write_executable(path: Path, content: str = "executable") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _write_required_pyinstaller_resources(onedir: Path) -> None:
    internal_dir = onedir / "_internal"
    (internal_dir / "mlx" / "lib").mkdir(parents=True)
    (internal_dir / "mlx" / "lib" / "mlx.metallib").write_text("metal", encoding="utf-8")
    (internal_dir / "espeakng_loader" / "espeak-ng-data").mkdir(parents=True)
    (internal_dir / "espeakng_loader" / "espeak-ng-data" / "phontab").write_text(
        "phoneme table",
        encoding="utf-8",
    )


def test_copy_sidecar_for_tauri_copies_onedir_resource_and_removes_legacy_binary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    dist_dir = tmp_path / "dist"
    binaries_dir = tmp_path / "binaries"
    onedir = dist_dir / "orpheus-desktop-backend"
    internal_dir = onedir / "_internal"
    executable = onedir / "orpheus-desktop-backend"
    legacy_external_bin = binaries_dir / "orpheus-desktop-backend-aarch64-apple-darwin"
    other_legacy_external_bin = binaries_dir / "orpheus-desktop-backend-x86_64-apple-darwin"
    ffmpeg_source = _write_executable(tmp_path / "tools" / "ffmpeg", "ffmpeg source")
    ffprobe_source = _write_executable(tmp_path / "tools" / "ffprobe", "ffprobe source")

    internal_dir.mkdir(parents=True)
    executable.write_text("backend executable", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    (internal_dir / "dependency.txt").write_text("dependency", encoding="utf-8")
    (internal_dir / "mlx" / "lib").mkdir(parents=True)
    (internal_dir / "mlx" / "lib" / "mlx.metallib").write_text("metal", encoding="utf-8")
    (internal_dir / "espeakng_loader" / "espeak-ng-data").mkdir(parents=True)
    (internal_dir / "espeakng_loader" / "espeak-ng-data" / "phontab").write_text(
        "phoneme table",
        encoding="utf-8",
    )
    binaries_dir.mkdir(parents=True)
    legacy_external_bin.write_text("old onefile binary", encoding="utf-8")
    other_legacy_external_bin.write_text("old x86 onefile binary", encoding="utf-8")

    monkeypatch.setattr(build_desktop_backend, "DIST_DIR", dist_dir)
    monkeypatch.setattr(build_desktop_backend, "TAURI_BINARIES_DIR", binaries_dir)
    monkeypatch.setattr(
        build_desktop_backend,
        "TAURI_SIDECAR_RESOURCE_DIR",
        binaries_dir / "orpheus-desktop-backend",
    )

    copied_executable = build_desktop_backend._copy_sidecar_for_tauri(  # noqa: SLF001
        "aarch64-apple-darwin",
        ffmpeg_binaries={"ffmpeg": ffmpeg_source, "ffprobe": ffprobe_source},
    )

    assert copied_executable == binaries_dir / "orpheus-desktop-backend" / "orpheus-desktop-backend"
    assert copied_executable.read_text(encoding="utf-8") == "backend executable"
    assert (copied_executable.parent / "_internal" / "dependency.txt").read_text(
        encoding="utf-8"
    ) == "dependency"
    assert copied_executable.stat().st_mode & stat.S_IXUSR
    assert (copied_executable.parent / "bin" / "ffmpeg").read_text(encoding="utf-8") == "ffmpeg source"
    assert (copied_executable.parent / "bin" / "ffprobe").read_text(encoding="utf-8") == "ffprobe source"
    assert (copied_executable.parent / "bin" / "ffmpeg").stat().st_mode & stat.S_IXUSR
    assert (copied_executable.parent / "bin" / "ffprobe").stat().st_mode & stat.S_IXUSR
    assert not legacy_external_bin.exists()
    assert not other_legacy_external_bin.exists()


def test_copy_sidecar_for_tauri_requires_mlx_metal_library(
    tmp_path: Path,
    monkeypatch,
) -> None:
    dist_dir = tmp_path / "dist"
    binaries_dir = tmp_path / "binaries"
    onedir = dist_dir / "orpheus-desktop-backend"
    internal_dir = onedir / "_internal"
    executable = onedir / "orpheus-desktop-backend"
    ffmpeg_source = _write_executable(tmp_path / "tools" / "ffmpeg")
    ffprobe_source = _write_executable(tmp_path / "tools" / "ffprobe")

    internal_dir.mkdir(parents=True)
    executable.write_text("backend executable", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)

    monkeypatch.setattr(build_desktop_backend, "DIST_DIR", dist_dir)
    monkeypatch.setattr(build_desktop_backend, "TAURI_BINARIES_DIR", binaries_dir)
    monkeypatch.setattr(
        build_desktop_backend,
        "TAURI_SIDECAR_RESOURCE_DIR",
        binaries_dir / "orpheus-desktop-backend",
    )

    with pytest.raises(RuntimeError, match="mlx/lib/mlx\\.metallib"):
        build_desktop_backend._copy_sidecar_for_tauri(  # noqa: SLF001
            "aarch64-apple-darwin",
            ffmpeg_binaries={"ffmpeg": ffmpeg_source, "ffprobe": ffprobe_source},
        )


def test_copy_sidecar_for_tauri_copies_resolved_ffmpeg_binaries_under_bin(
    tmp_path: Path,
    monkeypatch,
) -> None:
    dist_dir = tmp_path / "dist"
    binaries_dir = tmp_path / "binaries"
    onedir = dist_dir / "orpheus-desktop-backend"
    executable = _write_executable(onedir / "orpheus-desktop-backend", "backend")
    ffmpeg_source = _write_executable(tmp_path / "source" / "ffmpeg", "ffmpeg binary")
    ffprobe_source = _write_executable(tmp_path / "source" / "ffprobe", "ffprobe binary")
    _ = executable
    _write_required_pyinstaller_resources(onedir)

    monkeypatch.setattr(build_desktop_backend, "DIST_DIR", dist_dir)
    monkeypatch.setattr(build_desktop_backend, "TAURI_BINARIES_DIR", binaries_dir)
    monkeypatch.setattr(
        build_desktop_backend,
        "TAURI_SIDECAR_RESOURCE_DIR",
        binaries_dir / "orpheus-desktop-backend",
    )

    copied_executable = build_desktop_backend._copy_sidecar_for_tauri(  # noqa: SLF001
        "aarch64-apple-darwin",
        ffmpeg_binaries={"ffmpeg": ffmpeg_source, "ffprobe": ffprobe_source},
    )

    copied_bin = copied_executable.parent / "bin"
    assert (copied_bin / "ffmpeg").read_text(encoding="utf-8") == "ffmpeg binary"
    assert (copied_bin / "ffprobe").read_text(encoding="utf-8") == "ffprobe binary"
    assert (copied_bin / "ffmpeg").stat().st_mode & stat.S_IXUSR
    assert (copied_bin / "ffprobe").stat().st_mode & stat.S_IXUSR


def test_validate_sidecar_resources_requires_packaged_ffmpeg_and_ffprobe(tmp_path: Path) -> None:
    sidecar_dir = tmp_path / "sidecar"
    _write_required_pyinstaller_resources(sidecar_dir)

    with pytest.raises(RuntimeError) as exc_info:
        build_desktop_backend._validate_sidecar_resources(sidecar_dir)  # noqa: SLF001

    message = str(exc_info.value)
    assert "bin/ffmpeg" in message
    assert "bin/ffprobe" in message


def test_validate_runtime_dependencies_returns_resolved_ffmpeg_binary_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ffmpeg = _write_executable(tmp_path / "tools" / "ffmpeg")
    ffprobe = _write_executable(tmp_path / "tools" / "ffprobe")

    monkeypatch.setattr(build_desktop_backend, "REQUIRED_MODULES", ())
    monkeypatch.setattr(
        build_desktop_backend.subprocess,
        "run",
        lambda *_args, **_kwargs: MagicMock(returncode=0, stdout="6.0", stderr=""),
    )
    monkeypatch.setattr(
        build_desktop_backend,
        "resolve_ffmpeg_binary",
        lambda name: str({"ffmpeg": ffmpeg, "ffprobe": ffprobe}[name]),
    )

    resolved = build_desktop_backend._validate_runtime_dependencies("python")  # noqa: SLF001

    assert resolved == {"ffmpeg": ffmpeg, "ffprobe": ffprobe}


def test_normalize_mlx_dylib_layout_removes_root_duplicate_and_retargets_core(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sidecar_dir = tmp_path / "sidecar"
    core_extension = sidecar_dir / "_internal" / "mlx" / "core.cpython-312-darwin.so"
    nested_dylib = sidecar_dir / "_internal" / "mlx" / "lib" / "libmlx.dylib"
    root_dylib = sidecar_dir / "_internal" / "libmlx.dylib"
    core_extension.parent.mkdir(parents=True)
    nested_dylib.parent.mkdir(parents=True)
    core_extension.write_bytes(b"core")
    nested_dylib.write_bytes(b"nested")
    root_dylib.write_bytes(b"root duplicate")
    commands: list[list[str]] = []

    monkeypatch.setattr(build_desktop_backend.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        build_desktop_backend,
        "_macho_rpaths",
        lambda _path: ["@loader_path/.."],
    )

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        commands.append([str(part) for part in command])
        return MagicMock(returncode=0)

    monkeypatch.setattr(build_desktop_backend.subprocess, "run", fake_run)

    build_desktop_backend._normalize_mlx_dylib_layout(sidecar_dir)  # noqa: SLF001

    assert not root_dylib.exists()
    assert [
        "install_name_tool",
        "-delete_rpath",
        "@loader_path/..",
        str(core_extension),
    ] in commands
    assert [
        "install_name_tool",
        "-add_rpath",
        "@loader_path/lib",
        str(core_extension),
    ] in commands
    assert ["codesign", "--force", "--sign", "-", str(core_extension)] in commands


def test_expose_espeak_data_at_sidecar_root_links_files_and_copies_dirs(tmp_path: Path) -> None:
    sidecar_dir = tmp_path / "sidecar"
    data_dir = sidecar_dir / "_internal" / "espeakng_loader" / "espeak-ng-data"
    data_dir.mkdir(parents=True)
    (data_dir / "phontab").write_text("phoneme table", encoding="utf-8")
    (data_dir / "voices").mkdir()
    (data_dir / "voices" / "en").write_text("english voices", encoding="utf-8")

    build_desktop_backend._expose_espeak_data_at_sidecar_root(sidecar_dir)  # noqa: SLF001

    phontab_link = sidecar_dir / "phontab"
    voices_dir = sidecar_dir / "voices"
    assert phontab_link.is_symlink()
    assert voices_dir.is_dir()
    assert not voices_dir.is_symlink()
    assert phontab_link.read_text(encoding="utf-8") == "phoneme table"
    assert (voices_dir / "en").read_text(encoding="utf-8") == "english voices"
    assert phontab_link.readlink() == Path("_internal") / "espeakng_loader" / "espeak-ng-data" / "phontab"


def test_backend_build_prerequisites_include_mlx_runtime_imports() -> None:
    assert "jinja2" not in build_desktop_backend.REQUIRED_MODULES
    assert {"en_core_web_sm", "scipy", "spacy", "thinc", "torch", "transformers"}.issubset(
        set(build_desktop_backend.REQUIRED_MODULES),
    )


def test_build_script_help_runs_from_desktop_working_directory_without_pythonpath() -> None:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    completed = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "build_desktop_backend.py"), "--help"],
        cwd=PROJECT_ROOT / "desktop",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Build the desktop backend sidecar executable" in completed.stdout
