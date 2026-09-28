"""Regressions for setup, cache integrity, and automatic TTS selection."""

import builtins
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from modules import hf_cache
from modules.tts.model_snapshots import installed_model_path
from modules.tts.runtime_options import TTSRuntimeOptions
from modules.tts.runtime_spec import TTSRuntimeSpec
from scripts import download_models, setup_runtime


def test_setup_upgrades_hub_with_symlink_flag_support(monkeypatch):
    from packaging.requirements import Requirement

    requirements = (setup_runtime.REPO_ROOT / "requirements/base.txt").read_text()
    hub = next(Requirement(line) for line in requirements.splitlines()
               if line.startswith("huggingface-hub"))
    assert "1.8.0" not in hub.specifier
    assert "1.9.0" in hub.specifier
    monkeypatch.setattr(setup_runtime.RuntimeProfile, "is_compatible", lambda self: True)
    for profile in ("base", "mlx", "cuda"):
        assert "--upgrade" in setup_runtime.build_install_plan(profile).pip_command


@pytest.mark.parametrize("directory", ["scripts", "unrelated"])
def test_setup_verification_outside_repository(monkeypatch, tmp_path, directory):
    monkeypatch.chdir(setup_runtime.REPO_ROOT / "scripts" if directory == "scripts" else tmp_path)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setattr(setup_runtime.RuntimeProfile, "is_compatible", lambda self: True)
    probe = setup_runtime.RuntimeCapabilityService._TTS_PACKAGE_PROBE
    stubs = (
        "import sys\nfrom types import SimpleNamespace\n"
        "sys.modules['torch'] = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False))\n"
        "sys.modules['kokoro'] = SimpleNamespace(KModel=object, KPipeline=object)\n"
    )
    monkeypatch.setattr(setup_runtime.RuntimeCapabilityService, "_TTS_PACKAGE_PROBE", stubs + probe)
    state_path = tmp_path / "setup.json"
    setup_runtime.run_setup(
        profile_id="cuda", model_groups=["tts-cpu-cuda"], state_path=state_path,
        command_runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 0),
    )
    assert setup_runtime.read_setup_state(state_path)["profile"] == "cuda"


def test_incomplete_pinned_snapshot_reports_repair(monkeypatch, tmp_path):
    import huggingface_hub

    (tmp_path / "config.json").write_text("{}")
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda **kwargs: str(tmp_path))
    with pytest.raises(RuntimeError, match="scripts/download_models.py.*hexgrad/Kokoro-82M"):
        installed_model_path("hexgrad/Kokoro-82M")


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


@pytest.mark.parametrize("failure", [None, "allocation", "execution", "synchronization"])
def test_tts_cuda_executes_probe_without_cleaner_driver_gate(monkeypatch, failure):
    monkeypatch.setattr("modules.runtime_profiles.cuda_hardware_error", lambda: "CUDA 12.4 driver")
    tensor = Mock()
    tensor.add_.return_value.item.return_value = 2
    torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True), ones=Mock(return_value=tensor))
    if failure == "allocation":
        torch.ones.side_effect = RuntimeError("allocation failed")
    elif failure == "execution":
        tensor.add_.side_effect = RuntimeError("kernel failed")
    elif failure == "synchronization":
        tensor.add_.return_value.item.side_effect = RuntimeError("async CUDA failure")
    monkeypatch.setitem(sys.modules, "torch", torch)
    assert TTSRuntimeSpec.cuda_available_on_current_platform() is (failure is None)
    torch.ones.assert_called_once_with(1, device="cuda")


def test_cuda_payload_accepts_auto_device():
    options = TTSRuntimeOptions.from_payload({"provider": "cuda", "device": "auto"})
    assert options.provider == "cuda"
    assert options.device is None


