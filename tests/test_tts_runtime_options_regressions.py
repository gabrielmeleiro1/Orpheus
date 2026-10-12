"""Regression coverage for tts runtime options."""

from types import SimpleNamespace

import pytest

from modules.tts.runtime_options import TTSRuntimeOptions
from modules.tts.runtime_spec import TTSRuntimeSpec


@pytest.mark.parametrize("source", ["payload", "fields"])
def test_explicit_auto_overrides_configured_cuda(monkeypatch, source):
    monkeypatch.setattr(TTSRuntimeSpec, "mlx_supported_on_current_platform", lambda: False)
    monkeypatch.setattr(TTSRuntimeSpec, "cuda_available_on_current_platform", lambda: False)
    config = SimpleNamespace(tts_provider="cuda", tts_device="cuda:1", tts_quantization="bf16")
    request = (SimpleNamespace(tts_options={"provider": "auto", "device": "auto"})
               if source == "payload" else SimpleNamespace(tts_provider="auto", tts_device="auto"))
    spec = TTSRuntimeOptions.from_runtime_options(request, config).to_runtime_spec("kokoro", config)
    assert spec.provider == "cpu"
    assert spec.device is None


def test_cuda_payload_accepts_auto_device():
    options = TTSRuntimeOptions.from_payload({"provider": "cuda", "device": "auto"})
    assert options.provider == "cuda"
    assert options.device is None
