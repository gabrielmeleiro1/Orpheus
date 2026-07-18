"""Supported, installable local runtime profiles.

This module is deliberately dependency-free: setup tooling can inspect and
validate profiles before optional ML packages have been installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import platform
import shutil
from typing import Callable


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class RuntimeProfile:
    """A vetted dependency set and its compatible model-download groups."""

    id: str
    label: str
    description: str
    requirements_file: Path
    model_groups: tuple[str, ...]
    platform_kind: str = "any"
    risk: str | None = None

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
        description="Document conversion, playback, and cloud cleaning without local ML models.",
        requirements_file=_REQUIREMENTS_DIR / "base.txt",
        model_groups=(),
    ),
    RuntimeProfile(
        id="mlx",
        label="Apple Silicon (MLX)",
        description="Private local TTS and cleaning for macOS 14+ on arm64 Apple Silicon.",
        requirements_file=_REQUIREMENTS_DIR / "mlx.txt",
        model_groups=("tts-mlx", "cleaner-mlx"),
        platform_kind="apple-silicon",
    ),
    RuntimeProfile(
        id="cuda",
        label="NVIDIA CUDA",
        description=(
            "Opt-in local TTS and cleaning for Linux/Windows; readiness requires "
            "a supported NVIDIA GPU and driver probe."
        ),
        requirements_file=_REQUIREMENTS_DIR / "cuda.txt",
        model_groups=("tts-cpu-cuda", "cleaner-cuda"),
        platform_kind="nvidia-cuda",
        risk=None,
    ),
)


def runtime_profiles() -> tuple[RuntimeProfile, ...]:
    """Return all known profiles in the order shown to users."""
    return _RUNTIME_PROFILES


def compatible_runtime_profiles() -> tuple[RuntimeProfile, ...]:
    """Return profiles supported by the current operating system and CPU."""
    return tuple(profile for profile in _RUNTIME_PROFILES if profile.is_compatible())


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
    ``mlx`` on compatible Apple Silicon, ``cuda`` on Linux/Windows when an
    NVIDIA GPU is plausibly present (``nvidia-smi`` on PATH), otherwise
    ``base``. It never spawns a subprocess; ``nvidia-smi`` is only located,
    never executed.
    """
    mlx = get_runtime_profile("mlx")
    if mlx.is_compatible(system=system, machine=machine, os_version=os_version):
        return "mlx"
    cuda = get_runtime_profile("cuda")
    if (
        cuda.is_compatible(system=system, machine=machine, os_version=os_version)
        and which("nvidia-smi") is not None
    ):
        return "cuda"
    return "base"
