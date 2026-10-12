"""Unit tests for the CUDA cleaner backend (mocked torch)."""

from __future__ import annotations

import asyncio
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import pytest

from modules.tts.cleaner_config import CleanerConfig, CleanTrace
from modules.tts.cleaner_cuda import (
    CleanerCUDAStrategy,
    clear_cleaner_cache,
    ensure_cuda_runtime,
)
from modules.tts.cleaner_errors import CleanerRuntimeMemoryError, CleanerRuntimeUnavailable
from tests.fixtures.cleaner_cuda_helpers import (
    make_clean_trace,
    make_fake_torch_module,
    make_partial_cuda_cleaner,
    reset_cuda_runtime_state,
)


@pytest.fixture(autouse=True)
def _reset_cuda_runtime(monkeypatch):
    import modules.tts.cleaner_cuda as cleaner_cuda_module

    reset_cuda_runtime_state(cleaner_cuda_module)
    clear_cleaner_cache()
    yield
    reset_cuda_runtime_state(cleaner_cuda_module)
    clear_cleaner_cache()


def test_ensure_cuda_runtime_raises_import_error_when_torch_import_fails(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _fail_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "torch":
            raise ImportError("No module named 'torch'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _fail_import)

    with pytest.raises(ImportError, match="CUDA cleaner runtime unavailable"):
        ensure_cuda_runtime()


def test_ensure_cuda_runtime_raises_when_cuda_is_unavailable(monkeypatch):
    import sys

    fake_torch = make_fake_torch_module(cuda_available=False)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    with pytest.raises(ImportError, match="CUDA cleaner runtime unavailable"):
        ensure_cuda_runtime()


def test_load_cuda_model_maps_explicit_cuda_device_for_transformers(monkeypatch, tmp_path):
    import sys
    import types

    import modules.tts.cleaner_cuda as cleaner_cuda_module

    monkeypatch.setattr(cleaner_cuda_module, "installed_model_path", lambda name: str(tmp_path))
    fake_torch = make_fake_torch_module(cuda_available=True)
    observed: dict[str, object] = {}

    class _FakeTokenizer:
        pad_token = None
        eos_token = "<eos>"

        @classmethod
        def from_pretrained(cls, model_name, *, local_files_only):
            assert local_files_only is True
            observed["tokenizer_model_name"] = model_name
            return cls()

    class _FakeModel:
        @classmethod
        def from_pretrained(cls, model_name, **kwargs):
            observed["model_name"] = model_name
            observed.update(kwargs)
            return cls()

    class _FakeBitsAndBytesConfig:
        def __init__(self, **kwargs):
            observed["quantization_config"] = kwargs

    fake_transformers = types.SimpleNamespace(
        AutoModelForCausalLM=_FakeModel,
        AutoTokenizer=_FakeTokenizer,
        BitsAndBytesConfig=_FakeBitsAndBytesConfig,
    )
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    cleaner = make_partial_cuda_cleaner(
        CleanerConfig(
            backend="cuda",
            model_name="meta-llama/Llama-3.2-3B-Instruct",
            cuda_device="cuda:1",
            cuda_quantization="4bit",
            cache_model=False,
        ),
    )

    cleaner_cuda_module._load_cuda_model(cleaner, fake_torch)

    assert observed["device_map"] == {"": "cuda:1"}


def test_default_cuda_quantization_dependency_is_declared():
    requirements = (
        Path(__file__).parent.parent / "requirements" / "cuda.txt"
    ).read_text(
        encoding="utf-8",
    )

    assert "bitsandbytes" in requirements
    assert "accelerate" in requirements


@pytest.mark.parametrize("use_chat_template", [False, True])
def test_generate_llm_response_is_greedy_and_passes_max_tokens(monkeypatch, use_chat_template):
    import modules.tts.cleaner_cuda as cleaner_cuda_module

    fake_torch = make_fake_torch_module(cuda_available=True)
    monkeypatch.setattr(
        cleaner_cuda_module,
        "ensure_cuda_runtime",
        lambda: (fake_torch,),
    )

    generate_kwargs: dict[str, object] = {}

    class _FakeOutputRow:
        def __getitem__(self, idx):
            if isinstance(idx, slice):
                return [7, 8, 9]
            return list(range(10))

    class _FakeOutput:
        def __getitem__(self, idx):
            return _FakeOutputRow()

    class _FakeModel:
        def parameters(self):
            yield SimpleNamespace(device="cuda:0")

        def generate(self, **kwargs):
            generate_kwargs.update(kwargs)
            return _FakeOutput()

    class _FakeTensor:
        def __init__(self, shape):
            self.shape = shape

        def to(self, _device):
            return self

    class _FakeTokenizer:
        pad_token_id = 0
        eos_token_id = 2
        chat_template = "model chat template" if use_chat_template else None

        def apply_chat_template(self, messages, **kwargs):
            assert use_chat_template
            assert messages == [{"role": "user", "content": "prompt text"}]
            assert kwargs == {
                "add_generation_prompt": True,
                "tokenize": True,
                "return_dict": True,
                "return_tensors": "pt",
            }
            return {"input_ids": _FakeTensor(shape=[1, 5])}

        def __call__(self, prompt, return_tensors="pt"):
            assert not use_chat_template
            _ = prompt, return_tensors
            return {"input_ids": _FakeTensor(shape=[1, 5])}

        def decode(self, _token_ids, skip_special_tokens=True):
            _ = skip_special_tokens
            return "cleaned output"

    cleaner = make_partial_cuda_cleaner(
        CleanerConfig(
            backend="cuda",
            model_name="meta-llama/Llama-3.2-3B-Instruct",
            temperature=0.35,
            max_tokens=128,
        ),
        loaded=True,
    )
    cleaner._model = _FakeModel()
    cleaner._tokenizer = _FakeTokenizer()

    result = CleanerCUDAStrategy().generate_llm_response(cleaner, "prompt text")

    assert result == "cleaned output"
    assert generate_kwargs["max_new_tokens"] == 128
    assert "temperature" not in generate_kwargs
    assert generate_kwargs["do_sample"] is False
    assert generate_kwargs["num_beams"] == 1
    assert generate_kwargs["pad_token_id"] == 0


def test_model_cache_hit_skips_reload_when_cache_model_true(monkeypatch, tmp_path):
    import modules.tts.cleaner_cuda as cleaner_cuda_module

    monkeypatch.setattr(cleaner_cuda_module, "installed_model_path", lambda name: str(tmp_path))
    fake_torch = make_fake_torch_module(cuda_available=True)
    monkeypatch.setattr(
        cleaner_cuda_module,
        "ensure_cuda_runtime",
        lambda: (fake_torch,),
    )

    load_calls = 0

    def _fake_load_cuda_model(_cleaner, _torch_module):
        nonlocal load_calls
        load_calls += 1
        return object(), object()

    monkeypatch.setattr(cleaner_cuda_module, "_load_cuda_model", _fake_load_cuda_model)

    config = CleanerConfig(
        backend="cuda",
        model_name="meta-llama/Llama-3.2-3B-Instruct",
        cuda_device="cuda:0",
        cuda_quantization="4bit",
        cache_model=True,
    )
    cleaner = make_partial_cuda_cleaner(config)
    strategy = CleanerCUDAStrategy()

    strategy.load(cleaner)
    assert load_calls == 1
    assert cleaner._loaded is True

    second_cleaner = make_partial_cuda_cleaner(config)
    strategy.load(second_cleaner)

    assert load_calls == 1
    assert second_cleaner._loaded is True


def test_unload_without_cache_calls_torch_cuda_empty_cache(monkeypatch):
    import modules.tts.cleaner_cuda as cleaner_cuda_module

    fake_torch = make_fake_torch_module(cuda_available=True)
    cleaner_cuda_module.TORCH_CUDA_AVAILABLE = True
    cleaner_cuda_module.torch = fake_torch

    cleaner = make_partial_cuda_cleaner(
        CleanerConfig(backend="cuda", cache_model=True),
        loaded=True,
    )
    strategy = CleanerCUDAStrategy()

    strategy.unload(cleaner, keep_in_cache=False)

    assert cleaner._loaded is False
    assert fake_torch.cuda.empty_cache.call_count >= 1


def test_clean_batch_with_trace_async_preserves_order():
    class _FakeCleaner:
        def __init__(self):
            self._lock = threading.Lock()
            self.active_calls = 0
            self.peak_active_calls = 0

        def clean_with_trace(self, text: str) -> CleanTrace:
            with self._lock:
                self.active_calls += 1
                self.peak_active_calls = max(self.peak_active_calls, self.active_calls)
            try:
                time.sleep(0.02)
                return make_clean_trace(text, cleaned_suffix=" cleaned")
            finally:
                with self._lock:
                    self.active_calls -= 1

    cleaner = _FakeCleaner()
    strategy = CleanerCUDAStrategy()
    texts = ["alpha", "beta", "gamma"]

    traces = asyncio.run(strategy.clean_batch_with_trace_async(cleaner, texts))

    assert [trace.original_text for trace in traces] == texts
    assert [trace.cleaned_text for trace in traces] == [
        "alpha cleaned",
        "beta cleaned",
        "gamma cleaned",
    ]
    assert all(isinstance(trace, CleanTrace) for trace in traces)
    assert cleaner.peak_active_calls == 1


def test_load_raises_cleaner_runtime_unavailable_when_torch_missing(monkeypatch):
    import modules.tts.cleaner_cuda as cleaner_cuda_module

    def _fail_runtime():
        raise ImportError("No module named 'torch'")

    monkeypatch.setattr(cleaner_cuda_module, "ensure_cuda_runtime", _fail_runtime)

    cleaner = make_partial_cuda_cleaner(
        CleanerConfig(backend="cuda", cache_model=False),
    )

    with pytest.raises(CleanerRuntimeUnavailable, match="CUDA cleaner runtime unavailable"):
        CleanerCUDAStrategy().load(cleaner)


def test_load_cuda_model_oom_raises_cleaner_runtime_memory_error(monkeypatch):
    import modules.tts.cleaner_cuda as cleaner_cuda_module

    fake_torch = make_fake_torch_module(cuda_available=True)
    oom_error = fake_torch.cuda.OutOfMemoryError

    def _raise_oom(*_args, **_kwargs):
        raise oom_error("CUDA out of memory")

    monkeypatch.setattr(cleaner_cuda_module, "ensure_cuda_runtime", lambda: (fake_torch,))
    monkeypatch.setattr(cleaner_cuda_module, "_load_cuda_model", _raise_oom)

    cleaner = make_partial_cuda_cleaner(
        CleanerConfig(backend="cuda", cache_model=False),
    )

    with pytest.raises(CleanerRuntimeMemoryError, match="CUDA out of memory"):
        CleanerCUDAStrategy().load(cleaner)
