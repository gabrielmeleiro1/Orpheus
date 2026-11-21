"""Supported, installable local runtime profiles.

This module is deliberately dependency-free: setup tooling can inspect and
validate profiles before optional ML packages have been installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import platform
import shutil
import ctypes
import os
from typing import Callable


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CUDA_CLEANER_UNAVAILABLE_REASON = "Needs an NVIDIA GPU with a CUDA 12.6 driver"


def cuda_hardware_error() -> str | None:
    """Check the installed driver without importing or installing torch."""
    reason = CUDA_CLEANER_UNAVAILABLE_REASON
    if os.environ.get("CUDA_VISIBLE_DEVICES", "").strip() == "-1":
        return reason
    try:
        driver = ctypes.CDLL("nvcuda.dll" if platform.system() == "Windows" else "libcuda.so.1")
        count = ctypes.c_int()
        version = ctypes.c_int()
        if (driver.cuInit(0) != 0 or driver.cuDeviceGetCount(ctypes.byref(count)) != 0
                or count.value < 1 or driver.cuDriverGetVersion(ctypes.byref(version)) != 0
                or version.value < 12060):
            return reason
    except (OSError, AttributeError):
        return reason
    return None


@dataclass(frozen=True)
class RuntimeProfile:
    """A vetted dependency set and its compatible model-download groups."""

    id: str
    label: str
    description: str
    requirements_file: Path
    model_groups: tuple[str, ...]
    platform_kind: str = "any"

    def is_compatible(
        self,
        *,
        system: str | None = None,
        machine: str | None = None,
        os_version: str | None = None,
    ) -> bool:
        """Return whether this profile can run on the specified host platform."""
        normalized_system = (system or platform.system()).lower()
        normalized_machine = (machine or platform.machine()).lower()
        if self.platform_kind == "any":
            return True
        if self.platform_kind == "apple-silicon":
            if normalized_system != "darwin" or normalized_machine != "arm64":
                return False
            version = os_version if os_version is not None else platform.mac_ver()[0]
            return _macos_major_version(version) >= 14
        if self.platform_kind == "nvidia-cuda":
            return normalized_system in {"linux", "windows"}
        raise ValueError(f"Unknown platform kind: {self.platform_kind}")

    def incompatibility_reason(
        self,
        *,
        system: str | None = None,
        machine: str | None = None,
        os_version: str | None = None,
    ) -> str | None:
        """Explain why this profile is unavailable on the current host."""
        if self.is_compatible(system=system, machine=machine, os_version=os_version):
            return None
        if self.platform_kind == "apple-silicon":
            normalized_system = (system or platform.system()).lower()
            normalized_machine = (machine or platform.machine()).lower()
            if normalized_system == "darwin" and normalized_machine == "arm64":
                return "MLX requires macOS 14 or newer."
            return "MLX requires macOS 14 or newer on an arm64 Apple Silicon Mac."
        if self.platform_kind == "nvidia-cuda":
            return (
                "CUDA selection requires Linux or Windows; actual readiness also "
                "requires a supported NVIDIA GPU and driver probe."
            )
        return "This runtime is unavailable on the current platform."


_REQUIREMENTS_DIR = PROJECT_ROOT / "requirements"


def _macos_major_version(version: str) -> int:
    """Parse the macOS major version, treating missing/malformed values as unsupported."""
    try:
        return int(str(version).split(".", 1)[0])
    except (TypeError, ValueError):
        return 0


_RUNTIME_PROFILES = (
    RuntimeProfile(
        id="base",
        label="Library only",
        description=(
            "Library, playback, and source inspection. Converting requires a local "
            "voice runtime — you can add one later from Settings."
        ),
        requirements_file=_REQUIREMENTS_DIR / "base.txt",
        model_groups=(),
    ),
    RuntimeProfile(
        id="mlx",
        label="Apple Silicon (MLX)",
        description="Private local TTS and cleaning for macOS 14+ on arm64 Apple Silicon.",
        requirements_file=_REQUIREMENTS_DIR / "mlx.txt",
        model_groups=("tts-mlx", "cleaner-mlx", "cleaner-mlx-small"),
        platform_kind="apple-silicon",
    ),
    RuntimeProfile(
        id="cuda",
        label="NVIDIA CUDA",
        description=(
            "Local narration on CPU or NVIDIA GPU for Linux/Windows. Local text "
            "cleaners require an NVIDIA GPU with a CUDA 12.6 driver."
        ),
        requirements_file=_REQUIREMENTS_DIR / "cuda.txt",
        model_groups=("tts-cpu-cuda", "cleaner-cuda", "cleaner-cuda-large"),
        platform_kind="nvidia-cuda",
    ),
)


def runtime_profiles() -> tuple[RuntimeProfile, ...]:
    """Return all known profiles in the order shown to users."""
    return _RUNTIME_PROFILES


def get_runtime_profile(profile_id: str) -> RuntimeProfile:
    """Get a profile by its stable identifier."""
    normalized_id = str(profile_id or "").strip().lower()
    for profile in _RUNTIME_PROFILES:
        if profile.id == normalized_id:
            return profile
    raise ValueError(f"Unknown runtime profile: {profile_id!r}")


def recommended_profile_id(
    *,
    system: str | None = None,
    machine: str | None = None,
    os_version: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> str:
    """Recommend a runtime profile from the host platform alone.

    This is a pure hardware/OS hint independent of any install state:
    ``mlx`` on compatible Apple Silicon, ``cuda`` on Windows (including CPU
    narration) or Linux with ``nvidia-smi`` on PATH, otherwise
    ``base``. It never spawns a subprocess; ``nvidia-smi`` is only located,
    never executed.
    """
    mlx = get_runtime_profile("mlx")
    if mlx.is_compatible(system=system, machine=machine, os_version=os_version):
        return "mlx"
    cuda = get_runtime_profile("cuda")
    if (
        cuda.is_compatible(system=system, machine=machine, os_version=os_version)
        and ((system or platform.system()).lower() == "windows" or which("nvidia-smi") is not None)
    ):
        return "cuda"
    return "base"
