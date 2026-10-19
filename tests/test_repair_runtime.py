from __future__ import annotations

import json
from datetime import datetime

import pytest

from modules.app.services.conversion_runtime import ConversionRuntimeOptions
from modules.app.services.repair_runtime import (
    RepairRuntimeUnavailableError,
    effective_runtime_options_for_persistence,
    resolve_repair_runtime_options,
    serialize_persisted_runtime_options,
)
from modules.storage.models import Book, ConversionMode, PlaybackStatus, SourceType


def _book(*, conversion_runtime_options: str | None = None) -> Book:
    return Book(
        id=7,
        title="Repair Book",
        author="Tester",
        source_path="/tmp/book.md",
        source_type=SourceType.MD,
        conversion_mode=ConversionMode.AUDIOBOOK,
        total_chapters=2,
        playback_path="/tmp/output/book.m4b",
        playback_status=PlaybackStatus.PACKAGED,
        playback_position_sec=0.0,
        created_at=datetime(2026, 4, 1, 9, 0, 0),
        updated_at=datetime(2026, 4, 1, 9, 5, 0),
        conversion_runtime_options=conversion_runtime_options,
    )


def test_serialize_persisted_runtime_options_omits_parallel_and_api_key_fields() -> None:
    payload = serialize_persisted_runtime_options(
        ConversionRuntimeOptions(
            voice="af_bella",
            speed=1.35,
            tts_engine="kokoro",
            tts_quantization="bf16",
            cleaner_backend="api",
            cleaner_api_model="gpt-5-nano",
            cleaner_api_key="sk-secret",
            tts_options={"quantization": "bf16"},
            parallel_worker_mode="manual",
            enable_parallel=True,
            max_parallel_chapters=4,
        )
    )

    assert payload["voice"] == "af_bella"
    assert payload["speed"] == 1.35
    assert payload["tts_engine"] == "kokoro"
    assert "cleaner_api_key" not in payload
    assert "parallel_worker_mode" not in payload
    assert "enable_parallel" not in payload
    assert "max_parallel_chapters" not in payload


def test_serialize_persisted_runtime_options_strips_nested_credentials() -> None:
    payload = serialize_persisted_runtime_options(
        ConversionRuntimeOptions(
            voice="af_bella",
            tts_options={
                "quantization": "bf16",
                "provider_options": {
                    "device_id": 0,
                    "api_key": "sk-secret",
                    "nested": {
                        "token": "secret-token",
                        "dtype": "fp16",
                    },
                },
                "authorization": "Bearer secret",
            },
        )
    )

    assert payload["tts_options"] == {
        "quantization": "bf16",
        "provider_options": {
            "device_id": 0,
            "nested": {"dtype": "fp16"},
        },
    }


def test_resolve_repair_runtime_options_uses_persisted_settings_when_request_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    persisted = serialize_persisted_runtime_options(
        ConversionRuntimeOptions(
            voice="af_bella",
            speed=1.35,
            tts_engine="kokoro",
            tts_quantization="bf16",
            cleaner_backend="api",
            cleaner_api_model="gpt-5-nano",
            tts_options={"quantization": "bf16"},
            parallel_worker_mode="manual",
            enable_parallel=True,
            max_parallel_chapters=4,
        )
    )
    book = _book(conversion_runtime_options=json.dumps(persisted))

    resolved = resolve_repair_runtime_options(
        book=book,
        request_options=ConversionRuntimeOptions(),
        request_fields_set=frozenset(),
    )

    assert resolved.voice == "af_bella"
    assert resolved.speed == 1.35
    assert resolved.tts_engine == "kokoro"
    assert resolved.parallel_worker_mode == "auto"
    assert resolved.enable_parallel is None
    assert resolved.max_parallel_chapters is None


def test_resolve_repair_runtime_options_falls_back_to_app_defaults_without_history(tmp_path) -> None:
    from modules.app.config import AppConfig

    config = AppConfig(
        data_dir=tmp_path / "data", output_dir=tmp_path / "output", temp_dir=tmp_path / "temp",
        default_voice="am_adam",
        default_speed=1.0,
        tts_engine="kokoro",
        tts_quantization="bf16",
        cleaner_backend="mlx",
        cleaner_model_name="mlx-community/Llama-3.2-3B-Instruct-4bit",
    )
    resolved = resolve_repair_runtime_options(
        book=_book(conversion_runtime_options=None),
        request_options=ConversionRuntimeOptions(),
        request_fields_set=frozenset(),
        app_config=config,
    )

    assert resolved.voice == "am_adam"
    assert resolved.speed == 1.0
    assert resolved.tts_engine == "kokoro"
    assert resolved.tts_quantization == "bf16"
    assert resolved.cleaner_backend == "mlx"
    assert resolved.cleaner_model == "mlx-community/Llama-3.2-3B-Instruct-4bit"
    assert resolved.parallel_worker_mode == "auto"
    assert resolved.enable_parallel is None
    assert resolved.max_parallel_chapters is None


