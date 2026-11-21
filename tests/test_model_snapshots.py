"""Installed pinned snapshots must work without a cached main branch reference."""

import ast
import logging
import re
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from modules.tts.model_snapshots import installed_model_path, installed_voice_path
from scripts.download_models import MODEL_REVISIONS, write_download_receipt


@pytest.fixture(autouse=True)
def mlx_audio_source_modules(monkeypatch):
    from modules.tts import mlx_compat

    monkeypatch.setattr(mlx_compat, "require_local_phonemizer", lambda: None)
    try:
        import mlx_audio
    except ImportError:
        return None
    package = Path(mlx_audio.__file__).parent

    def load_functions(path, names, namespace):
        tree = ast.parse(path.read_text())
        functions = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name in names
        ]
        # Run the installed parsing code without initializing native Metal.
        module = ast.Module(body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            *functions,
        ], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)

    utils = ModuleType("mlx_audio.utils")
    utils.Path = Path
    utils.re = re
    load_functions(package / "utils.py", {"get_model_name_parts"}, vars(utils))
    monkeypatch.setitem(sys.modules, "mlx_audio.utils", utils)

    pipeline = ModuleType("mlx_audio.tts.models.kokoro.pipeline")
    pipeline.Path = Path
    pipeline.logging = logging
    pipeline.mx = np
    pipeline.LANG_CODES = {}
    pipeline.snapshot_download = Mock(side_effect=AssertionError("Voice lookup attempted the Hub"))
    pipeline.load_voice_tensor = Mock(return_value=np.zeros((2, 3)))
    names = {"load_voice", "load_single_voice"}
    load_functions(package / "tts/models/kokoro/pipeline.py", names, vars(pipeline))
    pipeline.KokoroPipeline = type(
        "KokoroPipeline", (), {name: vars(pipeline)[name] for name in names},
    )
    monkeypatch.setitem(sys.modules, pipeline.__name__, pipeline)
    return pipeline


def test_resolves_curated_snapshot_at_installed_revision(monkeypatch, tmp_path):
    import huggingface_hub

    calls = []

    def snapshot_download(**kwargs):
        calls.append(kwargs)
        return str(tmp_path)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot_download)
    (tmp_path / "config.json").write_text("{}")
    for model in ("hexgrad/Kokoro-82M", "Qwen/Qwen2.5-1.5B-Instruct"):
        write_download_receipt(tmp_path, model_id=model, revision=MODEL_REVISIONS[model])
        assert installed_model_path(model) == str(tmp_path)
        assert calls[-1] == {
            "repo_id": model, "revision": MODEL_REVISIONS[model], "local_files_only": True,
        }


def test_missing_snapshot_requires_reinstallation(monkeypatch):
    import huggingface_hub
    from huggingface_hub.errors import LocalEntryNotFoundError

    def missing(**kwargs):
        raise LocalEntryNotFoundError("missing")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", missing)
    with pytest.raises(RuntimeError, match="Reinstall.*Setup"):
        installed_model_path("hexgrad/Kokoro-82M")


@pytest.mark.parametrize("cached", [True, False])
def test_custom_repository_uses_cached_reference_or_download_guidance(monkeypatch, tmp_path, cached):
    import httpx
    import huggingface_hub
    import requests
    from modules.errors import TTSInstallationError

    cache = tmp_path / "hub"
    repository = cache / "models--custom--model"
    snapshot = repository / "snapshots" / ("a" * 40)
    if cached:
        snapshot.mkdir(parents=True)
        (snapshot / "config.json").write_text("{}")
        (snapshot / "model.safetensors").write_bytes(b"weights")
        (repository / "refs").mkdir()
        (repository / "refs" / "main").write_text(snapshot.name)

    def reject_http(*args, **kwargs):
        pytest.fail("Model loading attempted an HTTP request")

    monkeypatch.setattr(requests.Session, "request", reject_http)
    monkeypatch.setattr(httpx.Client, "send", reject_http)
    download = huggingface_hub.snapshot_download

    def snapshot_download(**kwargs):
        assert kwargs == {"repo_id": "custom/model", "local_files_only": True}
        return download(cache_dir=cache, **kwargs)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot_download)
    if cached:
        assert installed_model_path("custom/model") == str(snapshot)
        return

    with pytest.raises(TTSInstallationError) as exc_info:
        installed_model_path("custom/model")
    assert str(exc_info.value) == (
        "Model custom/model is not installed. Download it with "
        "python scripts/download_models.py --model custom/model, "
        "or point the model setting at a local directory."
    )


