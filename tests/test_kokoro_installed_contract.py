"""Check the installed package APIs used by the CPU/CUDA Kokoro adapter."""

import importlib.util
import inspect
import os
from pathlib import Path

import numpy as np
import pytest

from tests._factories_group2_hub import isolate_hub_credentials, isolate_hub_environment

pytestmark = [pytest.mark.integration, pytest.mark.tts]


@pytest.fixture
def kokoro(tmp_path, monkeypatch):
    home = tmp_path / "hf-home"
    root = home / "hub"
    isolate_hub_environment(monkeypatch, home)
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("AUDIOBOOK_TTS_TORCH_COMPILE", "0")
    pytest.importorskip("huggingface_hub")
    from huggingface_hub import constants

    isolate_hub_credentials(monkeypatch, home)

    monkeypatch.setattr(constants, "HF_HOME", str(home))
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(root))
    monkeypatch.setattr(constants, "HUGGINGFACE_HUB_CACHE", str(root))
    monkeypatch.setattr(constants, "HF_HUB_OFFLINE", True)
    for effective_root in (
        constants.HF_HOME, constants.HF_HUB_CACHE, constants.HUGGINGFACE_HUB_CACHE,
        constants.HF_TOKEN_PATH, constants.HF_STORED_TOKENS_PATH,
    ):
        assert Path(effective_root).resolve().is_relative_to(tmp_path.resolve()), (
            f"Hub cache escaped the temporary directory: {effective_root}"
        )
    yield pytest.importorskip("kokoro", exc_type=ImportError)


def test_pipeline_and_model_accept_adapter_constructor_arguments(kokoro):
    inspect.signature(kokoro.KPipeline).bind(
        lang_code="a", repo_id="hexgrad/Kokoro-82M", device="cpu",
    )
    inspect.signature(kokoro.KPipeline).bind(lang_code="a", model=False, device="cpu")
    inspect.signature(kokoro.KModel).bind(
        repo_id="hexgrad/Kokoro-82M", config="config.json", model="kokoro-v1_0.pth",
    )
    inspect.signature(kokoro.KPipeline.__call__).bind(
        object(), "Hello.", voice="af_heart", speed=1.0,
    )
    inspect.signature(kokoro.KPipeline.load_single_voice).bind(object(), "voice.pt")
    assert callable(kokoro.KModel.to)
    assert callable(kokoro.KModel.eval)


def test_local_voice_loading_returns_tensor_and_accepts_tensor_voice(kokoro, tmp_path):
    import torch

    path = tmp_path / "声 author's, voice.pt"
    expected = torch.arange(12, dtype=torch.float32).reshape(3, 4)
    torch.save(expected, path)
    # Voice loading does not need a model or a language front end.
    pipeline = kokoro.KPipeline.__new__(kokoro.KPipeline)
    pipeline.voices = {}
    voice = pipeline.load_single_voice(str(path))
    assert isinstance(voice, torch.Tensor)
    assert torch.equal(voice, expected)
    assert pipeline.load_single_voice(str(path)) is voice
    assert pipeline.load_voice(voice) is voice


def test_pipeline_result_unpacks_to_text_phonemes_and_numpy_compatible_audio(kokoro):
    import torch

    expected = torch.tensor([0.0, 0.25, -0.25], dtype=torch.float32)
    result = kokoro.KPipeline.Result(
        graphemes="Hello", phonemes="həloʊ", output=kokoro.KModel.Output(audio=expected),
    )
    graphemes, phonemes, audio = result
    assert (graphemes, phonemes) == ("Hello", "həloʊ")
    converted = np.asarray(audio, dtype=np.float32)
    assert converted.ndim == 1
    np.testing.assert_array_equal(converted, expected.numpy())


@pytest.mark.slow
def test_offline_weights_produce_audio_through_package_runtime(kokoro, tmp_path):
    configured = os.environ.get("ORPHEUS_TEST_HF_HOME")
    if not configured:
        pytest.skip("ORPHEUS_TEST_HF_HOME is not set")
    home = Path(configured).expanduser().resolve()
    forbidden = (Path.home() / ".cache/huggingface",
                 Path.home() / "Library/Application Support/com.orpheus.desktop")
    if any(home.is_relative_to(path.resolve()) for path in forbidden):
        pytest.skip("ORPHEUS_TEST_HF_HOME must use a dedicated test cache")
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError
    from modules.tts.runtime_spec import TTSRuntimeSpec
    from modules.tts.runtimes.kokoro_base import KokoroRuntimeConfig
    from modules.tts.runtimes.kokoro_package import PackageKokoroRuntime
    from scripts.download_models import pinned_revision_for

    repo = "hexgrad/Kokoro-82M"
    try:
        snapshot = Path(snapshot_download(
            repo, revision=pinned_revision_for(repo), cache_dir=home / "hub",
            local_files_only=True,
        ))
    except LocalEntryNotFoundError:
        pytest.skip("Pinned Kokoro weights are not available in the offline test cache")
    required = ("config.json", "kokoro-v1_0.pth", "voices/af_heart.pt")
    if not all((snapshot / name).is_file() for name in required):
        pytest.skip("Offline Kokoro snapshot lacks model, config, or af_heart voice")
    if importlib.util.find_spec("en_core_web_sm") is None:
        pytest.skip("Offline English phonemizer model en_core_web_sm is not installed")
    config = KokoroRuntimeConfig(
        runtime_spec=TTSRuntimeSpec("cpu", "kokoro", str(snapshot), "fp32", device="cpu"),
        model_name=str(snapshot), quantization="fp32", sample_rate=24000,
        default_voice="af_heart",
    )
    runtime = PackageKokoroRuntime(config)
    try:
        runtime.load()
        audio = runtime.synthesize("Hello.", "af_heart", output_path=tmp_path / "hello.wav")
        assert isinstance(audio, np.ndarray)
        assert audio.dtype == np.float32
        assert audio.ndim == 1 and audio.size > 0
        assert np.isfinite(audio).all()
        assert np.any(audio != 0)
    finally:
        runtime.unload(keep_in_cache=False)


def test_fixture_isolates_previously_imported_credentials(tmp_path, monkeypatch, request):
    pytest.importorskip("huggingface_hub")
    from huggingface_hub import constants

    for name in ("HF_TOKEN_PATH", "HF_STORED_TOKENS_PATH"):
        previous = str(tmp_path / "previous-home" / name.lower())
        monkeypatch.setenv(name, previous)
        monkeypatch.setattr(constants, name, previous)
    for name in (
        "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HF_OIDC_RESOURCE", "HF_OIDC_ID_TOKEN",
        "OAUTH_CLIENT_ID", "OAUTH_CLIENT_SECRET", "OAUTH_SCOPES",
    ):
        monkeypatch.setenv(name, "unused-test-credential")
    request.getfixturevalue("kokoro")

    home = tmp_path / "hf-home"
    for name in ("HF_TOKEN_PATH", "HF_STORED_TOKENS_PATH"):
        assert Path(getattr(constants, name)).is_relative_to(home)
        assert Path(os.environ[name]).is_relative_to(home)
    for name in (
        "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HF_OIDC_RESOURCE", "HF_OIDC_ID_TOKEN",
        "OAUTH_CLIENT_ID", "OAUTH_CLIENT_SECRET", "OAUTH_SCOPES",
    ):
        assert name not in os.environ
    from huggingface_hub.utils import build_hf_headers, get_token, get_stored_tokens

    assert get_token() is None
    assert get_stored_tokens() == {}
    assert "authorization" not in build_hf_headers()
