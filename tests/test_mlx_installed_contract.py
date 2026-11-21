"""Validate installed MLX interfaces in isolated native-runtime processes."""

import importlib.util
import os
from pathlib import Path
import platform
import subprocess
import sys
import textwrap

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.tts, pytest.mark.mlx]


@pytest.fixture(scope="module")
def mlx_environment(tmp_path_factory):
    if sys.platform != "darwin" or platform.machine() != "arm64":
        pytest.skip("MLX requires Darwin arm64")
    if importlib.util.find_spec("mlx_audio") is None:
        pytest.skip("mlx_audio is not installed")
    home = tmp_path_factory.mktemp("mlx-contract")
    env = dict(os.environ, HF_HOME=str(home), HF_HUB_CACHE=str(home / "hub"),
               HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
               XDG_CACHE_HOME=str(home / "cache"), PYTHONDONTWRITEBYTECODE="1")
    try:
        probe = subprocess.run(
            [sys.executable, "-c", "import mlx.core; import mlx_audio"],
            env=env, capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        pytest.skip("Native MLX import exceeded 30 seconds")
    if probe.returncode < 0 or "Metal" in probe.stderr or "NSException" in probe.stderr:
        pytest.skip(f"Native MLX import unavailable in this environment (exit {probe.returncode})")
    assert probe.returncode == 0, probe.stderr
    return env


def _run(env, source, timeout=45):
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)], env=env,
        capture_output=True, text=True, timeout=timeout,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_tts_entry_points_accept_adapter_arguments(mlx_environment):
    _run(mlx_environment, """
        import inspect
        from pathlib import Path
        from mlx_audio.tts.generate import generate_audio
        from mlx_audio.tts.utils import load_model
        from mlx_audio.utils import get_model_name_parts
        from modules.tts import mlx_compat

        inspect.signature(load_model).bind(
            model_path=Path('/offline/kokoro'),
            model_name_parts=get_model_name_parts('mlx-community/Kokoro-82M-bf16'),
        )
        kwargs = dict(text='Hello.', voice='af_heart', speed=1.0,
                      lang_code='a', audio_format='wav', play=False, verbose=False)
        model = object()
        mlx_compat.inject_generate_audio_model_arg(
            kwargs, '/offline/kokoro', loaded_model=model, generate_fn=generate_audio,
        )
        mlx_compat.inject_generate_audio_output_args(
            kwargs, '/offline/output', 'chapter', generate_fn=generate_audio,
        )
        assert {'model', 'model_path'} & kwargs.keys()
        inspect.signature(generate_audio).bind(**kwargs)
    """)


def test_compatibility_patches_find_installed_targets(mlx_environment):
    _run(mlx_environment, """
        import inspect
        import mlx.nn as nn
        import espeakng_loader
        from mlx_audio.tts.models.base import GenerationResult
        from mlx_audio.tts.models.kokoro.pipeline import KokoroPipeline
        from modules.tts import mlx_compat
        from tqdm import std
        from tqdm.auto import tqdm

        weights = inspect.signature(nn.Module.load_weights).parameters
        assert {'weights', 'file_or_weights'} & weights.keys()
        changed = mlx_compat.patch_mlx_load_weights_compat(nn.Module)
        assert changed == ('weights' not in weights and 'file_or_weights' in weights)
        if changed:
            assert nn.Module.load_weights._audiobook_weights_compat

        params = inspect.signature(GenerationResult).parameters
        assert 'sample_rate' in params
        changed = mlx_compat.patch_mlx_generation_result_compat(GenerationResult)
        assert changed == (params['sample_rate'].default is inspect.Parameter.empty)
        if changed:
            assert GenerationResult.__init__._audiobook_sample_rate_compat

        inspect.signature(KokoroPipeline.load_single_voice).bind(object(), 'af_heart')
        assert mlx_compat.patch_mlx_voice_loader_compat(KokoroPipeline)
        assert KokoroPipeline.load_single_voice._audiobook_voice_compat
        assert not mlx_compat.patch_mlx_voice_loader_compat(KokoroPipeline)

        inspect.signature(espeakng_loader.get_library_path).bind()
        inspect.signature(espeakng_loader.get_data_path).bind()
        mlx_compat.patch_espeakng_loader_paths()
        assert isinstance(espeakng_loader.get_data_path(), str)
        assert isinstance(espeakng_loader.get_library_path(), str)
        assert callable(std.tqdm.set_lock) and callable(tqdm.set_lock)
        mlx_compat.ensure_tqdm_thread_lock()
        assert mlx_compat._TQDM_LOCK_PATCHED
        assert std.tqdm.get_lock() is tqdm.get_lock()
    """)


