"""Tests for the cleaner backend registry."""

from __future__ import annotations

import asyncio

import pytest

from modules.tts.cleaner import TextCleaner
from modules.tts.cleaner_api import CleanerAPIStrategy
from modules.tts.cleaner_backends import (
    available_cleaner_backends,
    get_cleaner_backend,
    list_cleaner_backend_models,
    register_cleaner_backend,
    validate_cleaner_backend,
)
from modules.tts.cleaner_cuda import CleanerCUDAStrategy
from modules.tts.cleaner_mlx import CleanerMLXStrategy
from modules.tts.cleaner_config import (
    CUDA_CLEANER_MODEL_CHOICES,
    CUDA_CLEANER_QUANTIZATIONS,
    CleanerConfig,
    list_cleaner_models,
)
from tests.fixtures.fake_cleaner_backend import FakeCleanerBackend


def test_available_cleaner_backends_returns_sorted_builtins():
    assert available_cleaner_backends() == ("api", "basic", "cuda", "mlx")


def test_get_cleaner_backend_api_returns_api_strategy():
    backend = get_cleaner_backend("api")

    assert isinstance(backend, CleanerAPIStrategy)
    assert backend.name == "api"
    assert backend.is_local is False
    assert backend.requires_model is False
    assert backend.requires_api_key is True


def test_get_cleaner_backend_mlx_returns_mlx_strategy():
    backend = get_cleaner_backend("mlx")

    assert isinstance(backend, CleanerMLXStrategy)
    assert backend.name == "mlx"
    assert backend.is_local is True
    assert backend.requires_model is True
    assert backend.requires_api_key is False


def test_get_cleaner_backend_cuda_returns_cuda_strategy():
    backend = get_cleaner_backend("cuda")

    assert isinstance(backend, CleanerCUDAStrategy)
    assert backend.name == "cuda"
    assert backend.is_local is True
    assert backend.requires_model is True
    assert backend.requires_api_key is False
    assert backend.display_name == "Local CUDA"


def test_unknown_backend_raises_value_error_with_clear_message():
    with pytest.raises(ValueError, match=(
        r"Text cleaner backend 'unknown' is not supported\. "
        r"Available backends: api, basic, cuda, mlx\."
    )):
        validate_cleaner_backend("unknown")


def test_list_cleaner_backend_models_uses_backend_list_models():
    mlx_models = list_cleaner_backend_models("mlx")
    api_models = list_cleaner_backend_models("api")
    cuda_models = list_cleaner_backend_models("cuda")

    assert mlx_models
    assert api_models
    assert cuda_models
    assert mlx_models != api_models
    assert cuda_models == CUDA_CLEANER_MODEL_CHOICES
    assert cuda_models != list_cleaner_models()


def test_registering_fake_backend_lets_text_cleaner_use_it(registry_snapshot):
    register_cleaner_backend("fake", FakeCleanerBackend)

    cleaner = TextCleaner(CleanerConfig(backend="fake"))
    backend = cleaner._backend_strategy

    backend.load(cleaner)
    assert cleaner.is_loaded is True
    assert backend.generate_llm_response(cleaner, "prompt") == "fake-response:prompt"

    traces = asyncio.run(backend.clean_batch_with_trace_async(cleaner, ["one", "two"]))
    assert [trace.cleaned_text for trace in traces] == ["fake:one", "fake:two"]


def test_cleaner_facade_does_not_reexport_config_helpers():
    import modules.tts.cleaner as cleaner_module

    assert hasattr(cleaner_module, "CleanerAPIHTTPError")

    removed_exports = (
        "CleanerConfig",
        "CleanTrace",
        "CLEANER_MODEL_CHOICES",
        "API_MODEL_CHOICES",
        "mx",
        "load",
        "generate",
        "make_sampler",
        "MLX_LM_AVAILABLE",
        "urlrequest",
        "_ensure_mlx_lm_runtime",
        "_get_cached_cleaner",
        "_set_cached_cleaner",
        "_clear_cleaner_cache",
        "_CleanerAPIHTTPError",
    )

    for export_name in removed_exports:
        assert not hasattr(cleaner_module, export_name)


def test_benchmark_cleaner_backend_choices_match_registry():
    from scripts.benchmark_nagel_pipeline import cleaner_backend_choices

    assert cleaner_backend_choices() == available_cleaner_backends()


def test_benchmark_cuda_quantization_choices_match_config():
    from scripts.benchmark_nagel_pipeline import cleaner_cuda_quantization_choices

    assert cleaner_cuda_quantization_choices() == CUDA_CLEANER_QUANTIZATIONS


def test_parallel_worker_benchmark_reuses_benchmark_choices():
    from scripts import benchmark_nagel_pipeline
    from tools import benchmark_parallel_workers

    assert benchmark_parallel_workers.cleaner_backend_choices is benchmark_nagel_pipeline.cleaner_backend_choices
    assert (
        benchmark_parallel_workers.cleaner_cuda_quantization_choices
        is benchmark_nagel_pipeline.cleaner_cuda_quantization_choices
    )
