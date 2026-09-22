"""Regression coverage for basic cleanup, upload ownership and voice settings."""
from __future__ import annotations

import os
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from modules.app.config import AppConfig
from modules.app.controller import AppController, ConversionJob, JobStatus
from modules.app.voice_recommender import VoiceRecommender
from modules.pipeline.models import PipelineConfig
from modules.pipeline.stages.cleaning import clean_chunks_with_trace, save_cleaning_artifacts, save_pre_clean_markdown
from modules.storage.models import BookCreate, SourceType
from modules.tts.cleaner import TextCleaner
from modules.tts.cleaner_config import build_cleaner_config_from_pipeline
from modules.tts.cleaner_backends.metadata import build_cleaner_backend_option
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


def test_basic_conversion_request_runs_rule_only_cleaning(controller, monkeypatch):
    source = controller.config.temp_dir / "book.txt"
    source.write_text("A paragraph with **formatting** and a reference [12]. " * 5)
    service = controller._conversion_service
    monkeypatch.setattr(service, "_validate_requested_tts_runtime", lambda **kwargs: None)
    submitted = {}
    def reserve_only(**kwargs):
        submitted.update(kwargs["kwargs"])
        return kwargs["job"]
    monkeypatch.setattr(service, "_start_pipeline_job", reserve_only)
    response = TestClient(create_app(controller=controller)).post("/api/conversions", json={
        "source_path": str(source), "runtime_options": {"cleaner_backend": "basic"},
    })
    assert response.status_code == 200, response.text
    config = service._create_pipeline_config(runtime_options=submitted["runtime_options"])
    cleaner = TextCleaner(build_cleaner_config_from_pipeline(config))
    monkeypatch.setattr(cleaner, "load", Mock(side_effect=AssertionError("No model may load")))
    runtime = SimpleNamespace(config=config, log_verbose=Mock(), notify_progress=Mock(), check_cancelled=lambda: False)
    cleaned, _, _, cancelled = clean_chunks_with_trace(runtime, SimpleNamespace(), 0, 1, [source.read_text()], cleaner)
    assert not cancelled and cleaned[0]
    assert cleaner.stats_snapshot()["rule_only"] == 1
    assert cleaner.stats_snapshot()["llm_success"] == cleaner.stats_snapshot()["llm_fallback"] == 0
    option = build_cleaner_backend_option("basic")
    assert option["is_local"] and not option["requires_model"] and not option["requires_api_key"]
    assert option["models"] == []


def test_no_local_cleaner_setup_selects_basic(controller, monkeypatch):
    monkeypatch.setattr("scripts.setup_runtime._read_setup_state", lambda path: ({
        "model_groups": ["tts-cpu-cuda"], "profile": "cuda", "cleaner_cuda_quantization": "4bit",
    }, None))
    controller.config.apply_setup_preferences()
    assert controller.config.cleaner_backend == "basic"


@pytest.mark.parametrize("debug", [False, True])
def test_cleaned_text_is_saved_independently_of_debug(tmp_path, debug):
    config = PipelineConfig(save_cleaning_debug=debug)
    runtime = SimpleNamespace(config=config, log_verbose=Mock())
    chapter = SimpleNamespace(number=1, title="Chapter")
    save_pre_clean_markdown(runtime, chapter=chapter, output_dir=tmp_path, original_chunks=["raw"])
    save_cleaning_artifacts(runtime, chapter=chapter, output_dir=tmp_path,
                            cleaned_chunks=["clean"], llm_input_chunks=["input"], cleaning_trace_rows=[{"mode": "rule_only"}])
    assert (tmp_path / "chapter_01_cleaned.md").read_text() == "# Chapter\n\nclean"
    for name in ["before_cleaning.md", "llm_input.md", "cleaning_trace.jsonl"]:
        assert (tmp_path / f"chapter_01_{name}").exists() is debug
    assert PipelineConfig().save_cleaning_debug is False


def test_debug_environment_override_is_preserved(controller, monkeypatch):
    monkeypatch.setenv("SAVE_CLEANING_DEBUG", "true")
    controller.config.__post_init__()
    assert controller.config.save_cleaning_debug is True


