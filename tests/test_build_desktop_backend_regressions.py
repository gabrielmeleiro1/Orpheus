"""Regression coverage for build desktop backend."""

from pathlib import Path
import json
import platform
import re
import sys

import pytest

from scripts import build_desktop_backend as build


MANIFESTS = tuple(f"_internal/requirements/{name}.txt" for name in ("base", "mlx", "cuda"))


def _base_sidecar(root):
    executable = root / "orpheus-desktop-backend"
    executable.write_bytes(b"backend")
    executable.chmod(0o700)
    for relative in (*MANIFESTS, "bin/ffmpeg", "bin/ffprobe"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"resource")
    (root / "runtime-profile.json").write_text('{"profile":"base"}', encoding="utf-8")
    return executable


@pytest.mark.parametrize("target,profile,extra,suffix", [
    ("aarch64-apple-darwin", "base", (), ""),
    ("aarch64-apple-darwin", "mlx", (
        "_internal/espeakng_loader/espeak-ng-data/phontab",
        "_internal/mlx/lib/mlx.metallib",
        "_internal/mlx/lib/libmlx.dylib",
    ), ""),
    ("x86_64-pc-windows-msvc", "base", (), ".exe"),
    ("x86_64-pc-windows-msvc", "cuda", (
        "_internal/espeakng_loader/espeak-ng-data/phontab",
    ), ".exe"),
    ("x86_64-unknown-linux-gnu", "cuda", (
        "_internal/espeakng_loader/espeak-ng-data/phontab",
    ), ""),
])
def test_sidecar_resources_have_profile_specific_layout(target, profile, extra, suffix):
    assert tuple(path.as_posix() for path in build.required_sidecar_resources(
        target, runtime_profile=profile,
    )) == (
        "runtime-profile.json", *MANIFESTS, *extra,
        f"bin/ffmpeg{suffix}", f"bin/ffprobe{suffix}",
    )


def test_existing_sidecar_uses_embedded_profile_over_environment(tmp_path, monkeypatch):
    executable = _base_sidecar(tmp_path)
    monkeypatch.setattr(build, "TAURI_SIDECAR_RESOURCE_DIR", tmp_path)
    monkeypatch.setenv("ORPHEUS_RUNTIME_PROFILE", "mlx")

    assert build.validate_existing_sidecar(target_triple="aarch64-apple-darwin") == executable
    with pytest.raises(RuntimeError, match="profile mismatch"):
        (tmp_path / "runtime-profile.json").write_text('{"profile":"mlx"}', encoding="utf-8")
        build.validate_existing_sidecar(
            target_triple="aarch64-apple-darwin", runtime_profile="base",
        )


@pytest.mark.parametrize("payload", ["{", "[]", "{}", '{"profile":"unknown"}'])
def test_existing_sidecar_rejects_invalid_profile_markers(tmp_path, monkeypatch, payload):
    _base_sidecar(tmp_path)
    (tmp_path / "runtime-profile.json").write_text(payload, encoding="utf-8")
    monkeypatch.setattr(build, "TAURI_SIDECAR_RESOURCE_DIR", tmp_path)
    with pytest.raises(RuntimeError, match="profile"):
        build.validate_existing_sidecar(target_triple="aarch64-apple-darwin")


@pytest.mark.parametrize("relative", [*MANIFESTS, "bin/ffmpeg", "bin/ffprobe"])
def test_existing_sidecar_requires_resource_files(tmp_path, monkeypatch, relative):
    _base_sidecar(tmp_path)
    missing = tmp_path / relative
    missing.unlink()
    missing.mkdir()
    monkeypatch.setattr(build, "TAURI_SIDECAR_RESOURCE_DIR", tmp_path)
    with pytest.raises(RuntimeError, match=re.escape(relative)):
        build.validate_existing_sidecar(target_triple="aarch64-apple-darwin")


