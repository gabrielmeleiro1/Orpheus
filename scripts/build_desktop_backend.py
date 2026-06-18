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

COMMON_REQUIRED_MODULES = (
    "fastapi",
    "uvicorn",
    "numpy",
    "soundfile",
    "pydub",
    "en_core_web_sm",
    "scipy",
    "spacy",
    "thinc",
    "torch",
    "transformers",
    "kokoro",
    "espeakng_loader",
)
DARWIN_REQUIRED_MODULES = (
    "mlx",
    "mlx_lm",
    "mlx_audio",
)
REQUIRED_MODULES = (*COMMON_REQUIRED_MODULES, *DARWIN_REQUIRED_MODULES)

REQUIRED_BINARIES = ("ffmpeg", "ffprobe")
COMMON_REQUIRED_PYINSTALLER_RESOURCES = (
    Path("_internal") / "espeakng_loader" / "espeak-ng-data" / "phontab",
)
DARWIN_REQUIRED_PYINSTALLER_RESOURCES = (
    Path("_internal") / "mlx" / "lib" / "mlx.metallib",
)
REQUIRED_SIDECAR_RESOURCES = (
    *COMMON_REQUIRED_PYINSTALLER_RESOURCES,
    *DARWIN_REQUIRED_PYINSTALLER_RESOURCES,
    Path("bin") / "ffmpeg",
    Path("bin") / "ffprobe",
)
MLX_CORE_EXTENSION = Path("_internal") / "mlx" / "core.cpython-312-darwin.so"
MLX_ROOT_DYLIB = Path("_internal") / "libmlx.dylib"
MLX_NESTED_DYLIB = Path("_internal") / "mlx" / "lib" / "libmlx.dylib"
ESPEAK_DATA_DIR = Path("_internal") / "espeakng_loader" / "espeak-ng-data"


def _default_python_executable() -> str:
    repo_python = PROJECT_ROOT / "venv" / "bin" / "python"
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
        "--python",
        default=_default_python_executable(),
        help="Python executable to use for the PyInstaller build.",
    )
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="Do not remove previous pyinstaller work/dist directories before building.",
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


def required_modules(target_triple: str) -> tuple[str, ...]:
    modules = list(COMMON_REQUIRED_MODULES)
    if _is_darwin_target(target_triple):
        modules.extend(DARWIN_REQUIRED_MODULES)
    return tuple(dict.fromkeys(modules))


def required_sidecar_resources(target_triple: str) -> tuple[Path, ...]:
    resources = list(COMMON_REQUIRED_PYINSTALLER_RESOURCES)
    if _is_darwin_target(target_triple):
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
) -> dict[str, Path]:
    resolved_target = target_triple or _resolve_target_triple("")
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
    for module in required_modules(resolved_target):
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


def _run_pyinstaller(python_executable: str, *, clean: bool) -> None:
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
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)


def _validate_sidecar_resources(
    sidecar_dir: Path,
    *,
    target_triple: str | None = None,
) -> None:
    resolved_target = target_triple or _resolve_target_triple("")
    missing = [
        resource
        for resource in required_sidecar_resources(resolved_target)
        if not (sidecar_dir / resource).is_file()
    ]
    if missing:
        formatted = "\n".join(f"  - {resource.as_posix()}" for resource in missing)
        raise RuntimeError(
            "PyInstaller sidecar is missing required runtime resources:\n"
            f"{formatted}\n"
            "Rebuild with a spec that collects platform runtime package data."
        )


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
        destination_name = f"{binary_name}.exe" if _is_windows_target(target_triple) else binary_name
        destination = bin_dir / destination_name
        shutil.copy2(source, destination)


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
) -> Path:
    pyinstaller_binary_name = f"{SIDE_CAR_STEM}.exe" if _is_windows_target(target_triple) else SIDE_CAR_STEM
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

    legacy_tauri_name = (
        f"{SIDE_CAR_STEM}-{target_triple}.exe"
        if _is_windows_target(target_triple)
        else f"{SIDE_CAR_STEM}-{target_triple}"
    )
    legacy_output_binary = TAURI_BINARIES_DIR / legacy_tauri_name
    TAURI_BINARIES_DIR.mkdir(parents=True, exist_ok=True)
    if TAURI_SIDECAR_RESOURCE_DIR.exists():
        shutil.rmtree(TAURI_SIDECAR_RESOURCE_DIR)
    shutil.copytree(built_sidecar_dir, TAURI_SIDECAR_RESOURCE_DIR, symlinks=True)
    _copy_ffmpeg_binaries(
        TAURI_SIDECAR_RESOURCE_DIR,
        resolved_ffmpeg_binaries,
        target_triple=target_triple,
    )
    _normalize_mlx_dylib_layout(TAURI_SIDECAR_RESOURCE_DIR)
    _expose_espeak_data_at_sidecar_root(TAURI_SIDECAR_RESOURCE_DIR)
    _validate_sidecar_resources(TAURI_SIDECAR_RESOURCE_DIR, target_triple=target_triple)
    legacy_output_binary.unlink(missing_ok=True)
    for stale_binary in TAURI_BINARIES_DIR.glob(f"{SIDE_CAR_STEM}-*"):
        if stale_binary.is_file():
            stale_binary.unlink()

    output_binary = TAURI_SIDECAR_RESOURCE_DIR / pyinstaller_binary_name
    if not _is_windows_target(target_triple):
        output_binary.chmod(output_binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    return output_binary


def main() -> int:
    args = _parse_args()
    target_triple = _resolve_target_triple(args.target_triple)
    ffmpeg_binaries = _validate_runtime_dependencies(args.python, target_triple=target_triple)
    _run_pyinstaller(args.python, clean=not args.no_clean)
    output_binary = _copy_sidecar_for_tauri(
        target_triple,
        ffmpeg_binaries=ffmpeg_binaries,
    )

    print(f"Built desktop backend sidecar for target: {target_triple}")
    print(f"Tauri sidecar resource: {output_binary.parent}")
    print(f"Tauri sidecar executable: {output_binary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