def test_explicit_local_model_directory_is_preserved(tmp_path):
    assert installed_model_path(str(tmp_path)) == str(tmp_path)


@pytest.fixture
def offline_snapshots(monkeypatch, tmp_path):
    import httpx
    import huggingface_hub
    import requests
    from huggingface_hub import constants
    from modules.tts.runtime_spec import (
        CLEANER_MLX_DEFAULT_MODEL,
        KOKORO_MLX_DEFAULT_MODEL_REPO,
    )

    def reject_http(*args, **kwargs):
        pytest.fail("Model loading attempted an HTTP request")

    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setattr(constants, "HF_HUB_OFFLINE", True)
    monkeypatch.setattr(requests.Session, "request", reject_http)
    monkeypatch.setattr(httpx.Client, "send", reject_http)
    cache_dir = tmp_path / "hub"
    snapshots = {}
    for model in (KOKORO_MLX_DEFAULT_MODEL_REPO, CLEANER_MLX_DEFAULT_MODEL):
        repo_dir = cache_dir / ("models--" + model.replace("/", "--"))
        snapshot = repo_dir / "snapshots" / MODEL_REVISIONS[model]
        (snapshot / "voices").mkdir(parents=True)
        (snapshot / "config.json").write_text("{}")
        for voice in ("af_bella", "af_heart"):
            (snapshot / "voices" / f"{voice}.safetensors").write_bytes(b"voice")
        write_download_receipt(snapshot, model_id=model, revision=MODEL_REVISIONS[model])
        assert not (repo_dir / "refs" / "main").exists()
        snapshots[model] = snapshot

    real_download = huggingface_hub.snapshot_download

    def snapshot_download(**kwargs):
        return real_download(cache_dir=cache_dir, **kwargs)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot_download)
    return snapshots


def test_mlx_tts_loads_verified_snapshot_without_main_reference(monkeypatch, offline_snapshots):
    import huggingface_hub
    from modules.tts import mlx_compat

    model_name, snapshot = next(iter(offline_snapshots.items()))
    loaded_model = object()

    def load_model(*, model_path, **kwargs):
        if not Path(model_path).is_dir():
            huggingface_hub.snapshot_download(repo_id=model_path)
        assert Path(model_path) == snapshot
        return loaded_model

    monkeypatch.setattr(mlx_compat, "ensure_mlx_runtime", lambda: None)
    monkeypatch.setattr(mlx_compat, "patch_espeakng_loader_paths", lambda: None)
    monkeypatch.setattr(mlx_compat, "_load_tts_model_fn", load_model)
    assert mlx_compat.load_tts_model(model_name) is loaded_model


def test_mlx_cleaner_loads_verified_snapshot_without_main_reference(monkeypatch, offline_snapshots):
    import huggingface_hub
    from modules.tts import cleaner_mlx

    model_name, snapshot = list(offline_snapshots.items())[1]
    model, tokenizer = object(), object()

    def load_model(model_path, *, tokenizer_config):
        assert tokenizer_config == {"local_files_only": True}
        if not Path(model_path).is_dir():
            huggingface_hub.snapshot_download(repo_id=model_path)
        assert model_path == str(snapshot)
        return model, tokenizer

    monkeypatch.setattr(cleaner_mlx, "ensure_mlx_lm_runtime", lambda: None)
    monkeypatch.setattr(cleaner_mlx, "load", load_model)
    cleaner = SimpleNamespace(
        config=SimpleNamespace(model_name=model_name, cache_model=False),
        _loaded=False,
        _manager=Mock(),
        unload=Mock(),
    )
    cleaner_mlx.CleanerMLXStrategy().load(cleaner)
    assert (cleaner._model, cleaner._tokenizer) == (model, tokenizer)
    assert cleaner._loaded


