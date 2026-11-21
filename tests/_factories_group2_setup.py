"""Shared fixtures for setup regressions."""

from dataclasses import replace
from functools import partial
from importlib.metadata import version
from types import SimpleNamespace

import pytest

from modules.runtime_setup import hub_cache
from scripts import setup_runtime


@pytest.fixture
def setup_context(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUDIOBOOK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hf" / "hub"))
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "-1")
    from huggingface_hub import scan_cache_dir
    from modules.web.routes import setup as route

    monkeypatch.setattr(route, "_detect_installed_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(hub_cache, "_check_model_group_assets", partial(
        hub_cache._check_model_group_assets, cache_scanner=lambda: scan_cache_dir(tmp_path / "hf" / "hub"),
    ))
    manifest = tmp_path / "requirements.txt"
    manifest.write_text(f"pytest=={version('pytest')}\n", encoding="utf-8")
    get_profile = route.get_runtime_profile
    monkeypatch.setattr(route, "get_runtime_profile", lambda name: replace(
        get_profile(name), requirements_file=manifest,
    ))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(backend_instance_id="test-backend")))
    return SimpleNamespace(route=route, request=request, manifest=manifest)


class _RuntimeProbe:
    def __init__(self, *, device="cpu", available=True, timed_out=False):
        self.device = device
        self.available = available
        self.timed_out = timed_out
        self.calls = []

    def probe_package_tts_runtime(self):
        self.calls.append("tts-cpu-cuda")
        return {"available": self.available, "device": self.device, "timed_out": self.timed_out}

    def probe_mlx_tts_runtime(self):
        self.calls.append("tts-mlx")
        return {"available": self.available, "timed_out": self.timed_out}

    def probe_cleaner_backend(self, backend, *, cleaner_cuda_quantization=None):
        self.calls.append((backend, cleaner_cuda_quantization))
        return {"available": self.available, "timed_out": self.timed_out}


def _save(profile="base", groups=(), *, restart=False):
    return setup_runtime.write_setup_state(
        setup_runtime.default_state_path(), profile_id=profile, model_groups=groups,
        restart_required=restart, cleaner_cuda_quantization="8bit",
    )


def _status(context, probe):
    return context.route._build_uncached_setup_status_response(context.request, probe, None)