def _persisted_api_cleaner_book() -> Book:
    persisted = serialize_persisted_runtime_options(
        ConversionRuntimeOptions(
            voice="af_bella",
            speed=1.35,
            tts_engine="kokoro",
            cleaner_backend="api",
            cleaner_api_model="gpt-5-nano",
        )
    )
    return _book(conversion_runtime_options=json.dumps(persisted))


def test_resolve_repair_runtime_options_rejects_cloud_cleaner_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(RepairRuntimeUnavailableError, match="cleaner API key"):
        resolve_repair_runtime_options(
            book=_persisted_api_cleaner_book(),
            request_options=ConversionRuntimeOptions(),
            request_fields_set=frozenset(),
        )


def test_resolve_repair_runtime_options_accepts_explicit_cleaner_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    resolved = resolve_repair_runtime_options(
        book=_persisted_api_cleaner_book(),
        request_options=ConversionRuntimeOptions(cleaner_api_key="sk-live"),
        request_fields_set=frozenset({"cleaner_api_key"}),
    )

    assert resolved.cleaner_api_key == "sk-live"
    assert resolved.cleaner_backend == "api"
    assert resolved.voice == "af_bella"


def test_resolve_repair_runtime_options_allows_explicit_override_without_history() -> None:
    resolved = resolve_repair_runtime_options(
        book=_book(conversion_runtime_options=None),
        request_options=ConversionRuntimeOptions(
            voice="af_bella",
            speed=1.2,
            tts_engine="kokoro",
        ),
        request_fields_set=frozenset({"voice", "speed", "tts_engine"}),
    )

    assert resolved.voice == "af_bella"
    assert resolved.speed == 1.2
    assert resolved.parallel_worker_mode == "auto"


def test_legacy_partial_override_falls_back_to_app_config_for_unspecified_fields(tmp_path) -> None:
    """BUG3: legacy books must not fill unspecified narration fields from
    ConversionRuntimeOptions() defaults when any field is explicit."""
    from modules.app.config import AppConfig

    config = AppConfig(
        data_dir=tmp_path / "data", output_dir=tmp_path / "output", temp_dir=tmp_path / "temp",
        default_voice="am_echo",
        default_speed=1.4,
        tts_engine="kokoro",
        tts_quantization="fp16",
        cleaner_backend="mlx",
        cleaner_model_name="mlx-community/custom-cleaner",
    )
    resolved = resolve_repair_runtime_options(
        book=_book(conversion_runtime_options=None),
        request_options=ConversionRuntimeOptions(voice="af_bella"),
        request_fields_set=frozenset({"voice"}),
        app_config=config,
    )

    assert resolved.voice == "af_bella"
    assert resolved.speed == 1.4
    assert resolved.tts_engine == "kokoro"
    assert resolved.tts_quantization == "fp16"
    assert resolved.cleaner_backend == "mlx"
    assert resolved.cleaner_model == "mlx-community/custom-cleaner"
    assert resolved.parallel_worker_mode == "auto"


def test_effective_runtime_options_for_persistence_fills_omitted_request_fields() -> None:
    from types import SimpleNamespace

    from modules.app.services.conversion_runtime import NormalizedConversionRuntimeOptions
    from modules.tts.runtime_options import TTSRuntimeOptions
    from modules.tts.runtime_spec import TTSRuntimeSpec

    request = ConversionRuntimeOptions(voice="af_bella", speed=1.1)
    tts_runtime = TTSRuntimeSpec.from_legacy(
        engine="kokoro",
        quantization="bf16",
        provider="mlx",
        device="mps",
    )
    normalized = NormalizedConversionRuntimeOptions(
        voice="af_bella",
        speed=1.1,
        tts_engine="kokoro",
        tts_quantization="bf16",
        tts_model_name=None,
        tts_runtime=tts_runtime,
        tts_runtime_options=TTSRuntimeOptions(),
        cleaner_model="mlx-community/Llama-3.2-3B-Instruct-4bit",
        cleaner_backend="mlx",
        cleaner_cuda_device="cuda:0",
        cleaner_cuda_quantization="4bit",
        cleaner_api_model=None,
        cleaner_api_key=None,
        enable_parallel=False,
        max_parallel_chapters=1,
        parallel_worker_mode="auto",
        worker_auto_mode="memory_safe",
        process_safe=False,
        memory_policy=None,
        device_memory_policy=SimpleNamespace(),  # type: ignore[arg-type]
    )

    effective = effective_runtime_options_for_persistence(
        request_options=request,
        normalized=normalized,
    )
    payload = serialize_persisted_runtime_options(effective)

    assert payload["voice"] == "af_bella"
    assert payload["speed"] == 1.1
    assert payload["tts_engine"] == "kokoro"
    assert payload["tts_quantization"] == "bf16"
    assert payload["tts_provider"] == "mlx"
    assert payload["tts_device"] == "mps"
    assert payload["cleaner_backend"] == "mlx"
    assert payload["cleaner_model"] == "mlx-community/Llama-3.2-3B-Instruct-4bit"
