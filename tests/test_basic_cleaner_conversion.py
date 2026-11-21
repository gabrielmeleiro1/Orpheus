"""Basic cleanup configuration, conversion and model lifecycle."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from modules.runtime_setup import hub_cache
from modules.app.config import AppConfig
from modules.app.controller import AppController
from modules.pipeline.models import PipelineConfig
from modules.pipeline.stages.cleaning import clean_chunks_with_trace, save_cleaning_artifacts, save_pre_clean_markdown
from modules.tts.cleaner import TextCleaner
from modules.tts.cleaner_config import build_cleaner_config_from_pipeline
from modules.tts.cleaner_backends.metadata import build_cleaner_backend_option, list_cleaner_backend_options
from modules.web.server import create_app
from scripts import setup_runtime


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


@pytest.mark.parametrize("backend", ["basic", None])
def test_basic_conversion_request_runs_rule_only_cleaning(controller, monkeypatch, backend):
    if backend is None:
        controller.config.cleaner_backend = "basic"
    source = controller.config.temp_dir / "book.txt"
    source.write_text("A paragraph with **formatting** and a reference [12]. " * 5)
    service = controller._conversion_service
    monkeypatch.setattr(service, "_validate_requested_tts_runtime", lambda **kwargs: None)
    submitted = {}
    def reserve_only(**kwargs):
        submitted.update(kwargs["kwargs"])
        return kwargs["job"]
    monkeypatch.setattr(service._job_coordinator, "_start_pipeline_job", reserve_only)
    response = TestClient(create_app(controller=controller)).post("/api/conversions", json={
        "source_path": str(source), "runtime_options": {"cleaner_backend": backend},
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
    monkeypatch.setattr("modules.runtime_setup.state._read_setup_state", lambda path: ({
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
    assert (tmp_path / "chapter_01_before_cleaning.md").read_text() == "# Chapter\n\nraw"
    for name in ["llm_input.md", "cleaning_trace.jsonl"]:
        assert (tmp_path / f"chapter_01_{name}").exists() is debug
    assert PipelineConfig().save_cleaning_debug is False


def test_debug_environment_override_is_preserved(controller, monkeypatch):
    monkeypatch.setenv("SAVE_CLEANING_DEBUG", "true")
    controller.config.__post_init__()
    assert controller.config.save_cleaning_debug is True


def test_basic_runtime_is_available_without_any_model_probe(monkeypatch):
    from modules.app.services.runtime_capability_service import RuntimeCapabilityService
    service = RuntimeCapabilityService()
    monkeypatch.setattr(service, "_run_probe", Mock(side_effect=AssertionError("No probe needed")))
    assert service.get_cached_cleaner_backend_availability()["basic"] is True
    assert service.probe_cleaner_backend("basic")["available"] is True


@pytest.mark.parametrize("parallel", [False, True])
def test_basic_cleaner_lifecycle_never_loads_or_advertises_a_model(monkeypatch, parallel):
    from modules.pipeline.model_lifecycle import get_text_cleaner_for_chapter, warmup_models

    monkeypatch.setattr(TextCleaner, "load", Mock(side_effect=AssertionError("No model may load")))
    runtime = SimpleNamespace(
        config=PipelineConfig(cleaner_backend="basic"),
        state=SimpleNamespace(tts_engine=SimpleNamespace(is_loaded=True), text_cleaner=None, parallel_execution_active=parallel),
        log_verbose=Mock(),
    )
    warmup_models(runtime)
    cleaner = get_text_cleaner_for_chapter(runtime)
    cleaner.clean_with_trace("A quiet lake.")
    assert cleaner.stats_snapshot()["rule_only"] == 1
    messages = [call.args[0] for call in runtime.log_verbose.call_args_list]
    assert any("no model" in message for message in messages)
    assert not any("Text Cleaner model" in message for message in messages)


@pytest.mark.parametrize("downloaded", [False, True])
def test_no_cleaner_setup_keeps_basic_option_even_with_cached_qwen(tmp_path, monkeypatch, downloaded):
    state_path = tmp_path / "runtime-setup.json"
    setup_runtime.write_setup_state(
        state_path, profile_id="cuda", model_groups=("tts-cpu-cuda",),
    )
    monkeypatch.setattr("modules.runtime_setup.state.default_state_path", lambda: state_path)
    monkeypatch.setattr(hub_cache, "_check_model_group_assets", lambda groups, **kwargs: {
        group: {"ready": downloaded} for group in groups
    })
    controller = SimpleNamespace(
        get_available_tts_engines=lambda: [], get_available_cleaner_models=lambda: [],
        get_available_api_cleaner_models=lambda: [], get_cuda_cleaner_models=lambda: [],
        get_cleaner_backend_options=list_cleaner_backend_options,
    )
    with TestClient(create_app(controller=controller)) as client:
        response = client.get("/api/options/models")
    assert response.status_code == 200
    options = {option["name"]: option for option in response.json()["cleaner_backend_options"]}
    assert options["basic"]["requires_model"] is False
    assert options["basic"]["models"] == []
    assert ("cuda" in options) is downloaded