def test_rebuild_profile_precedence_is_argument_environment_marker_then_target(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "TAURI_SIDECAR_RESOURCE_DIR", tmp_path)
    monkeypatch.delenv("ORPHEUS_RUNTIME_PROFILE", raising=False)
    assert build._resolve_build_runtime_profile("aarch64-apple-darwin", None) == "mlx"
    assert build._resolve_build_runtime_profile("x86_64-pc-windows-msvc", None) == "base"
    (tmp_path / "runtime-profile.json").write_text('{"profile":"cuda"}', encoding="utf-8")
    assert build._resolve_build_runtime_profile("x86_64-pc-windows-msvc", None) == "cuda"
    monkeypatch.setenv("ORPHEUS_RUNTIME_PROFILE", "base")
    assert build._resolve_build_runtime_profile("x86_64-pc-windows-msvc", None) == "base"
    assert build._resolve_build_runtime_profile("x86_64-pc-windows-msvc", "cuda") == "cuda"


@pytest.mark.parametrize("system,preferred,alternate", [
    ("Windows", "Scripts/python.exe", "bin/python"),
    ("Darwin", "bin/python", "Scripts/python.exe"),
    ("Linux", "bin/python", "Scripts/python.exe"),
])
def test_default_interpreter_prefers_host_virtualenv_then_running_python(
    tmp_path, monkeypatch, system, preferred, alternate,
):
    monkeypatch.setattr(build, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(build.platform, "system", lambda: system)
    assert build._default_python_executable() == sys.executable
    other = tmp_path / ".venv" / alternate
    other.parent.mkdir(parents=True)
    other.write_bytes(b"python")
    other.chmod(0o700)
    assert build._default_python_executable() == sys.executable
    native = tmp_path / ".venv" / preferred
    native.parent.mkdir(parents=True)
    native.write_bytes(b"python")
    native.chmod(0o700)
    assert build._default_python_executable() == str(native)


@pytest.mark.parametrize("candidate", ["missing", "directory", "not_executable", "broken_link"])
def test_default_interpreter_skips_unusable_candidates(tmp_path, monkeypatch, candidate):
    monkeypatch.setattr(build, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(build.platform, "system", lambda: "Linux")
    native = tmp_path / ".venv/bin/python"
    native.parent.mkdir(parents=True)
    if candidate == "directory":
        native.mkdir()
    elif candidate == "not_executable":
        native.write_bytes(b"python")
        monkeypatch.setattr(build.os, "access", lambda path, mode: False)
    elif candidate == "broken_link":
        native.symlink_to(tmp_path / "missing-python")
    assert build._default_python_executable() == sys.executable


def test_build_subprocess_uses_selected_interpreter_and_profile(tmp_path, monkeypatch):
    package = tmp_path / "PyInstaller"
    package.mkdir()
    (package / "__init__.py").touch()
    (package / "__main__.py").write_text(
        "import json, os, platform, sys\n"
        "from pathlib import Path\n"
        "Path('facts.json').write_text(json.dumps({\n"
        " 'executable': sys.executable, 'version': list(sys.version_info[:3]),\n"
        " 'machine': platform.machine(), 'prefix': sys.prefix,\n"
        " 'argv': sys.argv[1:], 'cwd': os.getcwd(),\n"
        " 'profile': os.environ['ORPHEUS_RUNTIME_PROFILE']}))\n",
        encoding="utf-8",
    )
    spec = tmp_path / "backend.spec"
    spec.touch()
    monkeypatch.setattr(build, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(build, "SPEC_FILE", spec)
    for name, relative in (("BUILD_ROOT", "build"), ("WORK_DIR", "build/work"),
                           ("DIST_DIR", "build/dist")):
        monkeypatch.setattr(build, name, tmp_path / relative)
    monkeypatch.setenv("ORPHEUS_RUNTIME_PROFILE", "mlx")
    monkeypatch.delenv("PYTHONPATH", raising=False)

    build._run_pyinstaller(sys.executable, clean=True, runtime_profile="base")

    facts = json.loads((tmp_path / "facts.json").read_text(encoding="utf-8"))
    assert Path(facts["executable"]).resolve() == Path(sys.executable).resolve()
    assert facts["version"] == list(sys.version_info[:3])
    assert facts["machine"] == platform.machine()
    assert facts["prefix"] == sys.prefix
    assert Path(facts["cwd"]).resolve() == tmp_path.resolve()
    assert facts["profile"] == "base"
    assert facts["argv"] == [
        "--noconfirm", "--clean", "--distpath", str(tmp_path / "build/dist"),
        "--workpath", str(tmp_path / "build/work"), str(spec),
    ]
