"""Configuration precedence and saved setup defaults over isolated files."""

import os
from pathlib import Path

import pytest

from modules.app.config import AppConfig, SetupPreferences
from modules.tts.runtime_spec import CLEANER_CUDA_DEFAULT_MODEL, CLEANER_MLX_DEFAULT_MODEL
from scripts import setup_runtime


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key in tuple(os.environ):
        if key.startswith(("AUDIOBOOK_", "CLEANER_", "TTS_", "HF_", "HUGGINGFACE_")) or key in {
            "WORKER_POOL_REUSE_ENABLED", "SAVE_CLEANING_DEBUG", "PDF_EXTRACTION_BACKEND",
        }:
            monkeypatch.delenv(key)
    monkeypatch.setenv("AUDIOBOOK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hf" / "hub"))


def _expanded_environment_path(raw):
    """Preserve the original environment path normalization and HOME shortcut."""
    normalized = raw.strip()
    if normalized == "~" or normalized.startswith(("~/", "~\\")):
        home = os.getenv("HOME")
        if home:
            suffix = normalized[2:] if len(normalized) > 1 else ""
            return Path(home, suffix)
    return Path(normalized).expanduser()


@pytest.mark.parametrize("field,env_key", [
    ("data_dir", "AUDIOBOOK_DATA_DIR"),
    ("output_dir", "AUDIOBOOK_OUTPUT_DIR"),
    ("temp_dir", "AUDIOBOOK_TEMP_DIR"),
    ("db_path", "AUDIOBOOK_DB_PATH"),
])
@pytest.mark.parametrize("raw", [
    "./~/x", "~/x", "~", "a/../~/x", "./x", "x", "/absolute/x", "/~/x",
    "../~/x", "././~/x", ".//~/x", " ./~/x ", "./~", "~/./x", "~//x", "~\\x",
])
@pytest.mark.parametrize("absolute_home", [False, True])
def test_environment_paths_preserve_normalized_home_expansion(
    monkeypatch, tmp_path, field, env_key, raw, absolute_home,
):
    monkeypatch.setenv("HOME", str(tmp_path / "home") if absolute_home else "relative-home")
    monkeypatch.setenv(env_key, raw)
    monkeypatch.setattr(Path, "mkdir", lambda self, **kwargs: None)
    expected = _expanded_environment_path(raw)
    assert str(getattr(AppConfig(), field)) == str(expected)