def test_direct_package_load_configures_hub_before_kokoro(monkeypatch):
    from modules.tts.runtimes.kokoro_base import KokoroRuntimeConfig
    from modules.tts.runtimes.kokoro_package import PackageKokoroRuntime

    configured = Mock()
    monkeypatch.setattr(hf_cache, "configure_hf_cache", configured)
    monkeypatch.setattr("modules.tts.model_cache.get_cached_model", lambda key: None)
    monkeypatch.setattr("modules.tts.model_cache.set_cached_model", lambda *args: None)
    monkeypatch.setattr(PackageKokoroRuntime, "_local_model_dir", lambda self: None)
    original_import = builtins.__import__

    def checked_import(name, *args, **kwargs):
        if name == "kokoro":
            configured.assert_called_once()
            return SimpleNamespace(KPipeline=lambda **kwargs: object())
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", checked_import)
    spec = TTSRuntimeSpec.from_legacy("kokoro", "bf16", provider="cpu")
    PackageKokoroRuntime(KokoroRuntimeConfig(
        runtime_spec=spec, model_name=spec.resolved_model(), quantization="bf16", sample_rate=24000,
    )).load()


def test_configure_updates_already_imported_hub(monkeypatch, tmp_path):
    from huggingface_hub import constants, file_download

    monkeypatch.setattr(hf_cache, "_configured", False)
    monkeypatch.setattr(hf_cache.sys, "platform", "win32")
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    monkeypatch.delenv("HF_HUB_DISABLE_SYMLINKS", raising=False)
    monkeypatch.delenv("HF_HUB_DISABLE_SYMLINKS_WARNING", raising=False)
    monkeypatch.setattr(constants, "HF_HUB_DISABLE_SYMLINKS", False)
    monkeypatch.setattr(constants, "HF_HUB_DISABLE_SYMLINKS_WARNING", False)
    monkeypatch.setattr(hf_cache.os, "symlink", Mock(side_effect=OSError(1314, "denied")))
    hf_cache.configure_hf_cache()
    assert constants.HF_HUB_DISABLE_SYMLINKS is True
    assert file_download.are_symlinks_supported(tmp_path) is False


@pytest.mark.parametrize("repair", ["success", "failure", "unchanged"])
@pytest.mark.parametrize("destination", ["cache_dir", "local_dir"])
def test_corrupt_download_retry_preserves_trusted_receipt(monkeypatch, tmp_path, repair, destination):
    import huggingface_hub

    model = "hexgrad/Kokoro-82M"
    revision = download_models.MODEL_REVISIONS[model]
    weights = tmp_path / "weights.bin"
    weights.write_bytes(b"healthy")
    receipt = download_models.write_download_receipt(tmp_path, model_id=model, revision=revision)
    trusted = receipt.read_bytes()
    weights.write_bytes(b"damaged")
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda **kwargs: str(tmp_path))
    calls = []

    def download(**kwargs):
        calls.append(kwargs)
        assert receipt.read_bytes() == trusted
        if repair == "failure":
            raise OSError("offline")
        if repair == "success":
            weights.write_bytes(b"healthy")
        return str(weights)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", download)
    if repair == "success":
        download_models.download_model_snapshot(model, **{destination: tmp_path})
        assert weights.read_bytes() == b"healthy"
        assert download_models.validate_download_receipt(tmp_path, model_id=model, revision=revision)
    else:
        with pytest.raises((OSError, RuntimeError)):
            download_models.download_model_snapshot(model, **{destination: tmp_path})
        assert receipt.read_bytes() == trusted
        assert not download_models.validate_download_receipt(tmp_path, model_id=model, revision=revision)
    assert len(calls) == 1
    assert calls[0]["filename"] == "weights.bin"
    assert calls[0]["force_download"] is True
    assert calls[0]["revision"] == revision
    assert calls[0][destination] == str(tmp_path)


def test_healthy_download_retry_keeps_receipt(monkeypatch, tmp_path):
    import huggingface_hub

    model = "hexgrad/Kokoro-82M"
    revision = download_models.MODEL_REVISIONS[model]
    (tmp_path / "weights.bin").write_bytes(b"healthy")
    download_models.write_download_receipt(tmp_path, model_id=model, revision=revision)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda **kwargs: str(tmp_path))
    transfer = Mock(side_effect=AssertionError("Healthy cache must not be downloaded again"))
    writer = Mock(side_effect=AssertionError("Trusted receipt must not be rewritten"))
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", transfer)
    monkeypatch.setattr(download_models, "write_download_receipt", writer)
    assert download_models.download_model_snapshot(model) == tmp_path
    transfer.assert_not_called()
    writer.assert_not_called()
