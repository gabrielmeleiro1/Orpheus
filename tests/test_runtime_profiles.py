"""Runtime dependency profile contract tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from modules import runtime_profiles
from modules.tts.cleaner_mlx import mlx_lm_runtime_error_message
from modules.tts.mlx_compat import mlx_runtime_error_message


@pytest.mark.parametrize(
    ("system", "machine", "macos_version", "expected_ids"),
    [
        ("Darwin", "arm64", "14.0", ("base", "mlx")),
        ("Linux", "x86_64", "", ("base", "cuda")),
        ("Windows", "AMD64", "", ("base", "cuda")),
        ("Darwin", "x86_64", "14.0", ("base",)),
    ],
)
def test_compatible_profiles_are_platform_specific(
    monkeypatch: pytest.MonkeyPatch,
    system: str,
    machine: str,
    macos_version: str,
    expected_ids: tuple[str, ...],
) -> None:
    monkeypatch.setattr(runtime_profiles.platform, "system", lambda: system)
    monkeypatch.setattr(runtime_profiles.platform, "machine", lambda: machine)
    monkeypatch.setattr(
        runtime_profiles.platform,
        "mac_ver",
        lambda: (macos_version, ("", "", ""), ""),
    )

    assert (
        tuple(profile.id for profile in runtime_profiles.compatible_runtime_profiles())
        == expected_ids
    )


def test_base_manifest_stays_free_of_local_ml_runtimes() -> None:
    base_manifest = Path(runtime_profiles.PROJECT_ROOT / "requirements" / "base.txt")
    contents = base_manifest.read_text(encoding="utf-8").lower()

    assert "torch" not in contents
    assert "bitsandbytes" not in contents
    assert "mlx" not in contents
    assert "spacy" not in contents
    assert "thinc" not in contents
    assert "en-core-web-sm" not in contents


def test_local_profiles_pin_hashed_spacy_model_artifact() -> None:
    expected = (
        "en-core-web-sm @ https://github.com/explosion/spacy-models/releases/download/"
        "en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
        "#sha256=1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85"
    )
    for profile in ("mlx", "cuda"):
        contents = (runtime_profiles.PROJECT_ROOT / "requirements" / f"{profile}.txt").read_text(
            encoding="utf-8"
        )
        assert "spacy==3.8.11" in contents
        assert "thinc==8.3.10" in contents
        assert expected in contents


def test_mlx_manifest_has_torch_free_kokoro_phonemization_dependencies() -> None:
    manifest = Path(runtime_profiles.PROJECT_ROOT / "requirements" / "mlx.txt")
    contents = manifest.read_text(encoding="utf-8").lower()
    requirement_lines = tuple(
        line.strip()
        for line in contents.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )

    for dependency in (
        "transformers==5.12.1",
        "misaki==0.9.4",
        "espeakng-loader==0.2.4",
        "num2words==0.5.14",
        "phonemizer-fork==3.3.2",
        "language-tags==1.2.0",
    ):
        assert dependency in contents
    assert not any(line.startswith("misaki[en]") for line in requirement_lines)
    assert not any(line.startswith("torch") for line in requirement_lines)
    assert not any(line.startswith("bitsandbytes") for line in requirement_lines)


def test_cuda_manifest_uses_patched_pytorch_for_fixed_advisory() -> None:
    manifest = runtime_profiles.PROJECT_ROOT / "requirements" / "cuda.txt"
    contents = manifest.read_text(encoding="utf-8")

    assert "torch==2.13.0" in contents
    assert "torch==2.10.0" not in contents


def test_mlx_requires_arm64_and_macos_14_or_newer() -> None:
    profile = runtime_profiles.get_runtime_profile("mlx")

    assert profile.is_compatible(system="Darwin", machine="arm64", os_version="14.0")
    assert not profile.is_compatible(system="Darwin", machine="aarch64", os_version="14.0")
    assert not profile.is_compatible(system="Darwin", machine="arm64", os_version="13.6.9")
    assert "macOS 14" in (
        profile.incompatibility_reason(system="Darwin", machine="arm64", os_version="13.6.9") or ""
    )


def test_cuda_profile_discloses_readiness_probe_without_resolved_advisory() -> None:
    profile = runtime_profiles.get_runtime_profile("cuda")

    assert "driver" in profile.description.lower()
    assert "gpu" in profile.description.lower()
    assert profile.risk is None


def test_profile_model_groups_are_explicit() -> None:
    assert runtime_profiles.get_runtime_profile("mlx").model_groups == (
        "tts-mlx",
        "cleaner-mlx",
    )
    assert runtime_profiles.get_runtime_profile("cuda").model_groups == (
        "tts-cpu-cuda",
        "cleaner-cuda",
    )


def test_unknown_profile_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown runtime profile"):
        runtime_profiles.get_runtime_profile("anything-goes")


def test_recommended_profile_prefers_mlx_on_apple_silicon() -> None:
    assert (
        runtime_profiles.recommended_profile_id(
            system="Darwin",
            machine="arm64",
            os_version="14.0",
            which=lambda _: None,
        )
        == "mlx"
    )


def test_recommended_profile_is_cuda_only_when_nvidia_smi_is_present() -> None:
    recommended = runtime_profiles.recommended_profile_id(
        system="Linux",
        machine="x86_64",
        os_version="",
        which=lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None,
    )
    assert recommended == "cuda"


def test_recommended_profile_falls_back_to_base_without_gpu_or_apple_silicon() -> None:
    # Linux without a locatable nvidia-smi, and an Intel Mac, both fall back.
    assert (
        runtime_profiles.recommended_profile_id(
            system="Linux",
            machine="x86_64",
            os_version="",
            which=lambda _: None,
        )
        == "base"
    )
    assert (
        runtime_profiles.recommended_profile_id(
            system="Darwin",
            machine="x86_64",
            os_version="14.0",
            which=lambda name: "/usr/bin/nvidia-smi",
        )
        == "base"
    )


@pytest.mark.parametrize(
    "message",
    [
        mlx_runtime_error_message(ImportError("missing")),
        mlx_lm_runtime_error_message(ImportError("missing")),
    ],
)
def test_mlx_errors_point_to_profile_installer(message: str) -> None:
    assert "python -m pip install -r requirements/mlx.txt" in message
    assert "mlx-audio==0.1.0" not in message
