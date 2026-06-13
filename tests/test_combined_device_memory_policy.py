"""Combined TTS + cleaner CUDA device memory policy tests."""

from __future__ import annotations

import sys
import types

from modules.tts.cleaner_config import CleanerConfig
from modules.tts.runtime_spec import TTSRuntimeSpec

GIB = 1024**3


def _fake_cuda_torch(
    *,
    free_bytes_per_call: list[tuple[int, int]],
) -> types.SimpleNamespace:
    call_index = {"value": 0}

    def mem_get_info(device: int) -> tuple[int, int]:
        if device != 0:
            raise RuntimeError(f"unexpected device {device}")
        index = call_index["value"]
        call_index["value"] += 1
        return free_bytes_per_call[min(index, len(free_bytes_per_call) - 1)]

    fake_cuda = types.SimpleNamespace(
        device_count=lambda: 1,
        mem_get_info=mem_get_info,
        memory_stats=lambda device: {
            "active_bytes.all.current": 1 * GIB,
            "active_bytes.all.peak": 2 * GIB,
            "reserved_bytes.all.current": 512 * 1024**2,
        },
        device=lambda index: types.SimpleNamespace(
            __enter__=lambda self: self,
            __exit__=lambda self, *_args: False,
        ),
        empty_cache=lambda: None,
    )
    return types.SimpleNamespace(cuda=fake_cuda)


def test_combined_device_memory_policy_sample_uses_minimum_available_bytes():
    from modules.tts.device_memory import CombinedDeviceMemoryPolicy

    fake_torch = _fake_cuda_torch(
        free_bytes_per_call=[
            (12 * GIB, 24 * GIB),
            (8 * GIB, 24 * GIB),
        ],
    )
    policy = CombinedDeviceMemoryPolicy(
        tts_runtime_spec=TTSRuntimeSpec(
            provider="cuda",
            engine="kokoro",
            model=None,
            quantization="bf16",
            device="cuda:0",
        ),
        cleaner_config=CleanerConfig(
            backend="cuda",
            model_name="meta-llama/Llama-3.2-3B-Instruct",
            cuda_device="cuda",
            cuda_quantization="4bit",
        ),
        torch_module=fake_torch,
    )

    snapshot = policy.sample()

    assert snapshot is not None
    assert snapshot.provider == "combined"
    assert snapshot.device == "cuda:0"
    assert snapshot.available_bytes == 8 * GIB
    assert snapshot.total_bytes == 24 * GIB


def test_combined_device_memory_policy_estimated_worker_bytes_sums_constituents():
    from modules.tts.device_memory import CombinedDeviceMemoryPolicy

    runtime = TTSRuntimeSpec(
        provider="cuda",
        engine="kokoro",
        model=None,
        quantization="bf16",
        device="cuda:0",
    )
    policy = CombinedDeviceMemoryPolicy(
        tts_runtime_spec=runtime,
        cleaner_config=CleanerConfig(
            backend="cuda",
            model_name="meta-llama/Llama-3.2-3B-Instruct",
            cuda_device="cuda:0",
            cuda_quantization="4bit",
        ),
    )

    assert policy.estimated_worker_bytes(runtime) == (4 + 3) * GIB


def test_combined_device_memory_policy_clear_cache_clears_both_constituents():
    from modules.tts.device_memory import CombinedDeviceMemoryPolicy

    cleared_devices: list[int] = []

    class _DeviceContext:
        def __init__(self, index: int) -> None:
            self.index = index

        def __enter__(self) -> _DeviceContext:
            cleared_devices.append(self.index)
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

    fake_cuda = types.SimpleNamespace(
        device_count=lambda: 1,
        mem_get_info=lambda device: (8 * GIB, 16 * GIB),
        memory_stats=lambda device: {},
        device=lambda index: _DeviceContext(index),
        empty_cache=lambda: cleared_devices.append(-1),
    )
    fake_torch = types.SimpleNamespace(cuda=fake_cuda)

    CombinedDeviceMemoryPolicy(
        tts_runtime_spec=TTSRuntimeSpec(
            provider="cuda",
            engine="kokoro",
            model=None,
            quantization="bf16",
            device="cuda:0",
        ),
        cleaner_config=CleanerConfig(
            backend="cuda",
            model_name="meta-llama/Llama-3.2-1B-Instruct",
            cuda_device="cuda",
            cuda_quantization="8bit",
        ),
        torch_module=fake_torch,
    ).clear_cache()

    assert cleared_devices == [0, -1, 0, -1]