def test_cleaner_load_generate_and_tokenizer_interfaces(mlx_environment):
    if importlib.util.find_spec("mlx_lm") is None:
        pytest.skip("mlx_lm is not installed")
    _run(mlx_environment, """
        import inspect
        from mlx_lm.generate import generate_step
        from mlx_lm.tokenizer_utils import TokenizerWrapper
        from modules.tts.cleaner_mlx import ensure_mlx_lm_runtime
        from tokenizers import Tokenizer
        from tokenizers.models import WordLevel
        from transformers import PreTrainedTokenizerFast

        mx, load, stream_generate, make_sampler = ensure_mlx_lm_runtime()
        inspect.signature(load).bind('/offline/cleaner')
        sampler = make_sampler(temp=0.0)
        assert callable(sampler)
        inspect.signature(stream_generate).bind(
            object(), object(), prompt='Hello', max_tokens=1, sampler=sampler,
        )
        assert {'max_tokens', 'sampler'} <= inspect.signature(generate_step).parameters.keys()
        tokenizer = TokenizerWrapper(PreTrainedTokenizerFast(
            tokenizer_object=Tokenizer(WordLevel({'[UNK]': 0, 'Hello': 1}, unk_token='[UNK]')),
            unk_token='[UNK]', eos_token='[UNK]',
            chat_template="{{ messages[0]['content'] }}",
        ))
        assert tokenizer.encode('Hello') == [1]
        assert tokenizer.decode([1]) == 'Hello'
        assert tokenizer.apply_chat_template(
            [{'role': 'user', 'content': 'Hello'}], tokenize=False,
            add_generation_prompt=True,
        ) == 'Hello'
    """)


@pytest.mark.slow
def test_offline_kokoro_weights_generate_wav(mlx_environment, tmp_path):
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
    from modules.tts.runtime_spec import KOKORO_MLX_DEFAULT_MODEL_REPO
    from scripts.download_models import pinned_revision_for

    repo = KOKORO_MLX_DEFAULT_MODEL_REPO
    try:
        snapshot = Path(snapshot_download(
            repo, revision=pinned_revision_for(repo), cache_dir=home / "hub",
            local_files_only=True,
        ))
    except LocalEntryNotFoundError:
        pytest.skip("Pinned MLX Kokoro weights are not available in the offline test cache")
    if not ((snapshot / 'config.json').is_file()
            and list(snapshot.glob('*.safetensors'))
            and (snapshot / 'voices/af_heart.safetensors').is_file()):
        pytest.skip("Offline MLX snapshot lacks model, config, or af_heart voice")
    if importlib.util.find_spec("en_core_web_sm") is None:
        pytest.skip("Offline English phonemizer model en_core_web_sm is not installed")
    env = dict(mlx_environment, HOME=str(tmp_path / "home"),
               HF_HOME=str(home), HF_HUB_CACHE=str(home / "hub"),
               ORPHEUS_CONTRACT_OUTPUT=str(tmp_path), ORPHEUS_CONTRACT_REPO=repo)
    _run(env, """
        import os
        from pathlib import Path
        import numpy as np
        import soundfile as sf
        from apps.launchers.desktop_backend import configure_espeak_runtime
        from modules.tts import mlx_compat

        configure_espeak_runtime()
        mx, generate = mlx_compat.ensure_mlx_runtime()
        mlx_compat.ensure_tqdm_thread_lock()
        repo = os.environ['ORPHEUS_CONTRACT_REPO']
        model = mlx_compat.load_tts_model(repo)
        kwargs = dict(text='Hello.', voice='af_heart', speed=1.0,
                      audio_format='wav', verbose=False)
        mlx_compat.inject_generate_audio_model_arg(
            kwargs, repo, loaded_model=model, generate_fn=generate,
        )
        mlx_compat.inject_generate_audio_output_args(
            kwargs, os.environ['ORPHEUS_CONTRACT_OUTPUT'], 'hello', generate_fn=generate,
        )
        with mlx_compat.use_installed_voices(repo):
            generate(**kwargs)
        files = list(Path(os.environ['ORPHEUS_CONTRACT_OUTPUT']).glob('*.wav'))
        assert files
        audio, rate = sf.read(files[0])
        assert rate == 24000 and audio.size > 0
        assert np.isfinite(audio).all() and np.any(audio != 0)
    """, timeout=120)
