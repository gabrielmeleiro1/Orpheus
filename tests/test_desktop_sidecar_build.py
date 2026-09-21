"""Tests for packaging the desktop backend sidecar for Tauri."""

from __future__ import annotations

import json
import os
import platform
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from scripts import build_desktop_backend


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_tauri_bundle_declares_project_and_third_party_license_resources() -> None:
    config = json.loads(
        (PROJECT_ROOT / "desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8")
    )

    resources = config["bundle"]["resources"]
    assert resources["../../LICENSE"] == "LICENSE"
    assert resources["../../THIRD_PARTY_LICENSES.md"] == "THIRD_PARTY_LICENSES.md"


def _write_executable(path: Path, content: str = "executable") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _write_requirements_manifests(onedir: Path) -> None:
    manifest_dir = onedir / "_internal" / "requirements"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    for name in ("base", "mlx", "cuda"):
        (manifest_dir / f"{name}.txt").write_text("packaging\n", encoding="utf-8")


def _write_required_pyinstaller_resources(
    onedir: Path,
    *,
    runtime_profile: str = "mlx",
) -> None:
    internal_dir = onedir / "_internal"
    (internal_dir / "mlx" / "lib").mkdir(parents=True)
    (internal_dir / "mlx" / "lib" / "mlx.metallib").write_text("metal", encoding="utf-8")
    (internal_dir / "mlx" / "lib" / "libmlx.dylib").write_bytes(b"libmlx stub")
    (internal_dir / "mlx" / "lib" / "libjaccl.dylib").write_bytes(b"libjaccl stub")
    (internal_dir / "espeakng_loader" / "espeak-ng-data").mkdir(parents=True)
    (internal_dir / "espeakng_loader" / "espeak-ng-data" / "phontab").write_text(
        "phoneme table",
        encoding="utf-8",
    )
    _write_requirements_manifests(onedir)
    (onedir / "runtime-profile.json").write_text(
        json.dumps({"profile": runtime_profile}),
        encoding="utf-8",
    )


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Requires macOS sidecar packaging tools and Mach-O dependency handling",
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
    legacy_external_dir = binaries_dir / "orpheus-desktop-backend-amd64-unknown-linux-gnu"
    ffmpeg_source = _write_executable(tmp_path / "tools" / "ffmpeg", "ffmpeg source")
    ffprobe_source = _write_executable(tmp_path / "tools" / "ffprobe", "ffprobe source")

    internal_dir.mkdir(parents=True)
    executable.write_text("backend executable", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    (internal_dir / "dependency.txt").write_text("dependency", encoding="utf-8")
    (internal_dir / "mlx" / "lib").mkdir(parents=True)
    (internal_dir / "mlx" / "lib" / "mlx.metallib").write_text("metal", encoding="utf-8")
    (internal_dir / "mlx" / "lib" / "libmlx.dylib").write_bytes(b"libmlx stub")
    (internal_dir / "espeakng_loader" / "espeak-ng-data").mkdir(parents=True)
    (internal_dir / "espeakng_loader" / "espeak-ng-data" / "phontab").write_text(
        "phoneme table",
        encoding="utf-8",
    )
    _write_requirements_manifests(onedir)
    binaries_dir.mkdir(parents=True)
    legacy_external_bin.write_text("old onefile binary", encoding="utf-8")
    other_legacy_external_bin.write_text("old x86 onefile binary", encoding="utf-8")
    (legacy_external_dir / "_internal").mkdir(parents=True)
    (legacy_external_dir / "_internal" / "stale.txt").write_text("stale", encoding="utf-8")

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
    if os.name != "nt":
        assert copied_executable.stat().st_mode & stat.S_IXUSR
    assert (copied_executable.parent / "bin" / "ffmpeg").read_text(
        encoding="utf-8"
    ) == "ffmpeg source"
    assert (copied_executable.parent / "bin" / "ffprobe").read_text(
        encoding="utf-8"
    ) == "ffprobe source"
    assert json.loads(
        (copied_executable.parent / "runtime-profile.json").read_text(encoding="utf-8")
    ) == {"profile": "mlx"}
    if os.name != "nt":
        assert (copied_executable.parent / "bin" / "ffmpeg").stat().st_mode & stat.S_IXUSR
        assert (copied_executable.parent / "bin" / "ffprobe").stat().st_mode & stat.S_IXUSR
    assert not legacy_external_bin.exists()
    assert not other_legacy_external_bin.exists()
    assert not legacy_external_dir.exists()


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

    with pytest.raises(RuntimeError, match="mlx/lib/(mlx\\.metallib|libmlx\\.dylib)"):
        build_desktop_backend._copy_sidecar_for_tauri(  # noqa: SLF001
            "aarch64-apple-darwin",
            ffmpeg_binaries={"ffmpeg": ffmpeg_source, "ffprobe": ffprobe_source},
        )


def test_copy_sidecar_for_tauri_windows_does_not_require_mlx_resources(
    tmp_path: Path,
    monkeypatch,
) -> None:
    dist_dir = tmp_path / "dist"
    binaries_dir = tmp_path / "binaries"
    onedir = dist_dir / "orpheus-desktop-backend"
    executable = _write_executable(onedir / "orpheus-desktop-backend.exe", "backend exe")
    ffmpeg_source = _write_executable(tmp_path / "tools" / "ffmpeg.exe", "ffmpeg exe")
    ffprobe_source = _write_executable(tmp_path / "tools" / "ffprobe.exe", "ffprobe exe")
    _ = executable
    (onedir / "_internal" / "espeakng_loader" / "espeak-ng-data").mkdir(parents=True)
    (onedir / "_internal" / "espeakng_loader" / "espeak-ng-data" / "phontab").write_text(
        "phoneme table",
        encoding="utf-8",
    )
    _write_requirements_manifests(onedir)

    monkeypatch.setattr(build_desktop_backend, "DIST_DIR", dist_dir)
    monkeypatch.setattr(build_desktop_backend, "TAURI_BINARIES_DIR", binaries_dir)
    monkeypatch.setattr(
        build_desktop_backend,
        "TAURI_SIDECAR_RESOURCE_DIR",
        binaries_dir / "orpheus-desktop-backend",
    )

    copied_executable = build_desktop_backend._copy_sidecar_for_tauri(  # noqa: SLF001
        "x86_64-pc-windows-msvc",
        ffmpeg_binaries={"ffmpeg": ffmpeg_source, "ffprobe": ffprobe_source},
    )

    assert (
        copied_executable
        == binaries_dir / "orpheus-desktop-backend" / "orpheus-desktop-backend.exe"
    )
    assert (copied_executable.parent / "bin" / "ffmpeg.exe").read_text(
        encoding="utf-8"
    ) == "ffmpeg exe"
    assert (copied_executable.parent / "bin" / "ffprobe.exe").read_text(
        encoding="utf-8"
    ) == "ffprobe exe"


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Requires macOS sidecar packaging tools and Mach-O dependency handling",
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
    if os.name != "nt":
        assert (copied_bin / "ffmpeg").stat().st_mode & stat.S_IXUSR
        assert (copied_bin / "ffprobe").stat().st_mode & stat.S_IXUSR


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are the packaging failure mode")
def test_copy_ffmpeg_binaries_adds_owner_write_when_source_is_readonly_executable(
    tmp_path: Path,
) -> None:
    """Tauri resource processing needs owner-write on bundled Mach-O binaries."""
    readonly_mode = (
        stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
    )  # 0o555
    ffmpeg_source = tmp_path / "source" / "ffmpeg"
    ffprobe_source = tmp_path / "source" / "ffprobe"
    for path, content in ((ffmpeg_source, "ffmpeg"), (ffprobe_source, "ffprobe")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(readonly_mode)

    sidecar_dir = tmp_path / "sidecar"
    build_desktop_backend._copy_ffmpeg_binaries(  # noqa: SLF001
        sidecar_dir,
        {"ffmpeg": ffmpeg_source, "ffprobe": ffprobe_source},
        target_triple="aarch64-apple-darwin",
    )

    for name in ("ffmpeg", "ffprobe"):
        source = tmp_path / "source" / name
        destination = sidecar_dir / "bin" / name
        source_mode = source.stat().st_mode
        destination_mode = destination.stat().st_mode

        assert source_mode & readonly_mode == readonly_mode
        assert not (source_mode & stat.S_IWUSR)
        assert destination.read_text(encoding="utf-8") == name
        assert destination_mode & stat.S_IRUSR
        assert destination_mode & stat.S_IWUSR
        assert destination_mode & stat.S_IXUSR
        assert destination_mode & stat.S_IRGRP
        assert destination_mode & stat.S_IXGRP
        assert destination_mode & stat.S_IROTH
        assert destination_mode & stat.S_IXOTH


def test_validate_sidecar_resources_requires_packaged_ffmpeg_and_ffprobe(tmp_path: Path) -> None:
    sidecar_dir = tmp_path / "sidecar"
    _write_required_pyinstaller_resources(sidecar_dir)

    with pytest.raises(RuntimeError) as exc_info:
        build_desktop_backend._validate_sidecar_resources(sidecar_dir)  # noqa: SLF001

    message = str(exc_info.value)
    assert "bin/ffmpeg" in message
    assert "bin/ffprobe" in message


def test_validate_existing_sidecar_requires_executable_and_runtime_resources(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sidecar_dir = tmp_path / "binaries" / "orpheus-desktop-backend"
    monkeypatch.setattr(build_desktop_backend, "TAURI_SIDECAR_RESOURCE_DIR", sidecar_dir)

    with pytest.raises(RuntimeError, match="Build it directly or run the Tauri release build"):
        build_desktop_backend.validate_existing_sidecar(
            target_triple="aarch64-apple-darwin",
            runtime_profile="mlx",
        )

    _write_executable(sidecar_dir / "orpheus-desktop-backend")
    _write_required_pyinstaller_resources(sidecar_dir)
    _write_executable(sidecar_dir / "bin" / "ffmpeg")
    _write_executable(sidecar_dir / "bin" / "ffprobe")

    (sidecar_dir / "runtime-profile.json").write_text(
        json.dumps({"profile": "base"}),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="runtime profile mismatch"):
        build_desktop_backend.validate_existing_sidecar(
            target_triple="aarch64-apple-darwin",
            runtime_profile="mlx",
        )
    (sidecar_dir / "runtime-profile.json").write_text("[]", encoding="utf-8")
    with pytest.raises(RuntimeError, match="runtime profile marker is invalid"):
        build_desktop_backend.validate_existing_sidecar(
            target_triple="aarch64-apple-darwin",
            runtime_profile="mlx",
        )
    (sidecar_dir / "runtime-profile.json").write_text(
        json.dumps({"profile": "mlx"}),
        encoding="utf-8",
    )

    assert (
        build_desktop_backend.validate_existing_sidecar(
            target_triple="aarch64-apple-darwin",
            runtime_profile="mlx",
        )
        == sidecar_dir / "orpheus-desktop-backend"
    )


def test_desktop_build_script_delegates_fresh_sidecar_build_to_tauri() -> None:
    package_json = json.loads(
        (PROJECT_ROOT / "desktop" / "package.json").read_text(encoding="utf-8")
    )

    assert package_json["scripts"]["desktop:build"] == "tauri build"
    assert package_json["scripts"]["desktop:release"].startswith("tauri build")


def test_tauri_build_freshens_sidecar_before_frontend() -> None:
    tauri_config = json.loads(
        (PROJECT_ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text(
            encoding="utf-8"
        )
    )

    assert tauri_config["build"]["beforeBuildCommand"] == (
        "python ../scripts/build_desktop_backend.py && npm run build"
    )


def test_release_docs_describe_fresh_profiled_tauri_sidecar_build() -> None:
    docs = {
        path: (PROJECT_ROOT / path).read_text(encoding="utf-8")
        for path in (
            "README.md",
            "docs/RUN_APPLICATION.md",
            "docs/TESTING.md",
            "docs/ARCHITECTURE.md",
            "docs/DEPENDENCIES.md",
        )
    }

    for contents in docs.values():
        assert "ORPHEUS_RUNTIME_PROFILE" in contents
    assert "TAURI_ENV_TARGET_TRIPLE" in docs["docs/RUN_APPLICATION.md"]
    assert "TAURI_ENV_TARGET_TRIPLE" in docs["docs/ARCHITECTURE.md"]
    assert "required before `desktop:build` or `desktop:release`" not in docs["README.md"]
    assert "Sidecar must already exist" not in docs["docs/ARCHITECTURE.md"]


def test_validate_runtime_dependencies_returns_resolved_ffmpeg_binary_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ffmpeg = _write_executable(tmp_path / "tools" / "ffmpeg")
    ffprobe = _write_executable(tmp_path / "tools" / "ffprobe")

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


@pytest.mark.parametrize(
    ("runtime_profile", "target_triple"),
    [
        ("base", "x86_64-unknown-linux-gnu"),
        ("mlx", "aarch64-apple-darwin"),
        ("cuda", "x86_64-unknown-linux-gnu"),
    ],
)
def test_runtime_profile_uses_environment_when_cli_omitted(
    monkeypatch,
    runtime_profile: str,
    target_triple: str,
) -> None:
    monkeypatch.setenv("ORPHEUS_RUNTIME_PROFILE", runtime_profile)

    assert build_desktop_backend._resolve_runtime_profile(target_triple, None) == runtime_profile


def test_explicit_runtime_profile_wins_over_environment(monkeypatch) -> None:
    monkeypatch.setenv("ORPHEUS_RUNTIME_PROFILE", "cuda")

    assert (
        build_desktop_backend._resolve_runtime_profile(  # noqa: SLF001
            "x86_64-unknown-linux-gnu",
            "base",
        )
        == "base"
    )


@pytest.mark.parametrize("previous", ["base", "cuda"])
def test_rebuild_keeps_previous_runtime_without_override(tmp_path, monkeypatch, previous) -> None:
    monkeypatch.delenv("ORPHEUS_RUNTIME_PROFILE", raising=False)
    monkeypatch.setattr(build_desktop_backend, "TAURI_SIDECAR_RESOURCE_DIR", tmp_path)
    (tmp_path / "runtime-profile.json").write_text(json.dumps({"profile": previous}))

    assert build_desktop_backend._resolve_build_runtime_profile(
        "x86_64-pc-windows-msvc", None
    ) == previous


@pytest.mark.parametrize("override", ["cli", "environment"])
def test_rebuild_allows_explicit_downgrade_to_base(tmp_path, monkeypatch, override) -> None:
    monkeypatch.delenv("ORPHEUS_RUNTIME_PROFILE", raising=False)
    monkeypatch.setattr(build_desktop_backend, "TAURI_SIDECAR_RESOURCE_DIR", tmp_path)
    (tmp_path / "runtime-profile.json").write_text('{"profile":"cuda"}')
    if override == "environment":
        monkeypatch.setenv("ORPHEUS_RUNTIME_PROFILE", "base")

    assert build_desktop_backend._resolve_build_runtime_profile(
        "x86_64-pc-windows-msvc", "base" if override == "cli" else None
    ) == "base"


def test_first_build_uses_platform_default(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ORPHEUS_RUNTIME_PROFILE", raising=False)
    monkeypatch.setattr(build_desktop_backend, "TAURI_SIDECAR_RESOURCE_DIR", tmp_path)
    assert build_desktop_backend._resolve_build_runtime_profile(
        "x86_64-pc-windows-msvc", None
    ) == "base"


def test_main_rebuild_passes_preserved_cuda_profile_to_every_build_step(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ORPHEUS_RUNTIME_PROFILE", raising=False)
    monkeypatch.setattr(build_desktop_backend, "TAURI_SIDECAR_RESOURCE_DIR", tmp_path)
    (tmp_path / "runtime-profile.json").write_text('{"profile":"cuda"}')
    monkeypatch.setattr(build_desktop_backend, "_parse_args", lambda: SimpleNamespace(
        target_triple="x86_64-pc-windows-msvc", runtime_profile=None,
        python="python", no_clean=False, validate_existing=False,
    ))
    monkeypatch.setattr(build_desktop_backend, "_validate_pyinstaller_target_matches_host", lambda _: None)
    validate = MagicMock(return_value={})
    build = MagicMock()
    stage = MagicMock(return_value=tmp_path / "backend.exe")
    monkeypatch.setattr(build_desktop_backend, "_validate_runtime_dependencies", validate)
    monkeypatch.setattr(build_desktop_backend, "_run_pyinstaller", build)
    monkeypatch.setattr(build_desktop_backend, "_copy_sidecar_for_tauri", stage)

    assert build_desktop_backend.main() == 0
    for step in (validate, build, stage):
        assert step.call_args.kwargs["runtime_profile"] == "cuda"


@pytest.mark.parametrize("marker", ['{"profile":"mlx"}', '{"profile":"unknown"}', '{}', '[]', 'bad json'])
def test_rebuild_requires_override_for_invalid_or_incompatible_marker(tmp_path, monkeypatch, marker) -> None:
    monkeypatch.delenv("ORPHEUS_RUNTIME_PROFILE", raising=False)
    monkeypatch.setattr(build_desktop_backend, "TAURI_SIDECAR_RESOURCE_DIR", tmp_path)
    (tmp_path / "runtime-profile.json").write_text(marker)
    with pytest.raises(RuntimeError, match="ORPHEUS_RUNTIME_PROFILE"):
        build_desktop_backend._resolve_build_runtime_profile("x86_64-pc-windows-msvc", None)


def test_invalid_environment_runtime_profile_fails_before_dependency_probes(monkeypatch) -> None:
    monkeypatch.setenv("ORPHEUS_RUNTIME_PROFILE", "bogus")
    monkeypatch.setattr(build_desktop_backend.platform, "system", lambda: "Linux")
    monkeypatch.setattr(build_desktop_backend.platform, "machine", lambda: "x86_64")

    def fail_if_probed(*_args, **_kwargs):  # noqa: ANN001, ANN002
        pytest.fail("dependency/PyInstaller probes must not run for an invalid profile")

    monkeypatch.setattr(build_desktop_backend.subprocess, "run", fail_if_probed)

    with pytest.raises(RuntimeError, match="ORPHEUS_RUNTIME_PROFILE.*bogus"):
        build_desktop_backend._validate_runtime_dependencies(  # noqa: SLF001
            "python",
            target_triple="x86_64-unknown-linux-gnu",
        )


def test_architecture_mismatch_precedes_invalid_environment_profile(monkeypatch) -> None:
    monkeypatch.setenv("ORPHEUS_RUNTIME_PROFILE", "bogus")
    monkeypatch.setattr(build_desktop_backend.platform, "system", lambda: "Linux")
    monkeypatch.setattr(build_desktop_backend.platform, "machine", lambda: "x86_64")

    def fail_if_probed(*_args, **_kwargs):  # noqa: ANN001, ANN002
        pytest.fail("dependency/PyInstaller probes must not run for an architecture mismatch")

    monkeypatch.setattr(build_desktop_backend.subprocess, "run", fail_if_probed)

    with pytest.raises(RuntimeError, match="architecture mismatch"):
        build_desktop_backend._validate_runtime_dependencies(  # noqa: SLF001
            "python",
            target_triple="aarch64-unknown-linux-gnu",
        )


def test_main_checks_architecture_before_environment_profile(monkeypatch) -> None:
    monkeypatch.setenv("ORPHEUS_RUNTIME_PROFILE", "bogus")
    monkeypatch.setattr(build_desktop_backend.platform, "system", lambda: "Linux")
    monkeypatch.setattr(build_desktop_backend.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        build_desktop_backend,
        "_parse_args",
        lambda: SimpleNamespace(
            target_triple="aarch64-unknown-linux-gnu",
            runtime_profile=None,
            python="python",
            no_clean=False,
            validate_existing=False,
        ),
    )

    with pytest.raises(RuntimeError, match="architecture mismatch"):
        build_desktop_backend.main()


def test_target_triple_prefers_tauri_environment(monkeypatch) -> None:
    monkeypatch.setenv("TAURI_ENV_TARGET_TRIPLE", "aarch64-apple-darwin")
    monkeypatch.setenv("CARGO_BUILD_TARGET", "x86_64-unknown-linux-gnu")
    monkeypatch.setenv("TARGET", "x86_64-pc-windows-msvc")

    assert build_desktop_backend._resolve_target_triple("") == "aarch64-apple-darwin"
    assert (
        build_desktop_backend._resolve_target_triple("x86_64-apple-darwin")
        == "x86_64-apple-darwin"
    )


def test_default_python_prefers_windows_project_virtualenv(monkeypatch, tmp_path: Path) -> None:
    windows_python = _write_executable(tmp_path / ".venv" / "Scripts" / "python.exe")
    _write_executable(tmp_path / ".venv" / "bin" / "python")
    monkeypatch.setattr(build_desktop_backend, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(build_desktop_backend.platform, "system", lambda: "Windows")
    monkeypatch.setattr(build_desktop_backend.sys, "executable", "fallback-python")

    assert build_desktop_backend._default_python_executable() == str(windows_python)


def test_default_python_prefers_posix_project_virtualenv(monkeypatch, tmp_path: Path) -> None:
    posix_python = _write_executable(tmp_path / ".venv" / "bin" / "python")
    _write_executable(tmp_path / ".venv" / "Scripts" / "python.exe")
    monkeypatch.setattr(build_desktop_backend, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(build_desktop_backend.platform, "system", lambda: "Linux")
    monkeypatch.setattr(build_desktop_backend.sys, "executable", "fallback-python")

    assert build_desktop_backend._default_python_executable() == str(posix_python)


def test_validate_runtime_dependencies_rejects_cross_os_target(monkeypatch) -> None:
    monkeypatch.setattr(build_desktop_backend.platform, "system", lambda: "Linux")

    with pytest.raises(RuntimeError, match="Cross-OS PyInstaller builds are not supported"):
        build_desktop_backend._validate_runtime_dependencies(  # noqa: SLF001
            "python",
            target_triple="aarch64-apple-darwin",
        )


@pytest.mark.parametrize(
    ("host_machine", "target_triple"),
    [
        ("x86_64", "aarch64-unknown-linux-gnu"),
        ("amd64", "arm64-unknown-linux-gnu"),
        ("arm64", "x86_64-unknown-linux-gnu"),
        ("aarch64", "amd64-unknown-linux-gnu"),
    ],
)
def test_validate_runtime_dependencies_rejects_host_target_architecture_mismatch_before_probes(
    monkeypatch,
    host_machine: str,
    target_triple: str,
) -> None:
    monkeypatch.setattr(build_desktop_backend.platform, "system", lambda: "Linux")
    monkeypatch.setattr(build_desktop_backend.platform, "machine", lambda: host_machine)

    def fail_if_probed(*_args, **_kwargs):  # noqa: ANN001, ANN002
        pytest.fail("dependency/PyInstaller probes must not run for an architecture mismatch")

    monkeypatch.setattr(build_desktop_backend.subprocess, "run", fail_if_probed)

    with pytest.raises(RuntimeError, match="architecture mismatch"):
        build_desktop_backend._validate_runtime_dependencies(  # noqa: SLF001
            "python",
            target_triple=target_triple,
            runtime_profile="base",
        )


@pytest.mark.parametrize(
    ("host_machine", "target_triple"),
    [
        ("amd64", "x86_64-unknown-linux-gnu"),
        ("x86_64", "amd64-unknown-linux-gnu"),
        ("arm64", "aarch64-unknown-linux-gnu"),
        ("aarch64", "arm64-unknown-linux-gnu"),
    ],
)
def test_validate_pyinstaller_target_normalizes_architecture_aliases(
    monkeypatch,
    host_machine: str,
    target_triple: str,
) -> None:
    monkeypatch.setattr(build_desktop_backend.platform, "system", lambda: "Linux")
    monkeypatch.setattr(build_desktop_backend.platform, "machine", lambda: host_machine)

    build_desktop_backend._validate_pyinstaller_target_matches_host(target_triple)  # noqa: SLF001


def test_cuda_backend_build_prerequisites_include_bitsandbytes() -> None:
    modules = set(
        build_desktop_backend.required_modules(
            "x86_64-unknown-linux-gnu",
            runtime_profile="cuda",
        )
    )

    assert "bitsandbytes" in modules
    assert {"spacy", "thinc", "en_core_web_sm"}.issubset(modules)


def test_base_backend_build_excludes_local_tts_spacy_stack() -> None:
    modules = set(
        build_desktop_backend.required_modules(
            "x86_64-unknown-linux-gnu",
            runtime_profile="base",
        )
    )
    assert {"spacy", "thinc", "en_core_web_sm"}.isdisjoint(modules)

    spec_text = build_desktop_backend.SPEC_FILE.read_text(encoding="utf-8")
    base_branch = spec_text.split('if runtime_profile == "base":', 1)[1].split("\na = Analysis", 1)[
        0
    ]
    assert '"spacy"' in base_branch
    assert '"thinc"' in base_branch
    assert '"en_core_web_sm"' in base_branch


def test_backend_sidecar_spec_excludes_build_only_test_frameworks() -> None:
    spec_text = build_desktop_backend.SPEC_FILE.read_text(encoding="utf-8")
    analysis_excludes = spec_text.split("excludes=profile_excludes + [", 1)[1].split(
        "\n    ],",
        1,
    )[0]

    assert '"fsspec.conftest"' in analysis_excludes
    assert '"hypothesis"' in analysis_excludes
    assert '"pytest"' in analysis_excludes
    assert '"_pytest"' in analysis_excludes


def test_backend_sidecar_spec_collects_bitsandbytes_for_cuda_packaging() -> None:
    spec_text = build_desktop_backend.SPEC_FILE.read_text(encoding="utf-8")

    assert '_collect_submodules("bitsandbytes")' in spec_text
    assert '_collect_dynamic_libs("bitsandbytes")' in spec_text
    assert 'runtime_profile == "cuda"' in spec_text


def test_backend_sidecar_spec_collects_mlx_dynamic_libs_for_rpath_closure() -> None:
    """PyInstaller cannot resolve @rpath/libjaccl.dylib from libmlx; collect explicitly."""
    spec_text = build_desktop_backend.SPEC_FILE.read_text(encoding="utf-8")
    data_setup = spec_text.split("datas = []", 1)[1].split("\nbinaries = []", 1)[0]
    binaries_setup = spec_text.split("binaries = []", 1)[1].split("\nprofile_excludes", 1)[0]

    assert '_collect_data_files("mlx", includes=["lib/*.metallib"])' in data_setup
    assert '_collect_dynamic_libs("mlx")' in binaries_setup


def test_backend_sidecar_spec_collects_phonemizer_data_for_base_profile() -> None:
    spec_text = build_desktop_backend.SPEC_FILE.read_text(encoding="utf-8")
    data_setup = spec_text.split("datas = []", 1)[1].split("\nbinaries = []", 1)[0]

    assert '\ndatas += _collect_data_files("espeakng_loader")' in data_setup
    assert (
        '\ndatas += _collect_data_files("language_tags", includes=["data/json/*.json"])'
        in data_setup
    )


def test_backend_sidecar_spec_collects_curated_mlx_phonemizers() -> None:
    spec_text = build_desktop_backend.SPEC_FILE.read_text(encoding="utf-8")
    mlx_branch = spec_text.split('if runtime_profile == "mlx":', 1)[1].split(
        'elif runtime_profile == "cuda":',
        1,
    )[0]

    assert 'MLX_PHONEMIZER_HIDDEN_IMPORTS = ("misaki.en", "misaki.espeak")' in spec_text
    assert "hiddenimports += MLX_PHONEMIZER_HIDDEN_IMPORTS" in mlx_branch
    assert 'collect_submodules("misaki")' not in mlx_branch


@pytest.mark.parametrize("runtime_profile", ["base", "mlx"])
def test_torch_stack_is_not_required_for_torch_free_profiles(runtime_profile: str) -> None:
    target = "aarch64-apple-darwin" if runtime_profile == "mlx" else "x86_64-unknown-linux-gnu"
    modules = set(build_desktop_backend.required_modules(target, runtime_profile=runtime_profile))

    assert {"torch", "kokoro", "bitsandbytes"}.isdisjoint(modules)


def test_runtime_profile_defaults_to_mlx_only_for_apple_silicon() -> None:
    assert build_desktop_backend.default_runtime_profile("aarch64-apple-darwin") == "mlx"
    assert build_desktop_backend.default_runtime_profile("x86_64-apple-darwin") == "base"
    assert build_desktop_backend.default_runtime_profile("x86_64-unknown-linux-gnu") == "base"


def test_mlx_profile_rejects_non_apple_silicon_target() -> None:
    with pytest.raises(RuntimeError, match="MLX runtime profile"):
        build_desktop_backend.required_modules(
            "x86_64-apple-darwin",
            runtime_profile="mlx",
        )


def test_mlx_build_rejects_macos_older_than_14(monkeypatch) -> None:
    monkeypatch.setattr(build_desktop_backend.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(build_desktop_backend.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        build_desktop_backend.platform, "mac_ver", lambda: ("13.6.9", ("", "", ""), "")
    )

    with pytest.raises(RuntimeError, match="macOS 14"):
        build_desktop_backend._validate_runtime_dependencies(  # noqa: SLF001
            "python",
            target_triple="aarch64-apple-darwin",
            runtime_profile="mlx",
        )


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


def test_validate_mlx_native_dependency_closure_fails_when_rpath_dylib_missing(
    tmp_path: Path,
) -> None:
    """Missing @rpath siblings (e.g. libjaccl) must fail packaging validation."""
    sidecar_dir = tmp_path / "sidecar"
    nested_dylib = sidecar_dir / "_internal" / "mlx" / "lib" / "libmlx.dylib"
    nested_dylib.parent.mkdir(parents=True)
    nested_dylib.write_bytes(b"libmlx")

    def read_dependencies(path: Path) -> tuple[str, ...]:
        if Path(path).resolve() == nested_dylib.resolve():
            return (
                "@rpath/libmlx.dylib",
                "@rpath/libjaccl.dylib",
                "/usr/lib/libSystem.B.dylib",
            )
        return ()

    with pytest.raises(RuntimeError, match="libjaccl\\.dylib"):
        build_desktop_backend.validate_mlx_native_dependency_closure(
            sidecar_dir,
            read_dependencies=read_dependencies,
            read_rpaths=lambda _path: ["@loader_path"],
        )


def test_validate_mlx_native_dependency_closure_passes_when_rpath_siblings_present(
    tmp_path: Path,
) -> None:
    sidecar_dir = tmp_path / "sidecar"
    lib_dir = sidecar_dir / "_internal" / "mlx" / "lib"
    lib_dir.mkdir(parents=True)
    (lib_dir / "libmlx.dylib").write_bytes(b"libmlx")
    (lib_dir / "libjaccl.dylib").write_bytes(b"libjaccl")

    def read_dependencies(path: Path) -> tuple[str, ...]:
        name = Path(path).name
        if name == "libmlx.dylib":
            return ("@rpath/libmlx.dylib", "@rpath/libjaccl.dylib", "/usr/lib/libSystem.B.dylib")
        if name == "libjaccl.dylib":
            return ("@rpath/libjaccl.dylib", "/usr/lib/libSystem.B.dylib")
        return ()

    build_desktop_backend.validate_mlx_native_dependency_closure(
        sidecar_dir,
        read_dependencies=read_dependencies,
        read_rpaths=lambda _path: ["@loader_path"],
    )


def test_ensure_mlx_native_dylib_closure_copies_missing_rpath_siblings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Defense in depth: copy unresolved @rpath MLX libs from the build env."""
    source_root = tmp_path / "site-packages" / "mlx" / "lib"
    source_root.mkdir(parents=True)
    (source_root / "libmlx.dylib").write_bytes(b"src-mlx")
    (source_root / "libjaccl.dylib").write_bytes(b"src-jaccl")

    sidecar_dir = tmp_path / "sidecar"
    lib_dir = sidecar_dir / "_internal" / "mlx" / "lib"
    lib_dir.mkdir(parents=True)
    (lib_dir / "libmlx.dylib").write_bytes(b"packaged-mlx")

    monkeypatch.setattr(
        build_desktop_backend,
        "_mlx_site_lib_dir",
        lambda: source_root,
    )

    def read_dependencies(path: Path) -> tuple[str, ...]:
        if Path(path).name == "libmlx.dylib":
            return ("@rpath/libjaccl.dylib", "/usr/lib/libSystem.B.dylib")
        return ("/usr/lib/libSystem.B.dylib",)

    build_desktop_backend.ensure_mlx_native_dylib_closure(
        sidecar_dir,
        read_dependencies=read_dependencies,
        read_rpaths=lambda _path: ["@loader_path"],
    )

    assert (lib_dir / "libjaccl.dylib").read_bytes() == b"src-jaccl"


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Requires macOS sidecar packaging tools and Mach-O dependency handling",
)
def test_copy_sidecar_for_tauri_rejects_incomplete_mlx_rpath_closure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    dist_dir = tmp_path / "dist"
    binaries_dir = tmp_path / "binaries"
    onedir = dist_dir / "orpheus-desktop-backend"
    executable = _write_executable(onedir / "orpheus-desktop-backend", "backend")
    _write_required_pyinstaller_resources(onedir)
    # metallib present, but no libmlx/libjaccl — closure must still fail for mlx.
    ffmpeg_source = _write_executable(tmp_path / "tools" / "ffmpeg")
    ffprobe_source = _write_executable(tmp_path / "tools" / "ffprobe")

    monkeypatch.setattr(build_desktop_backend, "DIST_DIR", dist_dir)
    monkeypatch.setattr(build_desktop_backend, "TAURI_BINARIES_DIR", binaries_dir)
    monkeypatch.setattr(
        build_desktop_backend,
        "TAURI_SIDECAR_RESOURCE_DIR",
        binaries_dir / "orpheus-desktop-backend",
    )
    monkeypatch.setattr(build_desktop_backend.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        build_desktop_backend,
        "_normalize_mlx_dylib_layout",
        lambda _sidecar: None,
    )
    monkeypatch.setattr(
        build_desktop_backend,
        "ensure_mlx_native_dylib_closure",
        lambda *_args, **_kwargs: None,
    )

    def fail_closure(sidecar_dir: Path, **_kwargs) -> None:  # noqa: ANN003
        raise RuntimeError(f"unresolved MLX native dependency libjaccl.dylib in {sidecar_dir}")

    monkeypatch.setattr(
        build_desktop_backend,
        "validate_mlx_native_dependency_closure",
        fail_closure,
    )

    with pytest.raises(RuntimeError, match="libjaccl\\.dylib"):
        build_desktop_backend._copy_sidecar_for_tauri(  # noqa: SLF001
            "aarch64-apple-darwin",
            ffmpeg_binaries={"ffmpeg": ffmpeg_source, "ffprobe": ffprobe_source},
            runtime_profile="mlx",
        )

    assert executable.is_file()


def test_validate_mlx_native_dependency_closure_catches_real_missing_libjaccl(
    tmp_path: Path,
) -> None:
    """Regression: real libmlx.dylib @rpath/libjaccl must not pass without the sibling."""
    if platform.system() != "Darwin":
        pytest.skip("Mach-O MLX closure validation is Darwin-only")

    venv_lib = (
        PROJECT_ROOT
        / ".venv"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
        / "mlx"
        / "lib"
    )
    source_lib = venv_lib
    if not (source_lib / "libmlx.dylib").is_file():
        pytest.skip("mlx libmlx.dylib is not installed in the build environment")
    if not (source_lib / "libjaccl.dylib").is_file():
        pytest.skip("mlx libjaccl.dylib is not installed in the build environment")

    sidecar_dir = tmp_path / "sidecar"
    lib_dir = sidecar_dir / "_internal" / "mlx" / "lib"
    lib_dir.mkdir(parents=True)
    packaged_mlx = lib_dir / "libmlx.dylib"
    shutil.copy2(source_lib / "libmlx.dylib", packaged_mlx)
    # Match packaging: same-directory @rpath resolution for sibling natives.
    subprocess.run(
        ["install_name_tool", "-add_rpath", "@loader_path", str(packaged_mlx)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["codesign", "--force", "--sign", "-", str(packaged_mlx)],
        check=True,
        capture_output=True,
        text=True,
    )

    with pytest.raises(RuntimeError, match="libjaccl\\.dylib"):
        build_desktop_backend.validate_mlx_native_dependency_closure(sidecar_dir)

    shutil.copy2(source_lib / "libjaccl.dylib", lib_dir / "libjaccl.dylib")
    build_desktop_backend.validate_mlx_native_dependency_closure(sidecar_dir)

    import ctypes

    ctypes.CDLL(str(packaged_mlx))


def test_normalize_mlx_dylib_layout_adds_loader_path_rpath_on_libmlx(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sidecar_dir = tmp_path / "sidecar"
    core_extension = sidecar_dir / "_internal" / "mlx" / "core.cpython-312-darwin.so"
    nested_dylib = sidecar_dir / "_internal" / "mlx" / "lib" / "libmlx.dylib"
    core_extension.parent.mkdir(parents=True)
    nested_dylib.parent.mkdir(parents=True)
    core_extension.write_bytes(b"core")
    nested_dylib.write_bytes(b"nested")
    commands: list[list[str]] = []

    monkeypatch.setattr(build_desktop_backend.platform, "system", lambda: "Darwin")

    def fake_rpaths(path: Path) -> list[str]:
        if Path(path).name.startswith("core."):
            return ["@loader_path/lib"]
        return []

    monkeypatch.setattr(build_desktop_backend, "_macho_rpaths", fake_rpaths)

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        commands.append([str(part) for part in command])
        return MagicMock(returncode=0)

    monkeypatch.setattr(build_desktop_backend.subprocess, "run", fake_run)

    build_desktop_backend._normalize_mlx_dylib_layout(sidecar_dir)  # noqa: SLF001

    assert [
        "install_name_tool",
        "-add_rpath",
        "@loader_path",
        str(nested_dylib),
    ] in commands
    assert ["codesign", "--force", "--sign", "-", str(nested_dylib)] in commands


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
    if os.name == "nt":
        assert phontab_link.is_file()
    else:
        assert phontab_link.is_symlink()
        assert (
            phontab_link.readlink()
            == Path("_internal") / "espeakng_loader" / "espeak-ng-data" / "phontab"
        )
    assert voices_dir.is_dir()
    assert not voices_dir.is_symlink()
    assert phontab_link.read_text(encoding="utf-8") == "phoneme table"
    assert (voices_dir / "en").read_text(encoding="utf-8") == "english voices"


def test_backend_build_prerequisites_include_mlx_runtime_imports() -> None:
    modules = set(
        build_desktop_backend.required_modules(
            "aarch64-apple-darwin",
            runtime_profile="mlx",
        )
    )

    assert "jinja2" not in modules
    assert {"en_core_web_sm", "scipy", "spacy", "thinc", "mlx", "mlx_audio"}.issubset(modules)
    assert "torch" not in modules


def test_windows_backend_build_prerequisites_use_kokoro_package_not_mlx() -> None:
    modules = set(
        build_desktop_backend.required_modules(
            "x86_64-pc-windows-msvc",
            runtime_profile="cuda",
        )
    )

    assert {"kokoro", "torch", "soundfile", "espeakng_loader"}.issubset(modules)
    assert "mlx" not in modules
    assert "mlx_lm" not in modules
    assert "mlx_audio" not in modules


def test_parse_otool_dependencies_skips_header_and_compat_notes() -> None:
    output = """\
/opt/homebrew/bin/ffmpeg:
\t/opt/homebrew/Cellar/ffmpeg/8.1.1/lib/libavcodec.62.dylib (compatibility version 62.0.0, current version 62.28.101)
\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1356.0.0)
\t/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation (compatibility version 150.0.0, current version 4424.1.255)
\t@rpath/libfoo.dylib (compatibility version 1.0.0, current version 1.0.0)
"""
    deps = build_desktop_backend.parse_otool_dependencies(output)
    assert deps == (
        "/opt/homebrew/Cellar/ffmpeg/8.1.1/lib/libavcodec.62.dylib",
        "/usr/lib/libSystem.B.dylib",
        "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation",
        "@rpath/libfoo.dylib",
    )


def test_is_system_macho_dependency_excludes_apple_system_paths() -> None:
    assert build_desktop_backend.is_system_macho_dependency("/usr/lib/libSystem.B.dylib")
    assert build_desktop_backend.is_system_macho_dependency(
        "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation"
    )
    assert build_desktop_backend.is_system_macho_dependency("/Library/Apple/usr/lib/libfoo.dylib")
    assert not build_desktop_backend.is_system_macho_dependency(
        "/opt/homebrew/opt/ffmpeg/lib/libavcodec.dylib"
    )
    assert not build_desktop_backend.is_system_macho_dependency("@loader_path/lib/libavcodec.dylib")


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Requires macOS sidecar packaging tools and Mach-O dependency handling",
)
def test_collect_non_system_macho_dependencies_is_recursive(tmp_path: Path) -> None:
    root = tmp_path / "ffmpeg"
    root.write_bytes(b"ffmpeg")
    lib_a = tmp_path / "cellar" / "libavcodec.dylib"
    lib_b = tmp_path / "cellar" / "libavutil.dylib"
    lib_a.parent.mkdir(parents=True)
    lib_a.write_bytes(b"avcodec")
    lib_b.write_bytes(b"avutil")

    graph = {
        root.resolve(): (str(lib_a), "/usr/lib/libSystem.B.dylib"),
        lib_a.resolve(): (str(lib_b), "/System/Library/Frameworks/CoreFoundation.framework/C"),
        lib_b.resolve(): ("/usr/lib/libSystem.B.dylib",),
    }

    collected = build_desktop_backend.collect_non_system_macho_dependencies(
        root,
        read_dependencies=lambda path: graph[Path(path).resolve()],
    )
    assert collected.canonical_sources == (lib_a.resolve(), lib_b.resolve())
    assert collected.install_names_for(lib_a.resolve()) == frozenset({str(lib_a)})
    assert collected.install_names_for(lib_b.resolve()) == frozenset({str(lib_b)})


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Requires macOS sidecar packaging tools and Mach-O dependency handling",
)
def test_collect_preserves_symlink_and_opt_alias_install_names(tmp_path: Path) -> None:
    root = tmp_path / "ffmpeg"
    root.write_bytes(b"ffmpeg")
    cellar = tmp_path / "Cellar" / "ffmpeg" / "8.1.1" / "lib"
    cellar.mkdir(parents=True)
    real_lib = cellar / "libavdevice.62.3.101.dylib"
    real_lib.write_bytes(b"avdevice")
    link_lib = cellar / "libavdevice.62.dylib"
    link_lib.symlink_to("libavdevice.62.3.101.dylib")
    opt_lib = tmp_path / "opt" / "ffmpeg" / "lib" / "libavdevice.62.dylib"
    opt_lib.parent.mkdir(parents=True)
    opt_lib.symlink_to(real_lib)

    cellar_install = str(link_lib)
    opt_install = str(opt_lib)
    dep_graph = {
        root.resolve(): (cellar_install, opt_install, "/usr/lib/libSystem.B.dylib"),
        real_lib.resolve(): ("/usr/lib/libSystem.B.dylib",),
    }

    collected = build_desktop_backend.collect_non_system_macho_dependencies(
        root,
        read_dependencies=lambda path: dep_graph[Path(path).resolve()],
    )

    assert collected.canonical_sources == (real_lib.resolve(),)
    assert collected.install_names_for(real_lib.resolve()) == frozenset(
        {cellar_install, opt_install}
    )


def test_plan_private_lib_copies_fails_on_basename_collision(tmp_path: Path) -> None:
    first = tmp_path / "one" / "libfoo.dylib"
    second = tmp_path / "two" / "libfoo.dylib"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"aaa")
    second.write_bytes(b"bbb")
    lib_dir = tmp_path / "sidecar" / "bin" / "lib"

    differing = build_desktop_backend.MachODependencyGraph(
        {
            first.resolve(): frozenset({str(first)}),
            second.resolve(): frozenset({str(second)}),
        }
    )
    with pytest.raises(RuntimeError, match="basename collision|libfoo"):
        build_desktop_backend.plan_private_lib_copies(differing, lib_dir=lib_dir)

    identical_bytes = tmp_path / "three" / "libfoo.dylib"
    identical_bytes.parent.mkdir(parents=True)
    identical_bytes.write_bytes(b"aaa")
    same_content_collision = build_desktop_backend.MachODependencyGraph(
        {
            first.resolve(): frozenset({str(first)}),
            identical_bytes.resolve(): frozenset({str(identical_bytes)}),
        }
    )
    with pytest.raises(RuntimeError, match="basename collision|libfoo"):
        build_desktop_backend.plan_private_lib_copies(
            same_content_collision,
            lib_dir=lib_dir,
        )


def test_plan_private_lib_copies_uses_original_install_name_basename(
    tmp_path: Path,
) -> None:
    cellar = tmp_path / "Cellar" / "lib"
    cellar.mkdir(parents=True)
    real_lib = cellar / "libavdevice.62.3.101.dylib"
    real_lib.write_bytes(b"avdevice")
    link_lib = cellar / "libavdevice.62.dylib"
    link_lib.symlink_to("libavdevice.62.3.101.dylib")
    opt_lib = tmp_path / "opt" / "lib" / "libavdevice.62.dylib"
    opt_lib.parent.mkdir(parents=True)
    opt_lib.symlink_to(real_lib)
    lib_dir = tmp_path / "sidecar" / "bin" / "lib"

    graph = build_desktop_backend.MachODependencyGraph(
        {
            real_lib.resolve(): frozenset({str(link_lib), str(opt_lib)}),
        }
    )
    planned = build_desktop_backend.plan_private_lib_copies(graph, lib_dir=lib_dir)

    assert planned.copies == {real_lib.resolve(): lib_dir / "libavdevice.62.dylib"}
    assert planned.root_install_name_changes == {
        str(link_lib): "@loader_path/lib/libavdevice.62.dylib",
        str(opt_lib): "@loader_path/lib/libavdevice.62.dylib",
    }
    assert planned.lib_install_name_changes == {
        str(link_lib): "@loader_path/libavdevice.62.dylib",
        str(opt_lib): "@loader_path/libavdevice.62.dylib",
    }


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Requires macOS sidecar packaging tools and Mach-O dependency handling",
)
def test_package_macho_dependencies_relocatable_rewrites_and_codesigns(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bin_dir = tmp_path / "sidecar" / "bin"
    bin_dir.mkdir(parents=True)
    ffmpeg = bin_dir / "ffmpeg"
    ffprobe = bin_dir / "ffprobe"
    ffmpeg.write_bytes(b"ffmpeg")
    ffprobe.write_bytes(b"ffprobe")
    ffmpeg.chmod(0o555)
    ffprobe.chmod(0o555)

    cellar = tmp_path / "cellar"
    lib_av = cellar / "libavcodec.62.dylib"
    lib_util = cellar / "libavutil.60.dylib"
    cellar.mkdir()
    lib_av.write_bytes(b"avcodec-bytes")
    lib_util.write_bytes(b"avutil-bytes")

    dep_graph = {
        ffmpeg.resolve(): (str(lib_av), "/usr/lib/libSystem.B.dylib"),
        ffprobe.resolve(): (str(lib_av), "/usr/lib/libSystem.B.dylib"),
        lib_av.resolve(): (str(lib_util), "/usr/lib/libSystem.B.dylib"),
        lib_util.resolve(): ("/usr/lib/libSystem.B.dylib",),
    }
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        commands.append([str(part) for part in command])
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(build_desktop_backend.platform, "system", lambda: "Darwin")

    build_desktop_backend.package_macho_dependencies_relocatable(  # noqa: SLF001
        (ffmpeg, ffprobe),
        lib_dir=bin_dir / "lib",
        host_system="Darwin",
        read_dependencies=lambda path: dep_graph[Path(path).resolve()],
        run_tool=fake_run,
    )

    lib_dir = bin_dir / "lib"
    assert (lib_dir / "libavcodec.62.dylib").read_bytes() == b"avcodec-bytes"
    assert (lib_dir / "libavutil.60.dylib").read_bytes() == b"avutil-bytes"
    assert (lib_dir / "libavcodec.62.dylib").stat().st_mode & stat.S_IWUSR
    assert (lib_dir / "libavutil.60.dylib").stat().st_mode & stat.S_IWUSR

    assert [
        "install_name_tool",
        "-change",
        str(lib_av),
        "@loader_path/lib/libavcodec.62.dylib",
        str(ffmpeg),
    ] in commands
    assert [
        "install_name_tool",
        "-change",
        str(lib_util),
        "@loader_path/libavutil.60.dylib",
        str(lib_dir / "libavcodec.62.dylib"),
    ] in commands
    assert [
        "install_name_tool",
        "-id",
        "@loader_path/libavcodec.62.dylib",
        str(lib_dir / "libavcodec.62.dylib"),
    ] in commands
    assert ["codesign", "--force", "--sign", "-", str(ffmpeg)] in commands
    assert [
        "codesign",
        "--force",
        "--sign",
        "-",
        str(lib_dir / "libavcodec.62.dylib"),
    ] in commands


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Requires macOS sidecar packaging tools and Mach-O dependency handling",
)
def test_package_macho_dependencies_rewrites_symlink_install_names_not_resolved(
    tmp_path: Path,
) -> None:
    """Regression: -change must use original otool spelling, not Path.resolve().

    Homebrew ships ABI symlink install names (libavdevice.62.dylib) that resolve to
    versioned files (libavdevice.62.3.101.dylib). install_name_tool silently no-ops
    when given the resolved spelling that is not present in the Mach-O load commands.
    """
    bin_dir = tmp_path / "sidecar" / "bin"
    bin_dir.mkdir(parents=True)
    ffmpeg = bin_dir / "ffmpeg"
    ffmpeg.write_bytes(b"ffmpeg")

    cellar = tmp_path / "Cellar" / "ffmpeg" / "8.1.1" / "lib"
    cellar.mkdir(parents=True)
    real_lib = cellar / "libavdevice.62.3.101.dylib"
    real_lib.write_bytes(b"avdevice")
    link_lib = cellar / "libavdevice.62.dylib"
    link_lib.symlink_to("libavdevice.62.3.101.dylib")
    opt_lib = tmp_path / "opt" / "ffmpeg" / "lib" / "libavdevice.62.dylib"
    opt_lib.parent.mkdir(parents=True)
    opt_lib.symlink_to(real_lib)

    cellar_install = str(link_lib)
    opt_install = str(opt_lib)
    dep_graph = {
        ffmpeg.resolve(): (cellar_install, opt_install, "/usr/lib/libSystem.B.dylib"),
        real_lib.resolve(): ("/usr/lib/libSystem.B.dylib",),
    }
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003
        commands.append([str(part) for part in command])
        return MagicMock(returncode=0, stdout="", stderr="")

    build_desktop_backend.package_macho_dependencies_relocatable(
        (ffmpeg,),
        lib_dir=bin_dir / "lib",
        host_system="Darwin",
        read_dependencies=lambda path: dep_graph[Path(path).resolve()],
        run_tool=fake_run,
    )

    dest = bin_dir / "lib" / "libavdevice.62.dylib"
    assert dest.is_file()
    assert dest.read_bytes() == b"avdevice"
    assert not (bin_dir / "lib" / "libavdevice.62.3.101.dylib").exists()

    change_olds = [
        command[2] for command in commands if command[:2] == ["install_name_tool", "-change"]
    ]
    assert cellar_install in change_olds
    assert opt_install in change_olds
    assert str(real_lib.resolve()) not in change_olds
    assert [
        "install_name_tool",
        "-change",
        cellar_install,
        "@loader_path/lib/libavdevice.62.dylib",
        str(ffmpeg),
    ] in commands
    assert [
        "install_name_tool",
        "-change",
        opt_install,
        "@loader_path/lib/libavdevice.62.dylib",
        str(ffmpeg),
    ] in commands
    assert [
        "install_name_tool",
        "-id",
        "@loader_path/libavdevice.62.dylib",
        str(dest),
    ] in commands


def test_package_macho_dependencies_relocatable_is_noop_off_darwin(tmp_path: Path) -> None:
    binary = tmp_path / "ffmpeg"
    binary.write_bytes(b"x")
    called = {"read": False}

    def boom(_path: Path) -> tuple[str, ...]:
        called["read"] = True
        raise AssertionError("should not read dependencies off Darwin")

    build_desktop_backend.package_macho_dependencies_relocatable(
        (binary,),
        lib_dir=tmp_path / "lib",
        host_system="Linux",
        read_dependencies=boom,
        run_tool=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no tools")),
    )
    assert called["read"] is False
    assert not (tmp_path / "lib").exists()


def test_copy_ffmpeg_binaries_packages_relocatable_deps_on_darwin(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ffmpeg_source = _write_executable(tmp_path / "source" / "ffmpeg", "ffmpeg")
    ffprobe_source = _write_executable(tmp_path / "source" / "ffprobe", "ffprobe")
    sidecar_dir = tmp_path / "sidecar"
    seen: dict[str, object] = {}

    def fake_package(binaries, *, lib_dir, **kwargs):  # noqa: ANN001, ANN003
        seen["binaries"] = tuple(Path(p) for p in binaries)
        seen["lib_dir"] = Path(lib_dir)
        seen["kwargs"] = kwargs

    monkeypatch.setattr(build_desktop_backend.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        build_desktop_backend,
        "package_macho_dependencies_relocatable",
        fake_package,
    )

    build_desktop_backend._copy_ffmpeg_binaries(  # noqa: SLF001
        sidecar_dir,
        {"ffmpeg": ffmpeg_source, "ffprobe": ffprobe_source},
        target_triple="aarch64-apple-darwin",
    )

    assert seen["lib_dir"] == sidecar_dir / "bin" / "lib"
    assert {p.name for p in seen["binaries"]} == {"ffmpeg", "ffprobe"}


def test_copy_ffmpeg_binaries_skips_relocation_on_windows_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ffmpeg_source = _write_executable(tmp_path / "source" / "ffmpeg.exe", "ffmpeg")
    ffprobe_source = _write_executable(tmp_path / "source" / "ffprobe.exe", "ffprobe")
    sidecar_dir = tmp_path / "sidecar"

    def boom(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("Windows packaging must not relocate Mach-O deps")

    monkeypatch.setattr(
        build_desktop_backend,
        "package_macho_dependencies_relocatable",
        boom,
    )

    build_desktop_backend._copy_ffmpeg_binaries(  # noqa: SLF001
        sidecar_dir,
        {"ffmpeg": ffmpeg_source, "ffprobe": ffprobe_source},
        target_triple="x86_64-pc-windows-msvc",
    )
    assert (sidecar_dir / "bin" / "ffmpeg.exe").is_file()
    assert (sidecar_dir / "bin" / "ffprobe.exe").is_file()


def test_validate_relocated_macho_rejects_forbidden_absolute_deps(tmp_path: Path) -> None:
    binary = tmp_path / "ffmpeg"
    binary.write_bytes(b"x")

    with pytest.raises(RuntimeError, match="forbidden|/opt/homebrew"):
        build_desktop_backend.validate_macho_has_no_forbidden_absolute_deps(
            binary,
            read_dependencies=lambda _p: (
                "/opt/homebrew/opt/ffmpeg/lib/libavcodec.dylib",
                "/usr/lib/libSystem.B.dylib",
            ),
        )

    build_desktop_backend.validate_macho_has_no_forbidden_absolute_deps(
        binary,
        read_dependencies=lambda _p: (
            "@loader_path/lib/libavcodec.dylib",
            "/usr/lib/libSystem.B.dylib",
        ),
    )


def test_build_help_exposes_explicit_runtime_profile_selector() -> None:
    completed = subprocess.run(
        [sys.executable, str(build_desktop_backend.__file__), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "--runtime-profile" in completed.stdout
    assert "--validate-existing" in completed.stdout


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