def test_uploaded_source_removed_only_after_durable_copy(controller, monkeypatch):
    uploaded = controller.upload_text_source(text="Keep these words.", title="Book", max_bytes=1024)
    source = controller.config.temp_dir / "web_uploads" / uploaded["source_handle"].removeprefix("upload:")
    original = source.read_bytes()
    durable = controller._conversion_service._durable_source_path_for_conversion(source)
    assert durable.read_bytes() == original
    assert not source.exists()
    # Explicit queue deletion after conversion is idempotent.
    assert controller.delete_upload(uploaded["source_handle"]) == {"deleted": True}


def test_copy_failure_keeps_upload(controller, monkeypatch):
    uploaded = controller.upload_text_source(text="Keep these words.", title="Book", max_bytes=1024)
    source = controller.config.temp_dir / "web_uploads" / uploaded["source_handle"].removeprefix("upload:")
    monkeypatch.setattr("modules.app.services.conversion_service.shutil.copyfileobj", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        controller._conversion_service._durable_source_path_for_conversion(source)
    assert source.is_file()


def test_delete_upload_refuses_active_and_library_references(controller):
    uploaded = controller.upload_text_source(text="Keep these words.", title="Book", max_bytes=1024)
    handle = uploaded["source_handle"]
    source = controller.config.temp_dir / "web_uploads" / handle.removeprefix("upload:")
    job = ConversionJob(id="job", book_id=None, status=JobStatus.RUNNING)
    job.upload_source_path = source.resolve()
    controller._conversion_service._active_job = job
    client = TestClient(create_app(controller=controller))
    assert client.delete(f"/api/uploads/{handle}").status_code == 409
    other = controller.upload_text_source(text="Another source.", title="Other", max_bytes=1024)
    assert client.delete(f"/api/uploads/{other['source_handle']}").status_code == 200
    job.status = JobStatus.COMPLETED
    controller.repository.create_book(BookCreate(title="Book", source_path=str(source), source_type=SourceType.TXT, total_chapters=1))
    assert client.delete(f"/api/uploads/{handle}").status_code == 409
    assert source.is_file()
    assert client.delete("/api/uploads/upload:..").status_code == 400


def test_sweep_preserves_week_old_queue_and_active_upload(controller):
    uploaded = controller.upload_text_source(text="Keep these words.", title="Book", max_bytes=1024)
    source = controller.config.temp_dir / "web_uploads" / uploaded["source_handle"].removeprefix("upload:")
    age = time.time() - 6 * 24 * 3600
    os.utime(source, (age, age))
    assert controller.sweep_stale_uploads() == 0
    age -= 2 * 24 * 3600
    os.utime(source, (age, age))
    controller._conversion_service._active_job = ConversionJob(id="job", book_id=None, status=JobStatus.IDLE)
    assert controller.sweep_stale_uploads() == 0
    controller._conversion_service._active_job = None
    assert controller.sweep_stale_uploads() == 1


def test_missing_queued_upload_has_actionable_start_error(controller):
    response = TestClient(create_app(controller=controller)).post("/api/conversions", json={"source_path": "upload:missing.txt", "cleaner_backend": "basic"})
    assert response.status_code == 400
    assert response.json()["detail"] == "Source file is no longer available \u2014 add it again"


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
    monkeypatch.delenv("B1_MISSING_KEY", raising=False)
    recommender = VoiceRecommender(api_base_url="https://example.test", api_key_env="B1_MISSING_KEY", api_model="model")
    with pytest.raises(RuntimeError, match="Missing API key"):
        recommender.suggest_for_book(source_path=source, voices=[{"id": "first"}])


def test_basic_runtime_is_available_without_any_model_probe(monkeypatch):
    from modules.app.services.runtime_capability_service import RuntimeCapabilityService
    service = RuntimeCapabilityService()
    monkeypatch.setattr(service, "_run_probe", Mock(side_effect=AssertionError("No probe needed")))
    assert service.get_cached_cleaner_backend_availability()["basic"] is True
    assert service.probe_cleaner_backend("basic")["available"] is True
