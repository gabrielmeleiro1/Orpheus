"""A transient CUDA OOM retries the chunk once after releasing cached device memory."""

from __future__ import annotations

import sys

import numpy as np
import pytest

from modules.tts.runtime_spec import TTSRuntimeSpec
from modules.tts.runtimes.errors import TTSRuntimeMemoryError
from modules.tts.runtimes.kokoro_base import KokoroRuntimeConfig
from modules.tts.runtimes.kokoro_package import PackageKokoroRuntime


class _FlakyPipeline:
    def __init__(self, failures: list[BaseException]) -> None:
        self.failures = failures
        self.calls = 0

    def load_single_voice(self, path: str) -> str:
        return path

    def __call__(self, text, *, voice, speed):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        yield text, "", np.ones(120, dtype=np.float32)


def _runtime(monkeypatch, tmp_path, provider: str, failures: list[BaseException]):
    (tmp_path / "voices").mkdir()
    (tmp_path / "voices" / "am_adam.pt").write_bytes(b"voice")
    monkeypatch.setattr(
        sys.modules[PackageKokoroRuntime.__module__], "installed_model_path", lambda name: str(tmp_path),
    )
    spec = TTSRuntimeSpec.from_legacy("kokoro", "bf16", provider=provider)
    runtime = PackageKokoroRuntime(KokoroRuntimeConfig(
        runtime_spec=spec, model_name=spec.resolved_model(), quantization="bf16", sample_rate=24000,
    ))
    runtime._pipeline = _FlakyPipeline(failures)
    return runtime


@pytest.fixture
def emptied(monkeypatch):
    torch = pytest.importorskip("torch")
    calls: list[int] = []
    monkeypatch.setattr(torch.cuda, "empty_cache", lambda: calls.append(1))
    return torch, calls


def test_cuda_oom_releases_cache_and_retries_once(monkeypatch, tmp_path, emptied):
    torch, calls = emptied
    oom = torch.cuda.OutOfMemoryError("CUDA out of memory. Tried to allocate 64.00 MiB")
    runtime = _runtime(monkeypatch, tmp_path, "cuda", [oom])

    audio = runtime.synthesize("A chunk under memory pressure.", "am_adam")

    assert audio.size == 120
    assert runtime._pipeline.calls == 2
    assert calls == [1]


def test_repeated_cuda_oom_still_fails_the_chunk(monkeypatch, tmp_path, emptied):
    torch, calls = emptied
    failures = [torch.cuda.OutOfMemoryError("CUDA out of memory") for _ in range(2)]
    runtime = _runtime(monkeypatch, tmp_path, "cuda", failures)

    with pytest.raises(TTSRuntimeMemoryError, match="CUDA out of memory") as raised:
        runtime.synthesize("Still no memory.", "am_adam")
    assert isinstance(raised.value.__cause__, torch.cuda.OutOfMemoryError)
    assert runtime._pipeline.calls == 2
    assert calls == [1]


@pytest.mark.parametrize("provider,error", [
    ("cuda", ValueError("phonemizer failed")),
    ("cpu", RuntimeError("CUDA out of memory")),
])
def test_other_failures_are_not_retried(monkeypatch, tmp_path, emptied, provider, error):
    _torch, calls = emptied
    runtime = _runtime(monkeypatch, tmp_path, provider, [error])

    with pytest.raises(type(error)):
        runtime.synthesize("No retry here.", "am_adam")
    assert runtime._pipeline.calls == 1
    assert calls == []
