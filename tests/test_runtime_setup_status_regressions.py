"""Regression coverage for runtime setup status."""

import pytest

from scripts import setup_runtime

from tests._factories_group2_setup import _RuntimeProbe, _save


@pytest.mark.parametrize("device", ["cpu", "cuda"])
@pytest.mark.parametrize("with_cleaner", [False, True])
def test_cuda_profile_accepts_cpu_or_cuda_narration_and_probes_only_selected_cleaner(setup_context, device, with_cleaner):
    groups = ["tts-cpu-cuda"] + (["cleaner-cuda"] if with_cleaner else [])
    _save("cuda", groups)
    probe = _RuntimeProbe(device=device)
    status = setup_runtime.get_setup_status(probe_service=probe)
    assert status["ready"] is True
    assert status["profile"] == "cuda"
    assert status["probe"]["checks"][0] == {
        "id": "tts-cpu-cuda", "device": device, "available": True, "error": None,
    }
    assert probe.calls == ["tts-cpu-cuda"] + ([("cuda", "8bit")] if with_cleaner else [])


@pytest.mark.parametrize("profile,groups", [("cuda", []), ("cuda", ["cleaner-cuda"]), ("mlx", ["cleaner-mlx"])])
def test_non_base_selection_without_voice_is_not_ready(setup_context, profile, groups):
    _save(profile, groups)
    status = setup_runtime.get_setup_status(probe_service=_RuntimeProbe())
    assert status["probe"]["available"] is True
    assert status["ready"] is False
    assert status["state_error"] == "Select the Kokoro voice model — it's required to create audio."
