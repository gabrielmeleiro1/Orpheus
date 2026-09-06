"""CUDA cleaner model lifecycle warmup tests."""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from modules.tts.cleaner_memory import CLEANER_CUDA_LOAD_HEADROOM_BYTES
from modules.tts.cleaner_errors import CleanerRuntimeMemoryError
from modules.tts.device_memory import DeviceMemorySnapshot

GIB = 1024**3


def _cuda_warmup_pipeline() -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(
            tts_engine="kokoro",
            tts_model_name="mlx-community/Kokoro-82M",
            tts_quantization="bf16",
            voice="af_bella",
            cleaner_model_name="meta-llama/Llama-3.2-3B-Instruct",
            cleaner_backend="cuda",
            cleaner_cuda_device="cuda:0",
            cleaner_cuda_quantization="4bit",
            cleaner_api_base_url="https://api.openai.com/v1",
            cleaner_api_key_env="OPENAI_API_KEY",
            cleaner_api_key=None,
            cleaner_api_model="gpt-5-nano",
            cleaner_api_global_concurrency=6,
            cleaner_api_global_limiter=None,
            cleaner_skip_llm_when_not_needed=False,
            memory_policy=None,
        ),
        state=SimpleNamespace(tts_engine=None, text_cleaner=None),
        log_verbose=MagicMock(),
    )


def test_warmup_loads_tts_before_cleaner_without_error() -> None:
    model_lifecycle = importlib.import_module("modules.pipeline.model_lifecycle")
    pipeline = _cuda_warmup_pipeline()
    call_order: list[str] = []

    fake_engine = MagicMock()
    fake_engine.is_loaded = False
    fake_engine.load.side_effect = lambda: call_order.append("tts_load")
    fake_engine.warmup.side_effect = lambda **_: call_order.append("tts_warmup")

    fake_cleaner = MagicMock()
    fake_cleaner.load.side_effect = lambda: call_order.append("cleaner_load")

    with patch(
        "modules.tts.factory.TTSEngineFactory.create",
        return_value=fake_engine,
    ), patch(
        "modules.tts.cleaner.TextCleaner",
        return_value=fake_cleaner,
    ), patch.object(
        model_lifecycle,
        "_maybe_warn_cleaner_cuda_memory_low",
    ):
        model_lifecycle.warmup_models(pipeline)

    assert call_order == ["tts_load", "tts_warmup", "cleaner_load"]
    fake_engine.load.assert_called_once_with()
    fake_engine.warmup.assert_called_once_with(voice="af_bella")
    fake_cleaner.load.assert_called_once_with()


def test_warmup_defers_cleaner_load_when_skip_llm_gate_enabled() -> None:
    model_lifecycle = importlib.import_module("modules.pipeline.model_lifecycle")
    pipeline = _cuda_warmup_pipeline()
    pipeline.config.cleaner_skip_llm_when_not_needed = True
    call_order: list[str] = []

    fake_engine = MagicMock()
    fake_engine.is_loaded = False
    fake_engine.load.side_effect = lambda: call_order.append("tts_load")
    fake_engine.warmup.side_effect = lambda **_: call_order.append("tts_warmup")

    fake_cleaner = MagicMock()
    fake_cleaner.load.side_effect = lambda: call_order.append("cleaner_load")

    with patch(
        "modules.tts.factory.TTSEngineFactory.create",
        return_value=fake_engine,
    ), patch(
        "modules.tts.cleaner.TextCleaner",
        return_value=fake_cleaner,
    ), patch.object(
        model_lifecycle,
        "_maybe_warn_cleaner_cuda_memory_low",
    ):
        model_lifecycle.warmup_models(pipeline)

    assert call_order == ["tts_load", "tts_warmup"]
    assert pipeline.state.text_cleaner is fake_cleaner
    fake_cleaner.load.assert_not_called()


def test_get_text_cleaner_for_chapter_defers_load_when_skip_llm_gate_enabled() -> None:
    model_lifecycle = importlib.import_module("modules.pipeline.model_lifecycle")
    pipeline = _cuda_warmup_pipeline()
    pipeline.config.cleaner_skip_llm_when_not_needed = True
    pipeline.state.parallel_execution_active = False
    fake_cleaner = MagicMock()

    with patch(
        "modules.tts.cleaner.TextCleaner",
        return_value=fake_cleaner,
    ), patch.object(
        model_lifecycle,
        "_maybe_warn_cleaner_cuda_memory_low",
    ):
        cleaner = model_lifecycle.get_text_cleaner_for_chapter(pipeline)

    assert cleaner is fake_cleaner
    fake_cleaner.load.assert_not_called()


def test_warmup_warns_when_cuda_memory_below_estimate_plus_headroom() -> None:
    model_lifecycle = importlib.import_module("modules.pipeline.model_lifecycle")
    pipeline = _cuda_warmup_pipeline()
    fake_engine = MagicMock()
    fake_engine.is_loaded = False
    fake_cleaner = MagicMock()
    low_available = 2 * GIB
    snapshot = DeviceMemorySnapshot(
        provider="cuda",
        device="cuda:0",
        total_bytes=24 * GIB,
        available_bytes=low_available,
    )

    with patch(
        "modules.tts.factory.TTSEngineFactory.create",
        return_value=fake_engine,
    ), patch(
        "modules.tts.cleaner.TextCleaner",
        return_value=fake_cleaner,
    ), patch.object(
        model_lifecycle.CUDADeviceMemoryPolicy,
        "sample",
        return_value=snapshot,
    ):
        model_lifecycle.warmup_models(pipeline)

    expected_message = (
        "[WARMUP] CUDA memory may be insufficient for cleaner load "
        f"(available={low_available / GIB:.1f} GiB, "
        f"estimate={3.0:.1f} GiB + "
        f"headroom={CLEANER_CUDA_LOAD_HEADROOM_BYTES / GIB:.1f} GiB)"
    )
    pipeline.log_verbose.assert_any_call(expected_message, "warning")


def test_warmup_cleaner_cuda_oom_raises_classified_memory_error() -> None:
    model_lifecycle = importlib.import_module("modules.pipeline.model_lifecycle")
    pipeline = _cuda_warmup_pipeline()
    fake_engine = MagicMock()
    fake_engine.is_loaded = False
    fake_cleaner = MagicMock()
    fake_cleaner.load.side_effect = CleanerRuntimeMemoryError("CUDA out of memory")

    with patch(
        "modules.tts.factory.TTSEngineFactory.create",
        return_value=fake_engine,
    ), patch(
        "modules.tts.cleaner.TextCleaner",
        return_value=fake_cleaner,
    ), patch.object(
        model_lifecycle,
        "_maybe_warn_cleaner_cuda_memory_low",
    ):
        with pytest.raises(CleanerRuntimeMemoryError, match="CUDA out of memory"):
            model_lifecycle.warmup_models(pipeline)
