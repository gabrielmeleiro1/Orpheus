"""Voice suggestion credentials and model selection."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from modules.app.config import AppConfig
from modules.app.controller import AppController
from modules.app.voice_recommender import VoiceRecommender
from modules.web.server import create_app


@pytest.fixture
def controller(tmp_path):
    config = AppConfig(data_dir=tmp_path / "data", output_dir=tmp_path / "output",
                       temp_dir=tmp_path / "temp", db_path=tmp_path / "data" / "library.db",
                       worker_pool_reuse_enabled=False)
    app = AppController(config)
    yield app
    app._voice_service.cleanup()
    app._conversion_service.cleanup()
    app.repository.close()


def test_voice_suggestion_uses_request_model_and_key(controller, monkeypatch):
    source = controller.config.temp_dir / "book.txt"
    source.write_text("An audiobook excerpt.")
    voices = [{"id": "voice", "name": "Voice"}]
    monkeypatch.setattr(controller._voice_service, "get_available_voices", lambda **kwargs: voices)
    calls = []
    # Patch the transport boundary, leaving route/controller/service/recommender intact.
    import json
    def request(self, *, payload, api_key):
        calls.append((payload["model"], api_key))
        return json.dumps({"choices": [{"message": {"content": json.dumps({"recommended_voice_id": "voice"})}}]})
    monkeypatch.setattr(VoiceRecommender, "_post_chat_completions", request)
    response = TestClient(create_app(controller=controller)).post("/api/voices/suggest", json={
        "source_path": str(source), "tts_engine": "kokoro", "cleaner_backend": "api",
        "cleaner_api_model": "selected-model", "cleaner_api_key": "session-key",
    })
    assert response.status_code == 200, response.text
    assert calls == [("selected-model", "session-key")]
    assert "session-key" not in response.text


def test_unconfigured_recommender_does_not_return_first_voice(tmp_path, monkeypatch):
    source = tmp_path / "book.txt"
    source.write_text("An audiobook excerpt.")
    monkeypatch.delenv("VOICE_SUGGESTION_MISSING_KEY", raising=False)
    recommender = VoiceRecommender(api_base_url="https://example.test", api_key_env="VOICE_SUGGESTION_MISSING_KEY", api_model="model")
    with pytest.raises(RuntimeError, match="Missing API key"):
        recommender.suggest_for_book(source_path=source, voices=[{"id": "first"}])