def test_mlx_tts_snapshot_load_keeps_the_architecture_recognisable(monkeypatch, offline_snapshots):
    # Kokoro's config has no model_type, so mlx-audio infers the architecture
    # from the model name; a bare snapshot directory is named after a commit.
    mlx_audio_utils = pytest.importorskip("mlx_audio.utils")
    from modules.tts import mlx_compat

    model_name, snapshot = next(iter(offline_snapshots.items()))
    captured = {}

    def load_model(*, model_path, **kwargs):
        captured.update(kwargs, model_path=model_path)
        return object()

    monkeypatch.setattr(mlx_compat, "ensure_mlx_runtime", lambda: None)
    monkeypatch.setattr(mlx_compat, "patch_espeakng_loader_paths", lambda: None)
    monkeypatch.setattr(mlx_compat, "_load_tts_model_fn", load_model)
    mlx_compat.load_tts_model(model_name)

    assert Path(captured["model_path"]) == snapshot
    name_parts = captured.get("model_name_parts") or mlx_audio_utils.get_model_name_parts(
        captured["model_path"]
    )
    assert "kokoro" in name_parts


@pytest.mark.parametrize("output_mode", ["samples", "file", "temporary"])
@pytest.mark.parametrize("voice", ["af_bella", "af_bella,af_heart"])
@pytest.mark.parametrize("model_argument", ["model", "model_path"])
def test_mlx_generation_uses_snapshot_voice_files_without_main_reference(
    tmp_path, offline_snapshots, mlx_audio_source_modules, output_mode, voice, model_argument,
):
    if mlx_audio_source_modules is None:
        pytest.skip("mlx_audio is not installed")
    import soundfile as sf
    from modules.tts import engine_output

    model_name, snapshot = next(iter(offline_snapshots.items()))
    pipeline_module = mlx_audio_source_modules
    pipeline = pipeline_module.KokoroPipeline()
    pipeline.voices = {}
    pipeline.repo_id = "prince-canuma/Kokoro-82M"
    pipeline.lang_code = "a"

    def load_voice_tensor(path):
        assert Path(path).parent == snapshot / "voices"
        assert Path(path).read_bytes() == b"voice"
        return np.zeros((2, 3))

    pipeline_module.load_voice_tensor.side_effect = load_voice_tensor
    model = object()
    engine = SimpleNamespace(
        _model_path=model_name,
        _model=model,
        _model_lock=threading.Lock(),
        config=SimpleNamespace(sample_rate=24000),
    )

    def generate_audio(*, model_path, voice, output_path, file_prefix, **kwargs):
        assert model_path == str(snapshot)
        assert all(not voice_id.endswith(".safetensors") for voice_id in voice.split(","))
        pipeline.load_voice(voice)
        sf.write(str(Path(output_path) / f"{file_prefix}.wav"), np.zeros(24), 24000)

    def generate_with_model(*, model, voice, output_path, file_prefix, **kwargs):
        assert model is engine._model
        generate_audio(
            model_path=str(snapshot), voice=voice, output_path=output_path,
            file_prefix=file_prefix, **kwargs,
        )

    generate_fn = generate_with_model if model_argument == "model" else generate_audio
    output = tmp_path / "audio.wav"
    if output_mode == "samples":
        result = engine_output.synthesize_to_output_path(
            engine, "hello", voice, 1.0, output, generate_fn=generate_fn,
        )
        assert len(result) == 24
    elif output_mode == "file":
        assert engine_output.synthesize_to_output_file(
            engine, "hello", voice, 1.0, output, generate_fn=generate_fn,
        ) == output
    else:
        assert engine_output.generate_temp_wav_paths(
            engine, "hello", voice, 1.0, tmp_path, generate_fn=generate_fn,
        )
    assert pipeline_module.load_voice_tensor.call_count == len(voice.split(","))
    pipeline_module.snapshot_download.assert_not_called()


