"""Adoption reuses the work "Check runtime" already did."""

from __future__ import annotations

from modules.runtime_setup import hub_cache
from tests.test_setup_onboarding import _all_profiles_compatible, _client, _state_path


def test_adoption_verifies_models_with_the_shared_receipt_verifier(monkeypatch, tmp_path) -> None:
    from modules.web.routes import setup as setup_route

    _all_profiles_compatible(monkeypatch)
    monkeypatch.setenv("AUDIOBOOK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(setup_route, "_check_selected_profile_packages", lambda _profile: {"ready": True})
    monkeypatch.setattr("modules.runtime_setup.status._probe_profile", lambda *args, **kwargs: {"available": True, "checks": []})
    verifiers = []

    def record(groups, **kwargs):
        verifiers.append(kwargs.get("receipt_verifier"))
        return {group: {"ready": True, "reason": None} for group in groups}

    monkeypatch.setattr(hub_cache, "_check_model_group_assets", record)
    with _client() as client:
        response = client.post("/api/setup/adopt", json={"profile": "cuda", "model_groups": ["tts-cpu-cuda"]})
        cache = client.app.state.runtime_setup_status_cache

    assert response.status_code == 200, response.text
    assert _state_path(tmp_path).exists()
    # A fresh verifier rehashes every model file (about 7 s for the CUDA set on
    # the measured machine); both checks must reuse the cached identities.
    assert len(verifiers) >= 2
    assert all(verifier is cache.receipt_verifier for verifier in verifiers)
