"""Regression coverage for setup runtime."""

import subprocess

import pytest

from scripts import setup_runtime


def test_setup_upgrades_hub_with_symlink_flag_support(monkeypatch):
    from packaging.requirements import Requirement

    requirements = (setup_runtime.REPO_ROOT / "requirements/base.txt").read_text()
    hub = next(Requirement(line) for line in requirements.splitlines()
               if line.startswith("huggingface-hub"))
    assert "1.8.0" not in hub.specifier
    assert "1.9.0" in hub.specifier
    monkeypatch.setattr(setup_runtime.RuntimeProfile, "is_compatible", lambda self: True)
    for profile in ("base", "mlx", "cuda"):
        assert "--upgrade" in setup_runtime.build_install_plan(profile).pip_command


@pytest.mark.parametrize("directory", ["scripts", "unrelated"])
def test_setup_verification_outside_repository(monkeypatch, tmp_path, directory):
    monkeypatch.chdir(setup_runtime.REPO_ROOT / "scripts" if directory == "scripts" else tmp_path)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setattr(setup_runtime.RuntimeProfile, "is_compatible", lambda self: True)
    probe = setup_runtime.RuntimeCapabilityService._TTS_PACKAGE_PROBE
    stubs = (
        "import sys\nfrom types import SimpleNamespace\n"
        "sys.modules['torch'] = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False))\n"
        "sys.modules['kokoro'] = SimpleNamespace(KModel=object, KPipeline=object)\n"
    )
    monkeypatch.setattr(setup_runtime.RuntimeCapabilityService, "_TTS_PACKAGE_PROBE", stubs + probe)
    state_path = tmp_path / "setup.json"
    setup_runtime.run_setup(
        profile_id="cuda", model_groups=["tts-cpu-cuda"], state_path=state_path,
        command_runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 0),
    )
    assert setup_runtime.read_setup_state(state_path)["profile"] == "cuda"
