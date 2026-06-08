"""Provider-owned device memory policy tests."""

from __future__ import annotations

import sys
import types

import pytest

from modules.pipeline.models import PipelineConfig
from modules.tts.runtime_spec import TTSRuntimeSpec


GIB = 1024**3


def test_mlx_device_memory_policy_sample_maps_mlx_fields(monkeypatch):
    from modules.tts.device_memory import MLXDeviceMemoryPolicy

    fake_mlx_core = types.SimpleNamespace(
        get_active_memory=lambda: 3 * GIB,
        get_cache_memory=lambda: 2 * GIB,
        get_peak_memory=lambda: 5 * GIB,
    )

    def virtual_memory():
        return types.SimpleNamespace(total=32 * GIB, available=20 * GIB)

    def process_factory():
        return types.SimpleNamespace(
            memory_info=lambda: types.SimpleNamespace(rss=1 * GIB)
        )

    snapshot = MLXDeviceMemoryPolicy(
        virtual_memory=virtual_memory,
        process_factory=process_factory,
        mlx_core=fake_mlx_core,
    ).sample()

    assert snapshot is not None
    assert snapshot.provider == "mlx"
    assert snapshot.device is None
    assert snapshot.total_bytes == 32 * GIB
    assert snapshot.available_bytes == 20 * GIB
    assert snapshot.process_rss_bytes == 1 * GIB
    assert snapshot.active_bytes == 3 * GIB
    assert snapshot.cache_bytes == 2 * GIB
    assert snapshot.peak_bytes == 5 * GIB


def test_mlx_device_memory_policy_missing_mlx_returns_host_snapshot(monkeypatch):
    from modules.tts.device_memory import MLXDeviceMemoryPolicy

    monkeypatch.setitem(sys.modules, "mlx", None)
    monkeypatch.setitem(sys.modules, "mlx.core", None)

    def virtual_memory():
        return types.SimpleNamespace(total=16 * GIB, available=9 * GIB)

    snapshot = MLXDeviceMemoryPolicy(virtual_memory=virtual_memory).sample()

    assert snapshot is not None
    assert snapshot.provider == "mlx"
    assert snapshot.active_bytes == 0
    assert snapshot.cache_bytes == 0
    assert snapshot.peak_bytes == 0


def test_mlx_device_memory_policy_worker_environment_uses_active_workers():
    from modules.tts.device_memory import MLXDeviceMemoryPolicy

    env = MLXDeviceMemoryPolicy().configure_worker_environment(
        available_bytes=12 * GIB,
        active_workers=3,
    )

    assert env == {"MLX_METAL_MEMORY": str(int((12 * GIB / 3) * 0.9))}


def test_host_memory_policy_has_no_provider_specific_worker_environment():
    from modules.tts.device_memory import HostMemoryPolicy

    assert HostMemoryPolicy().configure_worker_environment(
        available_bytes=12 * GIB,
        active_workers=3,
    ) == {}


def test_cuda_device_memory_policy_is_unavailable_until_cuda_sampling_exists():
    from modules.tts.device_memory import get_device_memory_policy

    policy = get_device_memory_policy(
        TTSRuntimeSpec(provider="cuda", engine="kokoro", model=None, quantization="bf16")
    )

    assert policy.provider == "cuda"
    assert policy.sampling_unavailable_reason == "cuda device memory sampling unavailable"
    assert policy.sample() is None
    assert policy.configure_worker_environment(
        available_bytes=12 * GIB,
        active_workers=3,
    ) == {}


def test_device_memory_policy_factory_and_estimate_follow_runtime_provider():
    from modules.tts.device_memory import HostMemoryPolicy, MLXDeviceMemoryPolicy, get_device_memory_policy

    mlx_policy = get_device_memory_policy(
        TTSRuntimeSpec.from_legacy("kokoro", "bf16", provider="mlx")
    )
    host_policy = get_device_memory_policy(
        TTSRuntimeSpec(provider="local", engine="api", model=None, quantization="full")
    )

    assert isinstance(mlx_policy, MLXDeviceMemoryPolicy)
    assert mlx_policy.estimated_worker_bytes(
        TTSRuntimeSpec.from_legacy("kokoro", "bf16", provider="mlx")
    ) == 4 * GIB
    assert isinstance(host_policy, HostMemoryPolicy)
    assert host_policy.estimated_worker_bytes(
        TTSRuntimeSpec(provider="local", engine="api", model=None, quantization="full")
    ) is None


def test_device_memory_policy_deserialization_rejects_unknown_provider():
    from modules.tts.device_memory import device_memory_policy_from_serialized

    with pytest.raises(ValueError, match="Unsupported device memory policy provider"):
        device_memory_policy_from_serialized({"provider": "cudaa", "device": ""})


def test_pipeline_config_rebuilds_policy_when_serialized_provider_disagrees():
    config = PipelineConfig(
        tts_runtime=TTSRuntimeSpec.from_legacy(
            "kokoro",
            "bf16",
            provider="cuda",
            device="cuda:0",
        ),
        device_memory_policy={"provider": "host"},
    )

    assert config.device_memory_policy is not None
    assert config.device_memory_policy.provider == "cuda"
    assert config.device_memory_policy.device == "cuda:0"
