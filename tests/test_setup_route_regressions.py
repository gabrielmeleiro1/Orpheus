"""Regression coverage for setup route."""

from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from modules.runtime_setup import hub_cache
from modules.web.routes import setup as routes
from modules.web.server import create_app
from scripts import setup_runtime

from tests._factories_group2_setup import _RuntimeProbe, _save, _status


@pytest.mark.parametrize("frozen", [False, True], ids=["source", "frozen"])
@pytest.mark.parametrize("packages", ["installed", "missing", "incompatible", "missing-manifest"])
def test_library_only_readiness_uses_manifest_in_source_and_trusts_frozen_bundle(monkeypatch, setup_context, frozen, packages):
    context = setup_context
    monkeypatch.setattr(context.route.sys, "frozen", frozen, raising=False)
    if packages == "missing":
        context.manifest.write_text("orpheus-nonexistent-characterization-package==1.0\n", encoding="utf-8")
    elif packages == "incompatible":
        context.manifest.write_text("pytest==0.0.0\n", encoding="utf-8")
    elif packages == "missing-manifest":
        context.manifest.unlink()
    _save()
    probe = _RuntimeProbe(available=False)
    status = _status(context, probe)
    ready = frozen or packages == "installed"
    assert status["configured"] is True
    assert status["selected_profile"] == "base"
    assert status["selected_model_groups"] == []
    assert status["probe"] == {"ready": True, "checks": []}
    assert status["ready"] is ready
    assert status["state"] == ("ready" if ready else "unavailable")
    assert status["packages"]["ready"] is ready
    assert probe.calls == []


@pytest.mark.parametrize("frozen", [False, True], ids=["source", "frozen"])
def test_voice_assets_are_required_even_when_packages_and_runtime_are_ready(monkeypatch, setup_context, frozen):
    monkeypatch.setattr(setup_context.route.sys, "frozen", frozen, raising=False)
    _save("cuda", ["tts-cpu-cuda"])
    status = _status(setup_context, _RuntimeProbe())
    assert status["packages"]["ready"] is True
    assert status["probe"]["ready"] is True
    assert status["ready"] is False
    assert status["state"] == "unavailable"
    assert status["selected_model_groups"] == [{
        "id": "tts-cpu-cuda", "assets_ready": False, "assets_reason": "missing",
        "runtime_ready": True, "ready": False,
    }]
    assert status["state_error"] == "The Kokoro voice model is missing. Install it to create audio."