def test_local_env_supplies_defaults_without_replacing_process_values(monkeypatch, tmp_path):
    monkeypatch.setenv("TTS_DEVICE", "cpu")
    # Register absent keys so monkeypatch also restores additions made by the env loader.
    for key in ("TTS_PROVIDER", "CLEANER_API_MODEL"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    (tmp_path / ".env.local").write_text(
        'TTS_DEVICE=cuda\nTTS_PROVIDER="cuda"\nCLEANER_API_MODEL=\'local-model\'\n',
        encoding="utf-8",
    )
    config = AppConfig(tts_provider="cpu", tts_device="cuda:1", cleaner_api_model="constructor")
    assert (config.tts_provider, config.tts_device, config.cleaner_api_model) == ("cuda", "cpu", "local-model")


def _save_selection(profile, groups, precision="8bit"):
    return setup_runtime.write_setup_state(
        setup_runtime.default_state_path(), profile_id=profile, model_groups=groups,
        accepted_risks=(), restart_required=False, cleaner_cuda_quantization=precision,
    )


@pytest.mark.parametrize("profile,groups,expected_backend,expected_model", [
    ("cuda", ["tts-cpu-cuda", "cleaner-cuda"], "cuda", CLEANER_CUDA_DEFAULT_MODEL),
    ("mlx", ["tts-mlx", "cleaner-mlx"], "mlx", CLEANER_MLX_DEFAULT_MODEL),
    ("cuda", ["tts-cpu-cuda"], "basic", "environment-model"),
    ("base", [], "basic", "environment-model"),
])
def test_saved_preferences_apply_after_environment_and_constructor(monkeypatch, profile, groups, expected_backend, expected_model):
    monkeypatch.setenv("CLEANER_BACKEND", "api")
    monkeypatch.setenv("CLEANER_MODEL_NAME", "environment-model")
    monkeypatch.setenv("CLEANER_CUDA_QUANTIZATION", "fp16")
    _save_selection(profile, groups)
    config = AppConfig(cleaner_backend="mlx", cleaner_model_name="constructor-model", tts_provider="cpu")
    baseline = SetupPreferences("api", "environment-model", "fp16")
    assert config.setup_preferences() == baseline
    config.apply_setup_preferences()
    assert config.setup_preferences() == SetupPreferences(expected_backend, expected_model, "8bit")
    assert config.tts_provider == "cpu"
    config.reset_setup_preferences()
    assert config.setup_preferences() == baseline


def test_reset_restores_first_applied_baseline_and_recaptures_it_after_reset():
    config = AppConfig(cleaner_backend="api", cleaner_model_name="initial", cleaner_cuda_quantization="fp16")
    config.cleaner_model_name = "before-first-selection"
    baseline = config.setup_preferences()
    for group in ("cleaner-cuda", "cleaner-cuda-large"):
        _save_selection("cuda", ["tts-cpu-cuda", group])
        config.apply_setup_preferences()
    config.reset_setup_preferences()
    assert config.setup_preferences() == baseline
    config.cleaner_model_name = "after-reset"
    next_baseline = config.setup_preferences()
    config.apply_setup_preferences()
    config.reset_setup_preferences()
    config.reset_setup_preferences()
    assert config.setup_preferences() == next_baseline


@pytest.mark.parametrize("changed_field,new_value", [
    ("cleaner_backend", "basic"),
    ("cleaner_model_name", "edited-model"),
    ("cleaner_cuda_quantization", "4bit"),
])
def test_reset_preserves_independent_edits_to_each_cleaner_field(changed_field, new_value):
    config = AppConfig(cleaner_backend="api", cleaner_model_name="original", cleaner_cuda_quantization="fp16")
    baseline = config.setup_preferences()
    _save_selection("cuda", ["tts-cpu-cuda", "cleaner-cuda"])
    config.apply_setup_preferences()
    setattr(config, changed_field, new_value)
    config.reset_setup_preferences()
    expected = vars(baseline) | {changed_field: new_value}
    assert config.setup_preferences() == SetupPreferences(**expected)


@pytest.mark.parametrize("contents", [None, "invalid json"])
def test_missing_or_invalid_saved_state_preserves_applied_preferences(contents):
    config = AppConfig()
    _save_selection("cuda", ["tts-cpu-cuda", "cleaner-cuda"])
    config.apply_setup_preferences()
    selected = config.setup_preferences()
    path = setup_runtime.default_state_path()
    if contents is None:
        path.unlink()
    else:
        path.write_text(contents, encoding="utf-8")
    config.apply_setup_preferences()
    assert config.setup_preferences() == selected


# Complete serialized values capture the configuration contract, including untouched fields.
_BASE_CONFIG_VALUES = {
    "data_dir": "data",
    "output_dir": "output",
    "temp_dir": "temp",
    "default_voice": "am_adam",
    "default_speed": 1.0,
    "tts_engine": "kokoro",
    "tts_provider": None,
    "tts_device": None,
    "tts_model_name": None,
    "tts_quantization": "bf16",
    "cleaner_model_name": "mlx-community/Llama-3.2-3B-Instruct-4bit",
    "cleaner_backend": "mlx",
    "cleaner_cuda_device": "cuda",
    "cleaner_cuda_quantization": "4bit",
    "cleaner_api_base_url": "https://api.openai.com/v1",
    "cleaner_api_key_env": "OPENAI_API_KEY",
    "cleaner_api_model": "gpt-5-nano",
    "cleaner_api_global_concurrency": 6,
    "worker_pool_reuse_enabled": True,
    "save_cleaning_debug": False,
    "pdf_extraction_backend": "fitz",
    "enable_parallel": False,
    "max_parallel_chapters": 4,
    "chunk_size": 700,
    "enable_tts_file_batch_results": False,
    "keep_tts_engine_warm": True,
    "tts_idle_timeout_seconds": 300.0,
    "mp3_bitrate": "128k",
    "emit_chapter_mp3_artifacts": False,
    "enable_chapter_aac_packaging": True,
    "normalize_volume": True,
    "target_dbfs": -16.0,
    "db_path": "data/audiobooks.db",
}
_RESOLUTION_CASES = [
    ({}, {}, None, {}),
    ({"default_speed": 1}, {}, None, {"default_speed": 1}),
    ({"default_speed": True}, {}, None, {"default_speed": True}),
    (
        {"data_dir": "data"},
        {"AUDIOBOOK_DATA_DIR": "environment/data_dir"},
        None,
        {"data_dir": "environment/data_dir", "db_path": "environment/data_dir/audiobooks.db"},
    ),
    (
        {"data_dir": "custom/data_dir"},
        {"AUDIOBOOK_DATA_DIR": "environment/data_dir"},
        None,
        {"data_dir": "custom/data_dir", "db_path": "custom/data_dir/audiobooks.db"},
    ),
    ({}, {"AUDIOBOOK_DATA_DIR": ""}, None, {}),
    ({}, {"AUDIOBOOK_DATA_DIR": "  "}, None, {}),
    (
        {},
        {"AUDIOBOOK_DATA_DIR": " ~/env-path "},
        None,
        {"data_dir": "home/env-path", "db_path": "home/env-path/audiobooks.db"},
    ),
    (
        {},
        {"AUDIOBOOK_DATA_DIR": "~\\env-path"},
        None,
        {"data_dir": "home/env-path", "db_path": "home/env-path/audiobooks.db"},
    ),
    (
        {"output_dir": "output"},
        {"AUDIOBOOK_OUTPUT_DIR": "environment/output_dir"},
        None,
        {"output_dir": "environment/output_dir"},
    ),
    (
        {"output_dir": "custom/output_dir"},
        {"AUDIOBOOK_OUTPUT_DIR": "environment/output_dir"},
        None,
        {"output_dir": "custom/output_dir"},
    ),
    ({}, {"AUDIOBOOK_OUTPUT_DIR": ""}, None, {}),
    ({}, {"AUDIOBOOK_OUTPUT_DIR": "  "}, None, {}),
    ({}, {"AUDIOBOOK_OUTPUT_DIR": " ~/env-path "}, None, {"output_dir": "home/env-path"}),
    ({}, {"AUDIOBOOK_OUTPUT_DIR": "~\\env-path"}, None, {"output_dir": "home/env-path"}),
    (
        {"temp_dir": "temp"},
        {"AUDIOBOOK_TEMP_DIR": "environment/temp_dir"},
        None,
        {"temp_dir": "environment/temp_dir"},
    ),
    (
        {"temp_dir": "custom/temp_dir"},
        {"AUDIOBOOK_TEMP_DIR": "environment/temp_dir"},
        None,
        {"temp_dir": "custom/temp_dir"},
    ),
    ({}, {"AUDIOBOOK_TEMP_DIR": ""}, None, {}),
    ({}, {"AUDIOBOOK_TEMP_DIR": "  "}, None, {}),
    ({}, {"AUDIOBOOK_TEMP_DIR": " ~/env-path "}, None, {"temp_dir": "home/env-path"}),
    ({}, {"AUDIOBOOK_TEMP_DIR": "~\\env-path"}, None, {"temp_dir": "home/env-path"}),
    (
        {"db_path": None},
        {"AUDIOBOOK_DB_PATH": "environment/db_path"},
        None,
        {"db_path": "environment/db_path"},
    ),
    (
        {"db_path": "custom/db_path"},
        {"AUDIOBOOK_DB_PATH": "environment/db_path"},
        None,
        {"db_path": "custom/db_path"},
    ),
    ({}, {"AUDIOBOOK_DB_PATH": ""}, None, {}),
    ({}, {"AUDIOBOOK_DB_PATH": "  "}, None, {}),
    ({}, {"AUDIOBOOK_DB_PATH": " ~/env-path "}, None, {"db_path": "home/env-path"}),
    ({}, {"AUDIOBOOK_DB_PATH": "~\\env-path"}, None, {"db_path": "home/env-path"}),
    (
        {"tts_provider": "cpu"},
        {"TTS_PROVIDER": " environment "},
        None,
        {"tts_provider": "environment"},
    ),
    ({"tts_provider": "cpu"}, {"TTS_PROVIDER": " "}, None, {}),
    ({"tts_device": "cpu"}, {"TTS_DEVICE": " environment "}, None, {"tts_device": "environment"}),
    ({"tts_device": "cpu"}, {"TTS_DEVICE": " "}, None, {}),
    (
        {"tts_model_name": "constructor"},
        {"TTS_MODEL_NAME": " environment "},
        None,
        {"tts_model_name": "environment"},
    ),
    ({"tts_model_name": "constructor"}, {"TTS_MODEL_NAME": " "}, None, {}),
    (
        {"cleaner_backend": "api"},
        {"CLEANER_BACKEND": " environment "},
        None,
        {"cleaner_backend": "environment"},
    ),
    ({"cleaner_backend": "api"}, {"CLEANER_BACKEND": " "}, None, {"cleaner_backend": "api"}),
    (
        {"cleaner_model_name": "constructor"},
        {"CLEANER_MODEL_NAME": " environment "},
        None,
        {"cleaner_model_name": "environment"},
    ),
    (
        {"cleaner_model_name": "constructor"},
        {"CLEANER_MODEL_NAME": " "},
        None,
        {"cleaner_model_name": "constructor"},
    ),
    (
        {"cleaner_cuda_device": "cuda:0"},
        {"CLEANER_CUDA_DEVICE": " environment "},
        None,
        {"cleaner_cuda_device": "environment"},
    ),
    (
        {"cleaner_cuda_device": "cuda:0"},
        {"CLEANER_CUDA_DEVICE": " "},
        None,
        {"cleaner_cuda_device": "cuda:0"},
    ),
    (
        {"cleaner_cuda_quantization": "8bit"},
        {"CLEANER_CUDA_QUANTIZATION": " environment "},
        None,
        {"cleaner_cuda_quantization": "environment"},
    ),
    (
        {"cleaner_cuda_quantization": "8bit"},
        {"CLEANER_CUDA_QUANTIZATION": " "},
        None,
        {"cleaner_cuda_quantization": "8bit"},
    ),
    (
        {"cleaner_api_base_url": "https://example.org/v1"},
        {"CLEANER_API_BASE_URL": " environment "},
        None,
        {"cleaner_api_base_url": "environment"},
    ),
    (
        {"cleaner_api_base_url": "https://example.org/v1"},
        {"CLEANER_API_BASE_URL": " "},
        None,
        {"cleaner_api_base_url": "https://example.org/v1"},
    ),
    (
        {"cleaner_api_key_env": "CUSTOM_API_KEY"},
        {"CLEANER_API_KEY_ENV": " environment "},
        None,
        {"cleaner_api_key_env": "environment"},
    ),
    (
        {"cleaner_api_key_env": "CUSTOM_API_KEY"},
        {"CLEANER_API_KEY_ENV": " "},
        None,
        {"cleaner_api_key_env": "CUSTOM_API_KEY"},
    ),
    (
        {"cleaner_api_model": "constructor"},
        {"CLEANER_API_MODEL": " environment "},
        None,
        {"cleaner_api_model": "environment"},
    ),
    (
        {"cleaner_api_model": "constructor"},
        {"CLEANER_API_MODEL": " "},
        None,
        {"cleaner_api_model": None},
    ),
    ({}, {"WORKER_POOL_REUSE_ENABLED": "0"}, None, {"worker_pool_reuse_enabled": False}),
    ({}, {"WORKER_POOL_REUSE_ENABLED": " FALSE "}, None, {"worker_pool_reuse_enabled": False}),
    ({}, {"WORKER_POOL_REUSE_ENABLED": "no"}, None, {"worker_pool_reuse_enabled": False}),
    ({}, {"WORKER_POOL_REUSE_ENABLED": "off"}, None, {"worker_pool_reuse_enabled": False}),
    ({}, {"WORKER_POOL_REUSE_ENABLED": "1"}, None, {}),
    ({}, {"WORKER_POOL_REUSE_ENABLED": "unknown"}, None, {}),
    ({}, {"WORKER_POOL_REUSE_ENABLED": " "}, None, {}),
    ({}, {"SAVE_CLEANING_DEBUG": "0"}, None, {}),
    ({}, {"SAVE_CLEANING_DEBUG": " FALSE "}, None, {}),
    ({}, {"SAVE_CLEANING_DEBUG": "no"}, None, {}),
    ({}, {"SAVE_CLEANING_DEBUG": "off"}, None, {}),
    ({}, {"SAVE_CLEANING_DEBUG": "1"}, None, {"save_cleaning_debug": True}),
    ({}, {"SAVE_CLEANING_DEBUG": "unknown"}, None, {"save_cleaning_debug": True}),
    ({}, {"SAVE_CLEANING_DEBUG": " "}, None, {}),
    ({}, {"AUDIOBOOK_EMIT_CHAPTER_MP3_ARTIFACTS": "0"}, None, {}),
    ({}, {"AUDIOBOOK_EMIT_CHAPTER_MP3_ARTIFACTS": " FALSE "}, None, {}),
    ({}, {"AUDIOBOOK_EMIT_CHAPTER_MP3_ARTIFACTS": "no"}, None, {}),
    ({}, {"AUDIOBOOK_EMIT_CHAPTER_MP3_ARTIFACTS": "off"}, None, {}),
    ({}, {"AUDIOBOOK_EMIT_CHAPTER_MP3_ARTIFACTS": "1"}, None, {"emit_chapter_mp3_artifacts": True}),
    (
        {},
        {"AUDIOBOOK_EMIT_CHAPTER_MP3_ARTIFACTS": "unknown"},
        None,
        {"emit_chapter_mp3_artifacts": True},
    ),
    ({}, {"AUDIOBOOK_EMIT_CHAPTER_MP3_ARTIFACTS": " "}, None, {}),
    (
        {},
        {"AUDIOBOOK_ENABLE_CHAPTER_AAC_PACKAGING": "0"},
        None,
        {"enable_chapter_aac_packaging": False},
    ),
    (
        {},
        {"AUDIOBOOK_ENABLE_CHAPTER_AAC_PACKAGING": " FALSE "},
        None,
        {"enable_chapter_aac_packaging": False},
    ),
    (
        {},
        {"AUDIOBOOK_ENABLE_CHAPTER_AAC_PACKAGING": "no"},
        None,
        {"enable_chapter_aac_packaging": False},
    ),
    (
        {},
        {"AUDIOBOOK_ENABLE_CHAPTER_AAC_PACKAGING": "off"},
        None,
        {"enable_chapter_aac_packaging": False},
    ),
    ({}, {"AUDIOBOOK_ENABLE_CHAPTER_AAC_PACKAGING": "1"}, None, {}),
    ({}, {"AUDIOBOOK_ENABLE_CHAPTER_AAC_PACKAGING": "unknown"}, None, {}),
    ({}, {"AUDIOBOOK_ENABLE_CHAPTER_AAC_PACKAGING": " "}, None, {}),
    ({}, {"AUDIOBOOK_ENABLE_PARALLEL": "0"}, None, {}),
    ({}, {"AUDIOBOOK_ENABLE_PARALLEL": " FALSE "}, None, {}),
    ({}, {"AUDIOBOOK_ENABLE_PARALLEL": "no"}, None, {}),
    ({}, {"AUDIOBOOK_ENABLE_PARALLEL": "off"}, None, {}),
    ({}, {"AUDIOBOOK_ENABLE_PARALLEL": "1"}, None, {"enable_parallel": True}),
    ({}, {"AUDIOBOOK_ENABLE_PARALLEL": "unknown"}, None, {"enable_parallel": True}),
    ({}, {"AUDIOBOOK_ENABLE_PARALLEL": " "}, None, {}),
    (
        {"enable_parallel": True},
        {"CLEANER_API_GLOBAL_CONCURRENCY": "0"},
        None,
        {"cleaner_api_global_concurrency": 1, "enable_parallel": True},
    ),
    (
        {"enable_parallel": True},
        {"CLEANER_API_GLOBAL_CONCURRENCY": "-2"},
        None,
        {"cleaner_api_global_concurrency": 1, "enable_parallel": True},
    ),
    (
        {"enable_parallel": True},
        {"CLEANER_API_GLOBAL_CONCURRENCY": " 7 "},
        None,
        {"cleaner_api_global_concurrency": 7, "enable_parallel": True},
    ),
    (
        {"enable_parallel": True},
        {"CLEANER_API_GLOBAL_CONCURRENCY": "bad"},
        None,
        {"enable_parallel": True},
    ),
    (
        {"enable_parallel": True},
        {"CLEANER_API_GLOBAL_CONCURRENCY": ""},
        None,
        {"enable_parallel": True},
    ),
    (
        {"enable_parallel": True},
        {"AUDIOBOOK_MAX_PARALLEL_CHAPTERS": "0"},
        None,
        {"max_parallel_chapters": 1},
    ),
    (
        {"enable_parallel": True},
        {"AUDIOBOOK_MAX_PARALLEL_CHAPTERS": "-2"},
        None,
        {"max_parallel_chapters": 1},
    ),
    (
        {"enable_parallel": True},
        {"AUDIOBOOK_MAX_PARALLEL_CHAPTERS": " 7 "},
        None,
        {"enable_parallel": True, "max_parallel_chapters": 7},
    ),
    (
        {"enable_parallel": True},
        {"AUDIOBOOK_MAX_PARALLEL_CHAPTERS": "bad"},
        None,
        {"enable_parallel": True},
    ),
    (
        {"enable_parallel": True},
        {"AUDIOBOOK_MAX_PARALLEL_CHAPTERS": ""},
        None,
        {"enable_parallel": True},
    ),
    ({}, {"PDF_EXTRACTION_BACKEND": " FITZ "}, None, {}),
    (
        {},
        {"PDF_EXTRACTION_BACKEND": " PyMuPDF4LLM "},
        None,
        {"pdf_extraction_backend": "pymupdf4llm"},
    ),
    ({}, {"PDF_EXTRACTION_BACKEND": "auto"}, None, {"pdf_extraction_backend": "auto"}),
    ({}, {"PDF_EXTRACTION_BACKEND": "unknown"}, None, {}),
    ({}, {"PDF_EXTRACTION_BACKEND": " "}, None, {}),
    (
        {
            "cleaner_backend": "cuda",
            "cleaner_model_name": "mlx-community/Llama-3.2-3B-Instruct-4bit",
        },
        {},
        None,
        {"cleaner_model_name": "Qwen/Qwen2.5-1.5B-Instruct", "cleaner_backend": "cuda"},
    ),
    (
        {
            "cleaner_backend": "cuda",
            "cleaner_model_name": "mlx-community/Llama-3.2-3B-Instruct-4bit",
        },
        {"CLEANER_MODEL_NAME": "mlx-community/Llama-3.2-3B-Instruct-4bit"},
        None,
        {"cleaner_backend": "cuda"},
    ),
    (
        {
            "cleaner_backend": "mlx",
            "cleaner_model_name": "constructor-model",
            "tts_provider": "cpu",
        },
        {
            "CLEANER_BACKEND": "api",
            "CLEANER_MODEL_NAME": "environment-model",
            "CLEANER_CUDA_QUANTIZATION": "fp16",
        },
        {
            "profile": "cuda",
            "model_groups": ["tts-cpu-cuda", "cleaner-cuda"],
            "cleaner_cuda_quantization": "8bit",
        },
        {
            "tts_provider": "cpu",
            "cleaner_model_name": "Qwen/Qwen2.5-1.5B-Instruct",
            "cleaner_backend": "cuda",
            "cleaner_cuda_quantization": "8bit",
        },
    ),
    (
        {
            "cleaner_backend": "mlx",
            "cleaner_model_name": "constructor-model",
            "tts_provider": "cpu",
        },
        {
            "CLEANER_BACKEND": "api",
            "CLEANER_MODEL_NAME": "environment-model",
            "CLEANER_CUDA_QUANTIZATION": "fp16",
        },
        {
            "profile": "mlx",
            "model_groups": ["tts-mlx", "cleaner-mlx"],
            "cleaner_cuda_quantization": "8bit",
        },
        {"tts_provider": "cpu", "cleaner_cuda_quantization": "8bit"},
    ),
    (
        {
            "cleaner_backend": "mlx",
            "cleaner_model_name": "constructor-model",
            "tts_provider": "cpu",
        },
        {
            "CLEANER_BACKEND": "api",
            "CLEANER_MODEL_NAME": "environment-model",
            "CLEANER_CUDA_QUANTIZATION": "fp16",
        },
        {"profile": "cuda", "model_groups": ["tts-cpu-cuda"], "cleaner_cuda_quantization": "8bit"},
        {
            "tts_provider": "cpu",
            "cleaner_model_name": "environment-model",
            "cleaner_backend": "basic",
            "cleaner_cuda_quantization": "8bit",
        },
    ),
    (
        {
            "cleaner_backend": "mlx",
            "cleaner_model_name": "constructor-model",
            "tts_provider": "cpu",
        },
        {
            "CLEANER_BACKEND": "api",
            "CLEANER_MODEL_NAME": "environment-model",
            "CLEANER_CUDA_QUANTIZATION": "fp16",
        },
        {"profile": "base", "model_groups": [], "cleaner_cuda_quantization": "8bit"},
        {
            "tts_provider": "cpu",
            "cleaner_model_name": "environment-model",
            "cleaner_backend": "basic",
            "cleaner_cuda_quantization": "8bit",
        },
    ),
]


@pytest.mark.parametrize("overrides,environment,state,updates", _RESOLUTION_CASES)
def test_effective_values_match_serialized_precedence_matrix(
    monkeypatch, overrides, environment, state, updates
):
    import json

    monkeypatch.delenv("AUDIOBOOK_DATA_DIR")
    monkeypatch.setenv("HOME", "home")
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    config = AppConfig.from_dict(overrides)
    if state is not None:
        config.apply_setup_preferences(state=state)
    expected_values = _BASE_CONFIG_VALUES | updates
    for name in ("data_dir", "output_dir", "temp_dir", "db_path"):
        expected_values[name] = str(Path(expected_values[name]))
    expected = json.dumps(expected_values, sort_keys=True)
    assert json.dumps(config.to_dict(), sort_keys=True) == expected