@pytest.mark.parametrize("output_mode", ["samples", "file", "temporary"])
@pytest.mark.parametrize("model_argument", ["model", "model_path"])
@pytest.mark.parametrize("voice", ["af_bella", "af_bella,af_heart"])
def test_mlx_voice_parser_loads_intact_snapshot_paths_containing_commas(
    monkeypatch, tmp_path, offline_snapshots, mlx_audio_source_modules, voice,
    output_mode, model_argument,
):
    if mlx_audio_source_modules is None:
        pytest.skip("mlx_audio is not installed")
    import huggingface_hub
    import soundfile as sf
    from modules.tts import engine_output

    model_name, original_snapshot = next(iter(offline_snapshots.items()))
    snapshot = tmp_path / "Jane, Doe" / "snapshot"
    snapshot.parent.mkdir()
    original_snapshot.rename(snapshot)
    def snapshot_download(**kwargs):
        assert kwargs == {
            "repo_id": model_name, "revision": MODEL_REVISIONS[model_name],
            "local_files_only": True,
        }
        return str(snapshot)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot_download)
    pipeline_module = mlx_audio_source_modules
    pipeline = pipeline_module.KokoroPipeline()
    pipeline.voices = {}
    pipeline.repo_id = "prince-canuma/Kokoro-82M"
    pipeline.lang_code = "a"
    packs = {
        "af_bella": np.full((2, 3), 1.0),
        "af_heart": np.full((2, 3), 3.0),
    }
    pipeline_module.load_voice_tensor.side_effect = lambda path: packs[Path(path).stem]
    engine = SimpleNamespace(
        _model_path=model_name, _model=object(), _model_lock=threading.Lock(),
        config=SimpleNamespace(sample_rate=24000),
    )

    def generate_audio(*, model_path, voice, output_path, file_prefix, **kwargs):
        assert model_path == str(snapshot)
        pack = pipeline.load_voice(voice)
        np.testing.assert_array_equal(pack, np.mean([packs[v] for v in voice.split(",")], axis=0))
        sf.write(str(Path(output_path) / f"{file_prefix}.wav"), np.zeros(24), 24000)

    def generate_with_model(*, model, voice, output_path, file_prefix, **kwargs):
        assert model is engine._model
        generate_audio(
            model_path=str(snapshot), voice=voice, output_path=output_path,
            file_prefix=file_prefix, **kwargs,
        )

    generate_fn = generate_with_model if model_argument == "model" else generate_audio
    output = tmp_path / "audio.wav"
    if output_mode == "samples":
        assert len(engine_output.synthesize_to_output_path(
            engine, "hello", voice, 1.0, output, generate_fn=generate_fn,
        )) == 24
    elif output_mode == "file":
        assert engine_output.synthesize_to_output_file(
            engine, "hello", voice, 1.0, output, generate_fn=generate_fn,
        ) == output
    else:
        assert engine_output.generate_temp_wav_paths(
            engine, "hello", voice, 1.0, tmp_path, generate_fn=generate_fn,
        )
    assert [call.args[0] for call in pipeline_module.load_voice_tensor.call_args_list] == [
        str(snapshot / "voices" / f"{voice_id}.safetensors") for voice_id in voice.split(",")
    ]
    pipeline_module.snapshot_download.assert_not_called()