def test_get_combined_device_memory_policy_returns_combined_for_same_cuda_device():
    from modules.tts.device_memory import (
        CombinedDeviceMemoryPolicy,
        get_combined_device_memory_policy,
    )

    runtime = TTSRuntimeSpec(
        provider="cuda",
        engine="kokoro",
        model=None,
        quantization="bf16",
        device="cuda:0",
    )
    cleaner_config = CleanerConfig(
        backend="cuda",
        model_name="meta-llama/Llama-3.2-3B-Instruct",
        cuda_device="cuda",
        cuda_quantization="4bit",
    )

    policy = get_combined_device_memory_policy(runtime, cleaner_config)

    assert isinstance(policy, CombinedDeviceMemoryPolicy)
    assert policy.device == "cuda:0"


def test_get_combined_device_memory_policy_falls_back_when_different_cuda_devices():
    from modules.tts.device_memory import (
        CUDADeviceMemoryPolicy,
        get_combined_device_memory_policy,
    )

    runtime = TTSRuntimeSpec(
        provider="cuda",
        engine="kokoro",
        model=None,
        quantization="bf16",
        device="cuda:0",
    )
    cleaner_config = CleanerConfig(
        backend="cuda",
        model_name="meta-llama/Llama-3.2-3B-Instruct",
        cuda_device="cuda:1",
        cuda_quantization="4bit",
    )

    policy = get_combined_device_memory_policy(runtime, cleaner_config)

    assert isinstance(policy, CUDADeviceMemoryPolicy)
    assert policy.device == "cuda:0"


def test_get_combined_device_memory_policy_falls_back_for_non_cuda_cleaner():
    from modules.tts.device_memory import (
        CUDADeviceMemoryPolicy,
        get_combined_device_memory_policy,
    )

    runtime = TTSRuntimeSpec(
        provider="cuda",
        engine="kokoro",
        model=None,
        quantization="bf16",
        device="cuda:0",
    )
    cleaner_config = CleanerConfig(backend="api")

    policy = get_combined_device_memory_policy(runtime, cleaner_config)

    assert isinstance(policy, CUDADeviceMemoryPolicy)
    assert policy.provider == "cuda"


def test_get_combined_device_memory_policy_falls_back_for_non_cuda_tts():
    from modules.tts.device_memory import (
        HostMemoryPolicy,
        get_combined_device_memory_policy,
    )

    runtime = TTSRuntimeSpec.from_legacy("kokoro", "bf16", provider="mlx")
    cleaner_config = CleanerConfig(
        backend="cuda",
        model_name="meta-llama/Llama-3.2-3B-Instruct",
        cuda_device="cuda:0",
        cuda_quantization="4bit",
    )

    policy = get_combined_device_memory_policy(runtime, cleaner_config)

    assert isinstance(policy, HostMemoryPolicy)


def test_combined_device_memory_policy_sampling_unavailable_without_torch(monkeypatch):
    from modules.tts.device_memory import CombinedDeviceMemoryPolicy

    monkeypatch.setitem(sys.modules, "torch", None)

    policy = CombinedDeviceMemoryPolicy(
        tts_runtime_spec=TTSRuntimeSpec(
            provider="cuda",
            engine="kokoro",
            model=None,
            quantization="bf16",
            device="cuda:0",
        ),
        cleaner_config=CleanerConfig(
            backend="cuda",
            model_name="meta-llama/Llama-3.2-3B-Instruct",
            cuda_device="cuda:0",
            cuda_quantization="4bit",
        ),
        torch_module=None,
    )

    assert policy.sampling_unavailable_reason == "cuda device memory sampling unavailable"
    assert policy.sample() is None


def test_no_cleaner_device_memory_policy_class_exists():
    import modules.tts.device_memory as device_memory

    assert not hasattr(device_memory, "CleanerDeviceMemoryPolicy")
