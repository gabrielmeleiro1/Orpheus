#!/usr/bin/env python3
"""Build the Python desktop backend sidecar for Tauri packaging."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.audio.ffmpeg import resolve_ffmpeg_binary  # noqa: E402

DESKTOP_DIR = PROJECT_ROOT / "desktop"
TAURI_DIR = DESKTOP_DIR / "src-tauri"
TAURI_BINARIES_DIR = TAURI_DIR / "binaries"
SIDE_CAR_STEM = "orpheus-desktop-backend"
TAURI_SIDECAR_RESOURCE_DIR = TAURI_BINARIES_DIR / SIDE_CAR_STEM
SPEC_FILE = DESKTOP_DIR / "backend-sidecar.spec"
BUILD_ROOT = TAURI_DIR / "target" / "pyinstaller-sidecar"
WORK_DIR = BUILD_ROOT / "work"
DIST_DIR = BUILD_ROOT / "dist"

BASE_REQUIRED_MODULES = (
    "fastapi",
    "uvicorn",
    "numpy",
    "soundfile",
    "pydub",
    "scipy",
)
LOCAL_TTS_REQUIRED_MODULES = (
    "en_core_web_sm",
    "spacy",
    "thinc",
)
MLX_REQUIRED_MODULES = (
    "mlx",
    "mlx_lm",
    "mlx_audio",
    "transformers",
    "misaki",
    "espeakng_loader",
    "num2words",
    "phonemizer",
    "language_tags",
)
CUDA_REQUIRED_MODULES = (
    "torch",
    "transformers",
    "kokoro",
    "bitsandbytes",
    "misaki",
    "espeakng_loader",
    "language_tags",
)
REQUIRED_BINARIES = ("ffmpeg", "ffprobe")
PHONEMIZER_REQUIRED_PYINSTALLER_RESOURCES = (
    Path("_internal") / "espeakng_loader" / "espeak-ng-data" / "phontab",
)
DARWIN_REQUIRED_PYINSTALLER_RESOURCES = (Path("_internal") / "mlx" / "lib" / "mlx.metallib",)
REQUIREMENTS_MANIFEST_PYINSTALLER_RESOURCES = tuple(
    Path("_internal") / "requirements" / f"{name}.txt" for name in ("base", "mlx", "cuda")
)
MLX_CORE_EXTENSION = Path("_internal") / "mlx" / "core.cpython-312-darwin.so"
MLX_ROOT_DYLIB = Path("_internal") / "libmlx.dylib"
MLX_NESTED_DYLIB = Path("_internal") / "mlx" / "lib" / "libmlx.dylib"
ESPEAK_DATA_DIR = Path("_internal") / "espeakng_loader" / "espeak-ng-data"


def _default_python_executable() -> str:
    posix_python = PROJECT_ROOT / ".venv" / "bin" / "python"
    windows_python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    candidates = (
        (windows_python, posix_python)
        if platform.system().lower() == "windows"
        else (posix_python, windows_python)
    )
    for repo_python in candidates:
        if repo_python.is_file():
            return str(repo_python)
    return sys.executable


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the desktop backend sidecar executable for Tauri.",
    )
    parser.add_argument(
        "--target-triple",
        default="",
        help="Explicit target triple (for example: aarch64-apple-darwin).",
    )
    parser.add_argument(
        "--runtime-profile",
        choices=("base", "mlx", "cuda"),
        default=None,
        help=(
            "Runtime bundled into the sidecar. Explicit CLI selection overrides "
            "ORPHEUS_RUNTIME_PROFILE; otherwise the environment value is used, "
            "then mlx for Apple Silicon targets or base elsewhere."
        ),
    )
    parser.add_argument(
        "--python",
        default=_default_python_executable(),
        help="Python executable to use for the PyInstaller build.",
    )
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="Do not remove previous pyinstaller work/dist directories before building.",
    )
    parser.add_argument(
        "--validate-existing",
        action="store_true",
        help="Validate the existing Tauri sidecar resource without rebuilding it.",
    )
    return parser.parse_args()


def _resolve_target_triple(explicit_target: str) -> str:
    if explicit_target.strip():
        return explicit_target.strip()

    for key in ("TAURI_ENV_TARGET_TRIPLE", "CARGO_BUILD_TARGET", "TARGET"):
        value = os.getenv(key, "").strip()
        if value:
            return value

    rustc = shutil.which("rustc")
    if rustc:
        result = subprocess.run(
            [rustc, "-vV"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith("host:"):
                    host = line.split(":", 1)[1].strip()
                    if host:
                        return host

    machine = platform.machine().lower()
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}.get(
        machine, machine
    )
    system = platform.system().lower()
    if system == "darwin":
        return f"{arch}-apple-darwin"
    if system == "windows":
        return f"{arch}-pc-windows-msvc"
    if system == "linux":
        libc_name = platform.libc_ver()[0].lower()
        libc = "musl" if "musl" in libc_name else "gnu"
        return f"{arch}-unknown-linux-{libc}"
    raise RuntimeError(
        "Unable to detect target triple automatically. Pass --target-triple explicitly."
    )


def _is_windows_target(target_triple: str) -> bool:
    return "windows" in target_triple.lower()


def _is_darwin_target(target_triple: str) -> bool:
    return "apple-darwin" in target_triple.lower()


def _is_apple_silicon_target(target_triple: str) -> bool:
    normalized = target_triple.lower()
    return _target_architecture(target_triple) == "aarch64" and "apple-darwin" in normalized


def _target_os(target_triple: str) -> str:
    normalized = target_triple.lower()
    if "apple-darwin" in normalized:
        return "darwin"
    if "windows" in normalized:
        return "windows"
    if "linux" in normalized:
        return "linux"
    return "unknown"


def _host_os() -> str:
    system = platform.system().lower()
    if system in {"darwin", "windows", "linux"}:
        return system
    return "unknown"


def _normalize_architecture(value: str) -> str:
    """Normalize common host/target architecture aliases."""
    normalized = value.strip().lower().replace("-", "_")
    return {
        "amd64": "x86_64",
        "x64": "x86_64",
        "x86_64": "x86_64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
    }.get(normalized, normalized)


def _target_architecture(target_triple: str) -> str:
    return _normalize_architecture(target_triple.split("-", 1)[0])


def _validate_pyinstaller_target_matches_host(target_triple: str) -> None:
    target_os = _target_os(target_triple)
    host_os = _host_os()
    if target_os != "unknown" and host_os != "unknown" and target_os != host_os:
        raise RuntimeError(
            "Cross-OS PyInstaller builds are not supported.\n"
            f"Host OS: {host_os}\n"
            f"Target triple: {target_triple}\n"
            "Run this script on the target operating system or pass a same-OS target triple."
        )

    host_architecture = _normalize_architecture(platform.machine())
    target_architecture = _target_architecture(target_triple)
    if (
        host_architecture
        and target_architecture
        and host_architecture not in {"unknown", ""}
        and target_architecture not in {"unknown", ""}
        and host_architecture != target_architecture
    ):
        raise RuntimeError(
            "Host/target architecture mismatch: PyInstaller builds must run on the target architecture.\n"
            f"Host architecture: {host_architecture}\n"
            f"Target architecture: {target_architecture}\n"
            f"Target triple: {target_triple}\n"
            "Run this script on a matching host architecture or pass a same-architecture target triple."
        )


def _validate_runtime_profile_host(target_triple: str, runtime_profile: str) -> None:
    """Reject a selected profile when the build host cannot support it."""
    if runtime_profile != "mlx":
        return
    version = platform.mac_ver()[0]
    try:
        major_version = int(version.split(".", 1)[0])
    except (AttributeError, ValueError):
        major_version = 0
    if major_version < 14:
        raise RuntimeError(
            "The MLX runtime profile requires macOS 14 or newer. "
            f"Detected: {version or 'unknown'} (target {target_triple})."
        )


def default_runtime_profile(target_triple: str) -> str:
    """Return the safe packaging default for a target."""
    return "mlx" if _is_apple_silicon_target(target_triple) else "base"


def _resolve_runtime_profile(target_triple: str, runtime_profile: str | None) -> str:
    environment_profile = ""
    if runtime_profile is None:
        environment_profile = os.getenv("ORPHEUS_RUNTIME_PROFILE", "").strip()
    selected_profile = (
        runtime_profile
        if runtime_profile is not None
        else environment_profile or default_runtime_profile(target_triple)
    )
    profile = selected_profile.strip().lower()
    if profile not in {"base", "mlx", "cuda"}:
        if environment_profile:
            raise RuntimeError(
                "Invalid ORPHEUS_RUNTIME_PROFILE "
                f"{environment_profile!r}; expected one of: base, mlx, cuda."
            )
        raise RuntimeError(f"Unknown runtime profile: {runtime_profile!r}")
    if profile == "mlx" and not _is_apple_silicon_target(target_triple):
        raise RuntimeError("The MLX runtime profile requires an aarch64 Apple Darwin target.")
    if profile == "cuda" and _target_os(target_triple) not in {"linux", "windows"}:
        raise RuntimeError("The CUDA runtime profile requires a Linux or Windows target.")
    return profile


def required_modules(
    target_triple: str,
    *,
    runtime_profile: str | None = None,
) -> tuple[str, ...]:
    profile = _resolve_runtime_profile(target_triple, runtime_profile)
    modules = list(BASE_REQUIRED_MODULES)
    if profile == "mlx":
        modules.extend(LOCAL_TTS_REQUIRED_MODULES)
        modules.extend(MLX_REQUIRED_MODULES)
    elif profile == "cuda":
        modules.extend(LOCAL_TTS_REQUIRED_MODULES)
        modules.extend(CUDA_REQUIRED_MODULES)
    return tuple(dict.fromkeys(modules))


def required_sidecar_resources(
    target_triple: str,
    *,
    runtime_profile: str | None = None,
) -> tuple[Path, ...]:
    profile = _resolve_runtime_profile(target_triple, runtime_profile)
    resources: list[Path] = []
    resources.extend(REQUIREMENTS_MANIFEST_PYINSTALLER_RESOURCES)
    if profile in {"mlx", "cuda"}:
        resources.extend(PHONEMIZER_REQUIRED_PYINSTALLER_RESOURCES)
    if profile == "mlx":
        resources.extend(DARWIN_REQUIRED_PYINSTALLER_RESOURCES)
    suffix = ".exe" if _is_windows_target(target_triple) else ""
    resources.extend(Path("bin") / f"{binary}{suffix}" for binary in REQUIRED_BINARIES)
    return tuple(resources)


def _resolve_required_binary_paths() -> tuple[dict[str, Path], list[str]]:
    resolved_bins: dict[str, Path] = {}
    missing_bins: list[str] = []
    for binary in REQUIRED_BINARIES:
        resolved = resolve_ffmpeg_binary(binary)
        if resolved is None:
            missing_bins.append(binary)
        else:
            resolved_bins[binary] = Path(resolved)
    return resolved_bins, missing_bins


def _validate_runtime_dependencies(
    python_executable: str,
    *,
    target_triple: str | None = None,
    runtime_profile: str | None = None,
) -> dict[str, Path]:
    resolved_target = target_triple or _resolve_target_triple("")
    _validate_pyinstaller_target_matches_host(resolved_target)
    resolved_profile = _resolve_runtime_profile(resolved_target, runtime_profile)
    _validate_runtime_profile_host(resolved_target, resolved_profile)
    pyinstaller_check = subprocess.run(
        [python_executable, "-m", "PyInstaller", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if pyinstaller_check.returncode != 0:
        stderr = pyinstaller_check.stderr.strip() or pyinstaller_check.stdout.strip()
        raise RuntimeError(
            "PyInstaller is not available in the selected Python environment.\n"
            f"Python: {python_executable}\n"
            "Install it with:\n"
            f"  {python_executable} -m pip install pyinstaller\n"
            f"Details: {stderr}"
        )

    missing_modules: list[str] = []
    for module in required_modules(resolved_target, runtime_profile=resolved_profile):
        module_check = subprocess.run(
            [
                python_executable,
                "-c",
                "import importlib,sys; importlib.import_module(sys.argv[1])",
                module,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if module_check.returncode != 0:
            detail = module_check.stderr.strip() or module_check.stdout.strip() or "import failed"
            missing_modules.append(f"{module}: {detail}")

    resolved_bins, missing_bins = _resolve_required_binary_paths()

    errors: list[str] = []
    if missing_modules:
        errors.append("Missing required Python modules:")
        errors.extend(f"  - {entry}" for entry in missing_modules)
    if missing_bins:
        errors.append("Missing required system binaries:")
        errors.extend(f"  - {entry}" for entry in missing_bins)

    if errors:
        raise RuntimeError(
            "Desktop sidecar build prerequisites are not satisfied.\n"
            + "\n".join(errors)
            + "\nInstall the missing dependencies, then run this script again."
        )
    return resolved_bins


def _run_pyinstaller(
    python_executable: str,
    *,
    clean: bool,
    runtime_profile: str,
) -> None:
    if not SPEC_FILE.is_file():
        raise RuntimeError(f"Missing PyInstaller spec file: {SPEC_FILE}")

    if clean and BUILD_ROOT.exists():
        shutil.rmtree(BUILD_ROOT)

    BUILD_ROOT.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    command = [
        python_executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(WORK_DIR),
        str(SPEC_FILE),
    ]
    if clean:
        command.insert(4, "--clean")
    env = os.environ.copy()
    env["ORPHEUS_RUNTIME_PROFILE"] = runtime_profile
    subprocess.run(command, cwd=PROJECT_ROOT, env=env, check=True)


def _validate_sidecar_resources(
    sidecar_dir: Path,
    *,
    target_triple: str | None = None,
    runtime_profile: str | None = None,
) -> None:
    resolved_target = target_triple or _resolve_target_triple("")
    missing = [
        resource
        for resource in required_sidecar_resources(
            resolved_target,
            runtime_profile=runtime_profile,
        )
        if not (sidecar_dir / resource).is_file()
    ]
    if missing:
        formatted = "\n".join(f"  - {resource.as_posix()}" for resource in missing)
        raise RuntimeError(
            "PyInstaller sidecar is missing required runtime resources:\n"
            f"{formatted}\n"
            "Rebuild with a spec that collects platform runtime package data."
        )


def validate_existing_sidecar(
    *,
    target_triple: str | None = None,
    runtime_profile: str | None = None,
) -> Path:
    """Validate the sidecar required by ``tauri build`` and return its executable."""
    resolved_target = target_triple or _resolve_target_triple("")
    executable_name = f"{SIDE_CAR_STEM}.exe" if _is_windows_target(resolved_target) else SIDE_CAR_STEM
    executable = TAURI_SIDECAR_RESOURCE_DIR / executable_name
    if not executable.is_file() or (
        not _is_windows_target(resolved_target) and not os.access(executable, os.X_OK)
    ):
        raise RuntimeError(
            "The packaged desktop backend sidecar is missing or not executable. "
            "Build it directly or run the Tauri release build:\n"
            "  python scripts/build_desktop_backend.py --runtime-profile base|mlx|cuda\n"
            "  (from desktop/) npm run desktop:build"
        )

    # Without an explicit profile, validate the resources common to every
    # package. This keeps an intentionally built base sidecar valid on an MLX
    # host while still preventing an empty/broken Tauri bundle.
    _validate_sidecar_resources(
        TAURI_SIDECAR_RESOURCE_DIR,
        target_triple=resolved_target,
        runtime_profile=runtime_profile or "base",
    )
    return executable


def _copy_ffmpeg_binaries(
    sidecar_dir: Path,
    ffmpeg_binaries: Mapping[str, Path],
    *,
    target_triple: str,
) -> None:
    bin_dir = sidecar_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    for binary_name in REQUIRED_BINARIES:
        source = Path(ffmpeg_binaries[binary_name])
        if not source.is_file() or not os.access(source, os.X_OK):
            raise RuntimeError(f"Required runtime binary is not executable: {source}")
        destination_name = (
            f"{binary_name}.exe" if _is_windows_target(target_triple) else binary_name
        )
        destination = bin_dir / destination_name
        shutil.copy2(source, destination)
        # Tauri must mutate/sign bundled Mach-O resources during packaging.
        destination.chmod(destination.stat().st_mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


def _macho_rpaths(binary_path: Path) -> list[str]:
    completed = subprocess.run(
        ["otool", "-l", str(binary_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    rpaths: list[str] = []
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("path "):
            rpaths.append(stripped.split()[1])
    return rpaths


def _normalize_mlx_dylib_layout(sidecar_dir: Path) -> None:
    if platform.system() != "Darwin":
        return

    core_extension = sidecar_dir / MLX_CORE_EXTENSION
    root_dylib = sidecar_dir / MLX_ROOT_DYLIB
    nested_dylib = sidecar_dir / MLX_NESTED_DYLIB
    if not core_extension.is_file() or not nested_dylib.is_file():
        return

    rpaths = _macho_rpaths(core_extension)
    changed = False
    if "@loader_path/.." in rpaths:
        subprocess.run(
            ["install_name_tool", "-delete_rpath", "@loader_path/..", str(core_extension)],
            check=True,
        )
        changed = True
    if "@loader_path/lib" not in rpaths:
        subprocess.run(
            ["install_name_tool", "-add_rpath", "@loader_path/lib", str(core_extension)],
            check=True,
        )
        changed = True
    if changed:
        subprocess.run(["codesign", "--force", "--sign", "-", str(core_extension)], check=True)

    root_dylib.unlink(missing_ok=True)


def _expose_espeak_data_at_sidecar_root(sidecar_dir: Path) -> None:
    """Expose espeak-ng data where the bundled dylib resolves it in a macOS app."""
    data_dir = sidecar_dir / ESPEAK_DATA_DIR
    if not data_dir.is_dir():
        return

    for data_item in data_dir.iterdir():
        target = sidecar_dir / data_item.name
        if target.exists() or target.is_symlink():
            if target.is_symlink() and target.readlink() == ESPEAK_DATA_DIR / data_item.name:
                continue
            raise RuntimeError(
                "Cannot expose espeak-ng data at sidecar root because a "
                f"conflicting path already exists: {target}"
            )
        if data_item.is_dir():
            shutil.copytree(data_item, target, symlinks=True)
        else:
            try:
                target.symlink_to(ESPEAK_DATA_DIR / data_item.name)
            except OSError:
                shutil.copy2(data_item, target)


def _copy_sidecar_for_tauri(
    target_triple: str,
    *,
    ffmpeg_binaries: Mapping[str, Path] | None = None,
    runtime_profile: str | None = None,
) -> Path:
    pyinstaller_binary_name = (
        f"{SIDE_CAR_STEM}.exe" if _is_windows_target(target_triple) else SIDE_CAR_STEM
    )
    built_sidecar_dir = DIST_DIR / SIDE_CAR_STEM
    built_binary = built_sidecar_dir / pyinstaller_binary_name
    if not built_binary.is_file():
        raise RuntimeError(f"PyInstaller did not produce expected binary: {built_binary}")

    resolved_ffmpeg_binaries = (
        dict(ffmpeg_binaries)
        if ffmpeg_binaries is not None
        else _resolve_required_binary_paths()[0]
    )
    missing_ffmpeg_binaries = [
        binary for binary in REQUIRED_BINARIES if binary not in resolved_ffmpeg_binaries
    ]
    if missing_ffmpeg_binaries:
        formatted = "\n".join(f"  - {binary}" for binary in missing_ffmpeg_binaries)
        raise RuntimeError(f"Missing required runtime binaries:\n{formatted}")

    TAURI_BINARIES_DIR.mkdir(parents=True, exist_ok=True)
    if TAURI_SIDECAR_RESOURCE_DIR.exists():
        shutil.rmtree(TAURI_SIDECAR_RESOURCE_DIR)
    shutil.copytree(built_sidecar_dir, TAURI_SIDECAR_RESOURCE_DIR, symlinks=True)
    _copy_ffmpeg_binaries(
        TAURI_SIDECAR_RESOURCE_DIR,
        resolved_ffmpeg_binaries,
        target_triple=target_triple,
    )
    resolved_profile = _resolve_runtime_profile(target_triple, runtime_profile)
    if resolved_profile == "mlx":
        _normalize_mlx_dylib_layout(TAURI_SIDECAR_RESOURCE_DIR)
    if resolved_profile in {"mlx", "cuda"}:
        _expose_espeak_data_at_sidecar_root(TAURI_SIDECAR_RESOURCE_DIR)
    _validate_sidecar_resources(
        TAURI_SIDECAR_RESOURCE_DIR,
        target_triple=target_triple,
        runtime_profile=resolved_profile,
    )
    for stale_sidecar in TAURI_BINARIES_DIR.glob(f"{SIDE_CAR_STEM}-*"):
        if stale_sidecar.is_symlink() or stale_sidecar.is_file():
            stale_sidecar.unlink()
        elif stale_sidecar.is_dir():
            shutil.rmtree(stale_sidecar)

    output_binary = TAURI_SIDECAR_RESOURCE_DIR / pyinstaller_binary_name
    if not _is_windows_target(target_triple):
        output_binary.chmod(
            output_binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )

    return output_binary


def main() -> int:
    args = _parse_args()
    target_triple = _resolve_target_triple(args.target_triple)
    if args.validate_existing:
        output_binary = validate_existing_sidecar(
            target_triple=target_triple,
            runtime_profile=args.runtime_profile,
        )
        print(f"Validated Tauri sidecar resource: {output_binary.parent}")
        print(f"Tauri sidecar executable: {output_binary}")
        return 0

    _validate_pyinstaller_target_matches_host(target_triple)
    runtime_profile = _resolve_runtime_profile(target_triple, args.runtime_profile)
    ffmpeg_binaries = _validate_runtime_dependencies(
        args.python,
        target_triple=target_triple,
        runtime_profile=runtime_profile,
    )
    _run_pyinstaller(
        args.python,
        clean=not args.no_clean,
        runtime_profile=runtime_profile,
    )
    output_binary = _copy_sidecar_for_tauri(
        target_triple,
        ffmpeg_binaries=ffmpeg_binaries,
        runtime_profile=runtime_profile,
    )

    print(f"Built desktop backend sidecar for target: {target_triple}")
    print(f"Runtime profile: {runtime_profile}")
    print(f"Tauri sidecar resource: {output_binary.parent}")
    print(f"Tauri sidecar executable: {output_binary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
