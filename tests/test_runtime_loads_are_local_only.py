"""Runtime inference uses installed assets without initiating downloads."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from modules.tts import cleaner_cuda, cleaner_mlx, engine_runtime, mlx_compat, model_snapshots
from modules.tts.runtime_spec import (
    CLEANER_CUDA_DEFAULT_MODEL,
    CLEANER_MLX_DEFAULT_MODEL,
    KOKORO_MLX_DEFAULT_MODEL_REPO,
    TTSRuntimeSpec,
)
from modules.tts.runtimes import KokoroRuntimeConfig
from modules.tts.runtimes.kokoro_package import PackageKokoroRuntime


@pytest.fixture
def missing_hub(monkeypatch):
    import huggingface_hub
    from huggingface_hub.errors import LocalEntryNotFoundError

    def snapshot_download(*args, **kwargs):
        assert kwargs.get("local_files_only") is True, "Network-capable snapshot lookup"
        raise LocalEntryNotFoundError("Snapshot absent")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot_download)
    monkeypatch.setattr(
        huggingface_hub, "hf_hub_download",
        Mock(side_effect=AssertionError("Network-capable file lookup")),
    )
    monkeypatch.setattr(mlx_compat, "ensure_mlx_runtime", lambda: None)
    monkeypatch.setattr(mlx_compat, "patch_espeakng_loader_paths", lambda: None)
    monkeypatch.setattr(
        mlx_compat, "_load_tts_model_fn",
        lambda **kwargs: snapshot_download(repo_id=kwargs["model_path"]),
    )
    return snapshot_download


def conversion_engine():
    return SimpleNamespace(
        config=SimpleNamespace(model_name="mlx-community/Kokoro-82M", quantization="bf16"),
        _model_loaded=False, _model_path=KOKORO_MLX_DEFAULT_MODEL_REPO,
        _get_model_path=lambda: KOKORO_MLX_DEFAULT_MODEL_REPO,
    )


def test_conversion_model_load_requires_installed_snapshot(monkeypatch, missing_hub):
    monkeypatch.setattr(engine_runtime, "_get_cached_model", lambda key: None)
    with pytest.raises(RuntimeError, match="Reinstall.*Setup"):
        engine_runtime.load_model(conversion_engine())


def test_preview_requires_installed_snapshot(monkeypatch, missing_hub, tmp_path):
    from modules.app.config import AppConfig
    from modules.app.services.voice_service import VoiceService

    monkeypatch.setattr(engine_runtime, "_get_cached_model", lambda key: None)
    engine = conversion_engine()
    strategy = SimpleNamespace(
        load=lambda: engine_runtime.load_model(engine), unload=Mock(),
    )
    service = VoiceService(AppConfig(
        data_dir=tmp_path / "data", output_dir=tmp_path / "output", temp_dir=tmp_path,
    ), tts_factory=Mock())
    monkeypatch.setattr(service, "_create_tts_strategy", lambda spec: strategy)
    with pytest.raises(RuntimeError, match="Reinstall.*Setup"):
        service.generate_voice_preview("af_heart", text="Hello again.", tts_provider="mlx")
    strategy.unload.assert_called_once_with(keep_in_cache=False)
    assert service.metrics_snapshot()["preview_generation_count"] == 0


def test_mlx_cleaner_requires_installed_snapshot(monkeypatch, missing_hub):
    monkeypatch.setattr(cleaner_mlx, "ensure_mlx_lm_runtime", lambda: None)
    monkeypatch.setattr(cleaner_mlx, "load", lambda path, **kw: missing_hub(repo_id=path))
    cleaner = SimpleNamespace(
        config=SimpleNamespace(model_name=CLEANER_MLX_DEFAULT_MODEL, cache_model=False),
        _loaded=False, _manager=Mock(), unload=Mock(),
    )
    with pytest.raises(RuntimeError, match="Reinstall.*Setup"):
        cleaner_mlx.CleanerMLXStrategy().load(cleaner)
    assert not cleaner._loaded


@pytest.mark.parametrize("provider", ["cpu", "cuda"])
def test_package_kokoro_requires_installed_snapshot(monkeypatch, missing_hub, provider):
    from modules.tts import model_cache

    monkeypatch.setattr(model_cache, "get_cached_model", lambda key: None)
    monkeypatch.setitem(sys.modules, "kokoro", SimpleNamespace(
        KPipeline=lambda **kwargs: missing_hub(repo_id=kwargs["repo_id"]),
    ))
    runtime = PackageKokoroRuntime(KokoroRuntimeConfig(
        runtime_spec=TTSRuntimeSpec(provider=provider, engine="kokoro", model="hexgrad/Kokoro-82M", quantization="bf16"),
        model_name="hexgrad/Kokoro-82M", quantization="bf16", sample_rate=24000,
    ))
    with pytest.raises(RuntimeError, match="Reinstall.*Setup"):
        runtime.load()
    assert not runtime.is_loaded


@pytest.mark.parametrize("quantization", ["fp16", "4bit", "8bit"])
def test_cuda_cleaner_passes_local_only_to_all_loaders(monkeypatch, tmp_path, quantization):
    calls = []

    def from_pretrained(path, **kwargs):
        assert kwargs.get("local_files_only") is True, "Network-capable transformers lookup"
        assert Path(path) == tmp_path
        calls.append(kwargs)
        return SimpleNamespace(pad_token=None, eos_token="end")

    loader = SimpleNamespace(from_pretrained=from_pretrained)
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(
        AutoTokenizer=loader, AutoModelForCausalLM=loader,
        BitsAndBytesConfig=lambda **kw: kw,
    ))
    cleaner = SimpleNamespace(config=SimpleNamespace(
        model_name=str(tmp_path), cuda_device="cuda", cuda_quantization=quantization,
    ))
    cleaner_cuda._load_cuda_model(cleaner, SimpleNamespace(float16="float16"))
    assert len(calls) == 2


def test_cuda_cleaner_requires_installed_snapshot(monkeypatch, missing_hub):
    loader = SimpleNamespace(from_pretrained=lambda path, **kw: missing_hub(repo_id=path, **kw))
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(
        AutoTokenizer=loader, AutoModelForCausalLM=loader,
    ))
    cleaner = SimpleNamespace(config=SimpleNamespace(
        model_name=CLEANER_CUDA_DEFAULT_MODEL, cuda_device="cuda", cuda_quantization="fp16",
    ))
    with pytest.raises(RuntimeError, match="Reinstall.*Setup"):
        cleaner_cuda._load_cuda_model(cleaner, SimpleNamespace(float16="float16"))


def test_mlx_cleaner_tokenizer_is_local_only(monkeypatch, tmp_path):
    def load(path, **kwargs):
        assert Path(path) == tmp_path
        assert kwargs.get("tokenizer_config", {}).get("local_files_only") is True
        return object(), object()

    monkeypatch.setattr(cleaner_mlx, "ensure_mlx_lm_runtime", lambda: None)
    monkeypatch.setattr(cleaner_mlx, "load", load)
    cleaner = SimpleNamespace(
        config=SimpleNamespace(model_name=str(tmp_path), cache_model=False),
        _loaded=False, _manager=Mock(), unload=Mock(),
    )
    cleaner_mlx.CleanerMLXStrategy().load(cleaner)
    assert cleaner._loaded


@pytest.mark.parametrize("loader_kind", ["tts", "cleaner"])
def test_cached_custom_repository_loads_without_network(monkeypatch, tmp_path, loader_kind):
    import httpx
    import huggingface_hub
    import requests

    cache = tmp_path / "hub"
    repository = cache / "models--custom--model"
    snapshot = repository / "snapshots" / ("a" * 40)
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}")
    (snapshot / "model.safetensors").write_bytes(b"weights")
    (repository / "refs").mkdir()
    (repository / "refs" / "main").write_text(snapshot.name)
    download = huggingface_hub.snapshot_download

    def reject_http(*args, **kwargs):
        pytest.fail("Model loading attempted an HTTP request")

    monkeypatch.setattr(httpx.Client, "send", reject_http)
    monkeypatch.setattr(requests.Session, "request", reject_http)

    monkeypatch.setitem(sys.modules, "mlx_audio.utils", SimpleNamespace(
        get_model_name_parts=lambda name: name.split("/"),
    ))

    def snapshot_download(**kwargs):
        assert kwargs == {"repo_id": "custom/model", "local_files_only": True}
        return download(cache_dir=cache, **kwargs)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot_download)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", Mock(
        side_effect=AssertionError("Network-capable file lookup"),
    ))
    monkeypatch.setattr(model_snapshots._receipt_verifier, "verify", Mock(
        side_effect=AssertionError("Custom snapshots do not require catalog receipts"),
    ))
    monkeypatch.setattr(mlx_compat, "ensure_mlx_runtime", lambda: None)
    monkeypatch.setattr(mlx_compat, "patch_espeakng_loader_paths", lambda: None)
    loader = Mock(return_value=object())
    monkeypatch.setattr(mlx_compat, "_load_tts_model_fn", loader)

    if loader_kind == "tts":
        assert mlx_compat.load_tts_model("custom/model") is loader.return_value
        assert Path(loader.call_args.kwargs["model_path"]) == snapshot
    else:
        monkeypatch.setattr(cleaner_mlx, "ensure_mlx_lm_runtime", lambda: None)
        cleaner_loader = Mock(return_value=(object(), object()))
        monkeypatch.setattr(cleaner_mlx, "load", cleaner_loader)
        cleaner = SimpleNamespace(
            config=SimpleNamespace(model_name="custom/model", cache_model=False),
            _loaded=False, _manager=Mock(), unload=Mock(),
        )
        cleaner_mlx.CleanerMLXStrategy().load(cleaner)
        assert cleaner._loaded
        cleaner_loader.assert_called_once_with(
            str(snapshot), tokenizer_config={"local_files_only": True},
        )


def test_uncached_custom_repository_explains_how_to_install(missing_hub):
    with pytest.raises(RuntimeError) as exc_info:
        model_snapshots.installed_model_path("custom/model")
    message = str(exc_info.value)
    assert "python scripts/download_models.py --model custom/model" in message
    assert "local directory" in message
    assert "Reinstall" not in message


def test_local_model_voice_never_falls_back_to_repository(monkeypatch, tmp_path):
    monkeypatch.setattr(mlx_compat, "require_local_phonemizer", lambda: None)
    class Pipeline:
        def load_single_voice(self, voice):
            assert Path(voice).is_file(), "Voice lookup would reach the Hub"
            return voice

    monkeypatch.setitem(sys.modules, "mlx_audio.tts.models.kokoro.pipeline", SimpleNamespace(
        KokoroPipeline=Pipeline,
    ))
    pipeline = Pipeline()
    pipeline.voices = {}
    with mlx_compat.use_installed_voices(str(tmp_path)):
        with pytest.raises(RuntimeError, match="Reinstall.*Setup"):
            pipeline.load_single_voice("af_heart")


def test_missing_phonemizer_package_fails_before_download(monkeypatch, tmp_path):
    import importlib.metadata
    from modules.errors import TTSModelError
    from modules.tts import model_cache

    def missing_distribution(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "distribution", missing_distribution)
    monkeypatch.setattr(model_cache, "get_cached_model", lambda key: None)
    model = Mock()
    model.to.return_value.eval.return_value = model
    monkeypatch.setitem(sys.modules, "kokoro", SimpleNamespace(
        KModel=lambda **kwargs: model,
        KPipeline=Mock(side_effect=AssertionError("Phonemizer would download a package")),
    ))
    runtime = PackageKokoroRuntime(KokoroRuntimeConfig(
        runtime_spec=TTSRuntimeSpec(provider="cpu", engine="kokoro", model=str(tmp_path), quantization="bf16"),
        model_name=str(tmp_path), quantization="bf16", sample_rate=24000,
    ))
    with pytest.raises(RuntimeError, match="Reinstall.*Setup") as exc_info:
        runtime.load()
    assert isinstance(exc_info.value, TTSModelError)
    assert exc_info.value.message == "The English phonemizer model is missing. Reinstall the runtime from Setup."


def test_package_model_disappearing_before_load_never_uses_repository(monkeypatch, tmp_path):
    from modules.tts import model_cache
    from modules.tts.runtimes import kokoro_package

    monkeypatch.setattr(model_cache, "get_cached_model", lambda key: None)
    monkeypatch.setattr(kokoro_package, "installed_model_path", lambda name: str(tmp_path / "missing"))
    monkeypatch.setitem(sys.modules, "kokoro", SimpleNamespace(
        KPipeline=Mock(side_effect=AssertionError("Repository loading would download")),
    ))
    runtime = PackageKokoroRuntime(KokoroRuntimeConfig(
        runtime_spec=TTSRuntimeSpec(
            provider="cpu", engine="kokoro", model="hexgrad/Kokoro-82M", quantization="bf16",
        ),
        model_name="hexgrad/Kokoro-82M", quantization="bf16", sample_rate=24000,
    ))
    with pytest.raises(RuntimeError, match="Reinstall.*Setup"):
        runtime.load()


def test_missing_snapshots_remain_unavailable_in_setup(missing_hub):
    from modules.runtime_setup.hub_cache import _check_model_group_assets

    groups = ["tts-mlx", "tts-cpu-cuda", "cleaner-mlx", "cleaner-cuda"]
    status = _check_model_group_assets(groups, cache_scanner=lambda: SimpleNamespace(repos=[]))
    assert all(status[group] == {"ready": False, "reason": "missing"} for group in groups)


def test_mlx_generation_requires_installed_phonemizer(monkeypatch, tmp_path):
    import importlib.metadata

    def missing_distribution(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "distribution", missing_distribution)
    monkeypatch.setitem(sys.modules, "mlx_audio.tts.models.kokoro.pipeline", SimpleNamespace(
        KokoroPipeline=type("Pipeline", (), {"load_single_voice": Mock()}),
    ))
    with pytest.raises(RuntimeError, match="Reinstall.*Setup"):
        with mlx_compat.use_installed_voices(str(tmp_path)):
            pytest.fail("Generation would let the phonemizer download a package")


def test_package_model_constructor_failure_does_not_retry_without_local_model(monkeypatch, tmp_path):
    from modules.tts import model_cache

    monkeypatch.setattr(model_cache, "get_cached_model", lambda key: None)
    pipeline = Mock(side_effect=AssertionError("Pipeline would download its default model"))
    monkeypatch.setitem(sys.modules, "kokoro", SimpleNamespace(
        KModel=Mock(side_effect=TypeError("Invalid local model")), KPipeline=pipeline,
    ))
    runtime = PackageKokoroRuntime(KokoroRuntimeConfig(
        runtime_spec=TTSRuntimeSpec(
            provider="cpu", engine="kokoro", model=str(tmp_path), quantization="bf16",
        ),
        model_name=str(tmp_path), quantization="bf16", sample_rate=24000,
    ))
    with pytest.raises(RuntimeError, match="Invalid local model"):
        runtime.load()
    pipeline.assert_not_called()
