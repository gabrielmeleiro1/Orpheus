"""Tests for packaging the desktop backend sidecar for Tauri."""

from __future__ import annotations

import json
import os
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


def _write_required_pyinstaller_resources(onedir: Path) -> None:
    internal_dir = onedir / "_internal"
    (internal_dir / "mlx" / "lib").mkdir(parents=True)
    (internal_dir / "mlx" / "lib" / "mlx.metallib").write_text("metal", encoding="utf-8")
    (internal_dir / "espeakng_loader" / "espeak-ng-data").mkdir(parents=True)
    (internal_dir / "espeakng_loader" / "espeak-ng-data" / "phontab").write_text(
        "phoneme table",
        encoding="utf-8",
    )
    _write_requirements_manifests(onedir)


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

    with pytest.raises(RuntimeError, match="mlx/lib/mlx\\.metallib"):
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
            "docs/architecture/tooling-boundaries.md",
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


def test_backend_sidecar_spec_collects_bitsandbytes_for_cuda_packaging() -> None:
    spec_text = build_desktop_backend.SPEC_FILE.read_text(encoding="utf-8")

    assert '_collect_submodules("bitsandbytes")' in spec_text
    assert '_collect_dynamic_libs("bitsandbytes")' in spec_text
    assert 'runtime_profile == "cuda"' in spec_text


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
