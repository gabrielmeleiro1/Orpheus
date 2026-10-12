"""Regression coverage for kokoro package."""

from types import SimpleNamespace
from unittest.mock import Mock
import builtins
import sys

from modules import hf_cache
from modules.tts.runtime_spec import TTSRuntimeSpec


def test_direct_package_load_configures_hub_before_kokoro(monkeypatch, tmp_path):
    from modules.tts.runtimes.kokoro_base import KokoroRuntimeConfig
    from modules.tts.runtimes.kokoro_package import PackageKokoroRuntime

    configured = Mock()
    monkeypatch.setattr(hf_cache, "configure_hf_cache", configured)
    monkeypatch.setattr("modules.tts.model_cache.get_cached_model", lambda key: None)
    monkeypatch.setattr("modules.tts.model_cache.set_cached_model", lambda *args: None)
    monkeypatch.setattr(PackageKokoroRuntime, "_local_model_dir", lambda self: tmp_path)
    monkeypatch.setattr(
        sys.modules[PackageKokoroRuntime.__module__], "require_local_phonemizer", lambda: None,
    )
    original_import = builtins.__import__

    def checked_import(name, *args, **kwargs):
        if name == "kokoro":
            configured.assert_called_once()
            return SimpleNamespace(KPipeline=lambda **kwargs: object(), KModel=Mock())
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", checked_import)
    spec = TTSRuntimeSpec.from_legacy("kokoro", "bf16", provider="cpu")
    PackageKokoroRuntime(KokoroRuntimeConfig(
        runtime_spec=spec, model_name=spec.resolved_model(), quantization="bf16", sample_rate=24000,
    )).load()