@pytest.mark.parametrize("frozen", [False, True], ids=["source", "frozen"])
@pytest.mark.parametrize("available", [False, True])
def test_installed_voice_still_requires_a_working_runtime_in_both_packaging_modes(monkeypatch, setup_context, frozen, available):
    route = setup_context.route
    monkeypatch.setattr(route.sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(hub_cache, "_check_model_group_assets", lambda groups, **kwargs: {
        group: {"ready": True, "reason": None} for group in groups
    })
    _save("cuda", ["tts-cpu-cuda"])
    status = _status(setup_context, _RuntimeProbe(available=available))
    assert status["packages"]["ready"] is True
    assert status["selected_model_groups"][0]["assets_ready"] is True
    assert status["probe"]["ready"] is available
    assert status["ready"] is available
    assert status["state"] == ("ready" if available else "unavailable")


@pytest.mark.parametrize("hardware_available,probed_device,expected_device", [
    (False, None, "cpu"), (True, None, "cuda"), (True, "cpu", "cpu"), (False, "cuda", "cuda"),
])
def test_windows_cuda_selection_keeps_narration_available_without_a_cleaner_gpu(monkeypatch, setup_context, hardware_available, probed_device, expected_device):
    from modules import runtime_profiles

    route = setup_context.route
    monkeypatch.setattr(runtime_profiles.platform, "system", lambda: "Windows")
    reason = runtime_profiles.CUDA_CLEANER_UNAVAILABLE_REASON
    monkeypatch.setattr(route, "cuda_hardware_error", lambda: None if hardware_available else reason)
    detected = {"profile": "cuda", "device": probed_device} if probed_device else None
    profile = route._profile_payload("cuda", installed_runtime=detected)
    assert profile["compatible"] is True
    assert profile["device"] == expected_device
    assert profile["group_availability"]["tts-cpu-cuda"] == {"available": True, "unavailable_reason": None}
    for group in ("cleaner-cuda", "cleaner-cuda-large"):
        assert profile["group_availability"][group] == {
            "available": hardware_available, "unavailable_reason": None if hardware_available else reason,
        }


@pytest.mark.parametrize("timed_out,restart,expected", [(False, False, "unavailable"), (True, False, "checking"), (True, True, "restart_required")])
def test_timeout_is_checking_and_restart_takes_priority(setup_context, timed_out, restart, expected):
    _save("cuda", ["tts-cpu-cuda"], restart=restart)
    status = _status(setup_context, _RuntimeProbe(available=False, timed_out=timed_out))
    assert status["state"] == expected
    assert status.get("checking", False) is (expected == "checking")
    assert status["ready"] is False
    check = {"id": "tts-cpu-cuda", "ready": False}
    if timed_out:
        check["state"] = "timeout"
    assert status["probe"]["checks"] == [check]


def test_runtime_status_keeps_restart_separate_from_projected_readiness(setup_context):
    _save(restart=True)
    raw = setup_runtime.get_setup_status(probe_service=_RuntimeProbe())
    assert raw["ready"] is True
    assert raw["restart_required"] is True
    projected = _status(setup_context, _RuntimeProbe())
    assert projected["ready"] is False
    assert projected["state"] == "restart_required"


@pytest.mark.parametrize("saved", ["missing", "invalid", "base", "cuda"])
def test_pending_response_preserves_saved_selection_without_claiming_readiness(setup_context, saved):
    if saved == "invalid":
        setup_runtime.default_state_path().write_text("invalid json", encoding="utf-8")
    elif saved != "missing":
        _save(saved, ["tts-cpu-cuda"] if saved == "cuda" else [], restart=True)
    status = setup_context.route._pending_setup_status_response(setup_context.request)
    assert status["ready"] is False
    assert status["configured"] is (saved in {"base", "cuda"})
    assert status["selected_profile"] == (saved if saved in {"base", "cuda"} else None)
    assert status["state"] == ("error" if saved == "invalid" else "checking")
    assert status.get("checking", False) is (saved != "invalid")
    assert status["restart_required"] is (saved in {"base", "cuda"})
    if saved == "cuda":
        # current behaviour: the pending projection says checking even when restart is required.
        assert status["probe"] == {"ready": False, "checks": [
            {"id": "tts-cpu-cuda", "ready": False, "state": "pending"},
        ]}
        assert status["cleaner_cuda_quantization"] == "8bit"


def test_adoption_keeps_setup_lock_until_preferences_apply(tmp_path, monkeypatch):
    state_path = tmp_path / "setup.json"
    monkeypatch.setattr("modules.runtime_setup.state.default_state_path", lambda: state_path)
    monkeypatch.setattr(routes, "build_setup_status_response", lambda _: {})
    def apply(**kwargs):
        with pytest.raises(setup_runtime.SetupExecutionError):
            with setup_runtime.SetupLock(state_path, timeout_seconds=0):
                pass
    with TestClient(create_app(controller=SimpleNamespace(config=SimpleNamespace(apply_setup_preferences=apply)))) as client:
        assert client.post("/api/setup/adopt", json={"profile": "base"}).status_code == 200


@pytest.mark.parametrize("configure", [True, False])
def test_download_validates_active_selection_only_when_configuring(monkeypatch, configure):
    monkeypatch.setattr("modules.runtime_profiles.platform.system", lambda: "Windows")
    start = Mock(return_value={"lifecycle": "running"})
    monkeypatch.setattr(routes, "_get_download_service", lambda _: SimpleNamespace(start=start))
    monkeypatch.setattr(routes, "cuda_hardware_error", lambda: None)
    with TestClient(create_app(controller=object())) as client:
        response = client.post("/api/setup/models/download", json={
            "profile": "cuda", "model_groups": ["tts-cpu-cuda", "cleaner-cuda", "cleaner-cuda-large"],
            "configure_runtime": configure,
        })
    assert response.status_code == (422 if configure else 200)
    assert start.call_count == (0 if configure else 1)