def test_mlx_voice_loader_restores_repository_loading_after_generation_fails(
    offline_snapshots, mlx_audio_source_modules,
):
    if mlx_audio_source_modules is None:
        pytest.skip("mlx_audio is not installed")
    from modules.tts import mlx_compat

    model_name = next(iter(offline_snapshots))
    pipeline_module = mlx_audio_source_modules
    pipeline = pipeline_module.KokoroPipeline()
    pipeline.voices = {}
    pipeline.repo_id = "custom/model"
    pipeline.lang_code = "a"
    with pytest.raises(RuntimeError, match="Generation failed"):
        with mlx_compat.use_installed_voices(model_name):
            pipeline.load_voice("af_bella")
            raise RuntimeError("Generation failed")
    pipeline.voices = {}
    with pytest.raises(AssertionError, match="Voice lookup attempted the Hub"):
        pipeline.load_voice("af_bella")
    assert pipeline_module.snapshot_download.call_args.kwargs["repo_id"] == "custom/model"


def test_mlx_loaders_accept_explicit_local_paths(monkeypatch, tmp_path):
    from modules.tts import cleaner_mlx, mlx_compat

    tts_load, cleaner_load = Mock(), Mock(return_value=(object(), object()))
    monkeypatch.setattr(mlx_compat, "ensure_mlx_runtime", lambda: None)
    monkeypatch.setattr(mlx_compat, "patch_espeakng_loader_paths", lambda: None)
    monkeypatch.setattr(mlx_compat, "_load_tts_model_fn", tts_load)
    monkeypatch.setattr(cleaner_mlx, "ensure_mlx_lm_runtime", lambda: None)
    monkeypatch.setattr(cleaner_mlx, "load", cleaner_load)
    for model_name in (str(tmp_path),):
        mlx_compat.load_tts_model(model_name)
        assert tts_load.call_args.kwargs["model_path"] == tmp_path
        cleaner = SimpleNamespace(
            config=SimpleNamespace(model_name=model_name, cache_model=False),
            _loaded=False, _manager=Mock(), unload=Mock(),
        )
        cleaner_mlx.CleanerMLXStrategy().load(cleaner)
        cleaner_load.assert_called_with(model_name, tokenizer_config={"local_files_only": True})
        with pytest.raises(RuntimeError, match="Reinstall.*Setup"):
            installed_voice_path(model_name, "af_bella")


def test_installed_voice_preserves_explicit_voice_files(offline_snapshots, tmp_path):
    model_name = next(iter(offline_snapshots))
    voice_file = tmp_path / "custom.safetensors"
    voice_file.write_bytes(b"custom voice")
    assert installed_voice_path(model_name, str(voice_file)) == str(voice_file)


def test_missing_installed_voice_fails_without_hub_fallback(offline_snapshots):
    model_name = next(iter(offline_snapshots))
    with pytest.raises(RuntimeError, match="Voice af_missing is missing.*Reinstall"):
        installed_voice_path(model_name, "af_missing")


def test_damaged_snapshot_is_rejected_before_mlx_loading(monkeypatch, offline_snapshots):
    from modules.tts import cleaner_mlx, mlx_compat

    loader = Mock()
    monkeypatch.setattr(mlx_compat, "ensure_mlx_runtime", lambda: None)
    monkeypatch.setattr(mlx_compat, "patch_espeakng_loader_paths", lambda: None)
    monkeypatch.setattr(mlx_compat, "_load_tts_model_fn", loader)
    monkeypatch.setattr(cleaner_mlx, "ensure_mlx_lm_runtime", lambda: None)
    monkeypatch.setattr(cleaner_mlx, "load", loader)
    for model_name, snapshot in offline_snapshots.items():
        (snapshot / "config.json").write_text("damaged")
        with pytest.raises(RuntimeError, match="Incomplete or damaged cache"):
            mlx_compat.load_tts_model(model_name)
        cleaner = SimpleNamespace(
            config=SimpleNamespace(model_name=model_name, cache_model=False),
            _loaded=False, _manager=Mock(), unload=Mock(),
        )
        with pytest.raises(RuntimeError, match="Incomplete or damaged cache"):
            cleaner_mlx.CleanerMLXStrategy().load(cleaner)
    loader.assert_not_called()
