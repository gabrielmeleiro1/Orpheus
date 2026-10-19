"""Resolution uses supplied inputs and keeps diagnostic values private."""

import builtins
from copy import deepcopy
import os
from pathlib import Path
from types import MappingProxyType

import pytest

from modules.app.config_resolution import resolve_config
from modules.runtime_setup.catalog import MODEL_GROUPS


def test_resolution_does_not_read_process_state_or_touch_files(monkeypatch):
    environment = {"HOME": "supplied-home", "AUDIOBOOK_DATA_DIR": "~/data", "TTS_DEVICE": "cpu"}
    overrides = {"cleaner_backend": "api"}
    state = {"profile": "cuda", "model_groups": ["cleaner-cuda"], "cleaner_cuda_quantization": "8bit"}
    before = deepcopy((environment, overrides, state, dict(MODEL_GROUPS)))

    def forbidden(*args, **kwargs):
        raise AssertionError("Resolution must only use supplied inputs")

    class UnreadableEnvironment(dict):
        __getitem__ = get = __iter__ = __setitem__ = forbidden

    with monkeypatch.context() as guard:
        guard.setattr(os, "environ", UnreadableEnvironment())
        guard.setattr(os, "getenv", forbidden)
        guard.setattr(builtins, "open", forbidden)
        for name in ("mkdir", "open", "read_text", "write_text", "exists", "is_file", "home", "expanduser"):
            guard.setattr(Path, name, forbidden)
        first = resolve_config(
            MappingProxyType(environment), MappingProxyType(overrides),
            setup_state=MappingProxyType(state), model_groups=MappingProxyType(MODEL_GROUPS),
        )
        second = resolve_config(environment, overrides, setup_state=state, model_groups=MODEL_GROUPS)
    assert first == second
    assert first.values["data_dir"] == Path("supplied-home/data")
    assert first.values["tts_device"] == "cpu"
    assert (environment, overrides, state, dict(MODEL_GROUPS)) == before
    first.values["tts_device"] = "changed"
    assert second.values["tts_device"] == "cpu"


@pytest.mark.parametrize("environment,overrides,expected", [
    ({}, {}, {"data_dir": "default", "default_voice": "default", "cleaner_model_name": "default"}),
    ({}, {"data_dir": Path("data")}, {"data_dir": "constructor"}),
    ({"AUDIOBOOK_DATA_DIR": "env-data"}, {"data_dir": Path("data")}, {"data_dir": "env"}),
    ({"AUDIOBOOK_DATA_DIR": "env-data"}, {"data_dir": Path("custom")}, {"data_dir": "constructor"}),
    ({"TTS_PROVIDER": " "}, {"tts_provider": "cpu"}, {"tts_provider": "env"}),
    ({"CLEANER_BACKEND": " "}, {"cleaner_backend": "api"}, {"cleaner_backend": "constructor"}),
    ({"AUDIOBOOK_MAX_PARALLEL_CHAPTERS": "bad"}, {}, {"max_parallel_chapters": "default"}),
    ({"AUDIOBOOK_MAX_PARALLEL_CHAPTERS": "0"}, {}, {"max_parallel_chapters": "env"}),
    ({"CLEANER_MODEL_NAME": "explicit"}, {}, {"cleaner_model_name": "env"}),
    (
        {"AUDIOBOOK_MAX_PARALLEL_CHAPTERS": "1"}, {"enable_parallel": True},
        {"enable_parallel": "env"},
    ),
])
def test_provenance_identifies_the_winning_input(environment, overrides, expected):
    result = resolve_config(environment, overrides)
    assert result.provenance.keys() == result.values.keys()
    for name, source in expected.items():
        assert result.provenance[name] == source


@pytest.mark.parametrize("groups,model_source", [(["cleaner-cuda"], "setup"), ([], "env")])
def test_setup_provenance_keeps_the_model_source_when_no_cleaner_is_selected(groups, model_source):
    result = resolve_config(
        {"CLEANER_MODEL_NAME": "environment-model"},
        setup_state={"profile": "cuda", "model_groups": groups, "cleaner_cuda_quantization": "8bit"},
        model_groups=MODEL_GROUPS,
    )
    assert result.provenance["cleaner_backend"] == "setup"
    assert result.provenance["cleaner_model_name"] == model_source
    assert result.provenance["cleaner_cuda_quantization"] == "setup"


def test_resolution_repr_hides_values_and_never_copies_environment_credentials():
    environment = {
        "OPENAI_API_KEY": "private-openai-key", "HF_TOKEN": "private-hub-token",
        "HUGGING_FACE_HUB_TOKEN": "private-legacy-token", "CUSTOM_CREDENTIAL": "private-custom-key",
        "CLEANER_API_KEY_ENV": "CUSTOM_CREDENTIAL",
        "CLEANER_API_BASE_URL": "https://user:private-password@example.org/?token=private-query-token",
    }
    result = resolve_config(environment)
    assert result.values["cleaner_api_key_env"] == "CUSTOM_CREDENTIAL"
    assert result.values["cleaner_api_base_url"] == environment["CLEANER_API_BASE_URL"]
    assert not (environment.keys() & result.values.keys())
    output = repr(result) + repr(result.provenance)
    for secret in (
        "private-openai-key", "private-hub-token", "private-legacy-token", "private-custom-key",
        "private-password", "private-query-token",
    ):
        assert secret not in output
    assert result.provenance["cleaner_api_base_url"] == "env"


@pytest.mark.parametrize("raw", ["~", "~other/books", "./~/books", "./~", "./~other/books"])
def test_host_home_expansion_is_an_explicit_input(raw):
    result = resolve_config({"AUDIOBOOK_OUTPUT_DIR": raw}, expanded_paths={raw: Path("expanded/books")})
    assert result.values["output_dir"] == Path("expanded/books")
    with pytest.raises(RuntimeError, match="Home expansion"):
        resolve_config({"AUDIOBOOK_OUTPUT_DIR": raw})

