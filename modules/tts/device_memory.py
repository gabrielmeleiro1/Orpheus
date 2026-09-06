"""Provider-owned device memory policies."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from modules.tts.runtime_spec import TTSRuntimeSpec


@dataclass(frozen=True)
class DeviceMemorySnapshot:
    provider: str
    device: str | None
    total_bytes: int
    available_bytes: int
    process_rss_bytes: int = 0
    active_bytes: int = 0
    cache_bytes: int = 0
    peak_bytes: int = 0

class DeviceMemoryPolicy(Protocol):
    provider: str
    device: str | None
    sampling_unavailable_reason: str | None

    def sample(self) -> DeviceMemorySnapshot | None: ...

    def configure_worker_environment(
        self,
        *,
        available_bytes: int,
        active_workers: int,
    ) -> dict[str, str]: ...

    def clear_cache(self) -> None: ...

    def estimated_worker_bytes(self, runtime_spec: TTSRuntimeSpec) -> int | None: ...

    def to_dict(self) -> dict[str, str | None]: ...


def _sysconf_memory_snapshot() -> tuple[int, int] | None:
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        total_pages = int(os.sysconf("SC_PHYS_PAGES"))
        available_pages = int(os.sysconf("SC_AVPHYS_PAGES"))
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    if page_size <= 0 or total_pages <= 0 or available_pages < 0:
        return None
    return total_pages * page_size, available_pages * page_size


def _host_memory_bytes(
    *,
    virtual_memory: Callable[[], Any] | None,
) -> tuple[int, int] | None:
    if virtual_memory is None:
        try:
            import psutil

            virtual_memory = psutil.virtual_memory
        except Exception:
            virtual_memory = None

    if virtual_memory is not None:
        try:
            memory = virtual_memory()
            return int(memory.total), int(memory.available)
        except Exception:
            return None

    return _sysconf_memory_snapshot()


def _process_rss_bytes(process_factory: Callable[[], Any] | None) -> int:
    if process_factory is None:
        try:
            import psutil

            process_factory = psutil.Process
        except Exception:
            return 0
    try:
        process = process_factory()
        if process is None:
            return 0
        return max(0, int(process.memory_info().rss))
    except Exception:
        return 0


def _read_byte_count(source: Any, *names: str) -> int:
    for name in names:
        probe = getattr(source, name, None)
        if not callable(probe):
            continue
        try:
            return max(0, int(probe()))
        except Exception:
            return 0
    return 0


class HostMemoryPolicy:
    provider = "host"
    sampling_unavailable_reason: str | None = None

    def __init__(
        self,
        *,
        device: str | None = None,
        virtual_memory: Callable[[], Any] | None = None,
        process_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.device = device
        self._virtual_memory = virtual_memory
        self._process_factory = process_factory

    def sample(self) -> DeviceMemorySnapshot | None:
        host_memory = _host_memory_bytes(virtual_memory=self._virtual_memory)
        if host_memory is None:
            return None
        total_bytes, available_bytes = host_memory
        return DeviceMemorySnapshot(
            provider=self.provider,
            device=self.device,
            total_bytes=max(0, int(total_bytes)),
            available_bytes=max(0, int(available_bytes)),
            process_rss_bytes=_process_rss_bytes(self._process_factory),
        )

    def configure_worker_environment(
        self,
        *,
        available_bytes: int,
        active_workers: int,
    ) -> dict[str, str]:
        _ = available_bytes, active_workers
        return {}

    def clear_cache(self) -> None:
        return

    def estimated_worker_bytes(self, runtime_spec: TTSRuntimeSpec) -> int | None:
        _ = runtime_spec
        return None

    def to_dict(self) -> dict[str, str | None]:
        return {"provider": self.provider, "device": self.device}


class MLXDeviceMemoryPolicy(HostMemoryPolicy):
    provider = "mlx"

    def __init__(
        self,
        *,
        device: str | None = None,
        virtual_memory: Callable[[], Any] | None = None,
        process_factory: Callable[[], Any] | None = None,
        mlx_core: Any = None,
    ) -> None:
        super().__init__(
            device=device,
            virtual_memory=virtual_memory,
            process_factory=process_factory,
        )
        self._mlx_core = mlx_core

    def _resolve_mlx_core(self) -> Any:
        if self._mlx_core is not None:
            return self._mlx_core
        try:
            import mlx.core as mx

            return mx
        except Exception:
            return None

    def sample(self) -> DeviceMemorySnapshot | None:
        host_snapshot = super().sample()
        if host_snapshot is None:
            return None
        mlx_core = self._resolve_mlx_core()
        return DeviceMemorySnapshot(
            provider=self.provider,
            device=self.device,
            total_bytes=host_snapshot.total_bytes,
            available_bytes=host_snapshot.available_bytes,
            process_rss_bytes=host_snapshot.process_rss_bytes,
            active_bytes=_read_byte_count(
                mlx_core,
                "get_active_memory",
                "active_memory",
            ),
            cache_bytes=_read_byte_count(
                mlx_core,
                "get_cache_memory",
                "cache_memory",
            ),
            peak_bytes=_read_byte_count(
                mlx_core,
                "get_peak_memory",
                "peak_memory",
            ),
        )

    def configure_worker_environment(
        self,
        *,
        available_bytes: int,
        active_workers: int,
    ) -> dict[str, str]:
        safe_worker_count = max(1, int(active_workers))
        worker_bytes = max(0, int(available_bytes)) / safe_worker_count
        return {"MLX_METAL_MEMORY": str(int(worker_bytes * 0.9))}

    def clear_cache(self) -> None:
        mlx_core = self._resolve_mlx_core()
        clear_cache = getattr(mlx_core, "clear_cache", None)
        if callable(clear_cache):
            clear_cache()

    def estimated_worker_bytes(self, runtime_spec: TTSRuntimeSpec) -> int | None:
        if runtime_spec.engine == "kokoro":
            return 4 * 1024**3
        return None


def _cuda_memory_stat(stats: Mapping[str, object], *keys: str) -> int:
    for key in keys:
        try:
            value = stats.get(key)
            if value is not None:
                return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0


def _cuda_device_index(device: str | None) -> int | None:
    if device is None or device == "cuda":
        return 0
    if device.startswith("cuda:"):
        try:
            return int(device.split(":", 1)[1])
        except ValueError:
            return None
    return None


def _same_cuda_device_index(left: str | None, right: str | None) -> bool:
    left_index = _cuda_device_index(left)
    right_index = _cuda_device_index(right)
    if left_index is None or right_index is None:
        return False
    return left_index == right_index


class CUDADeviceMemoryPolicy:
    provider = "cuda"

    def __init__(
        self,
        *,
        device: str | None = None,
        torch_module: Any = None,
    ) -> None:
        self.device = device
        self._torch_module = torch_module

    def _resolve_torch(self) -> Any:
        if self._torch_module is not None:
            return self._torch_module
        try:
            import torch

            return torch
        except Exception:
            return None

    @property
    def sampling_unavailable_reason(self) -> str | None:
        torch_module = self._resolve_torch()
        if torch_module is None:
            return "cuda device memory sampling unavailable"
        cuda = getattr(torch_module, "cuda", None)
        if cuda is None or not callable(getattr(cuda, "mem_get_info", None)):
            return "cuda device memory sampling unavailable"
        return None

    def sample(self) -> DeviceMemorySnapshot | None:
        torch_module = self._resolve_torch()
        if torch_module is None:
            return None
        cuda = getattr(torch_module, "cuda", None)
        if cuda is None:
            return None
        mem_get_info = getattr(cuda, "mem_get_info", None)
        if not callable(mem_get_info):
            return None

        device_index = _cuda_device_index(self.device)
        if device_index is None:
            return None

        try:
            device_count = getattr(cuda, "device_count", None)
            if callable(device_count):
                if device_index < 0 or device_index >= int(device_count()):
                    return None

            free_bytes, total_bytes = mem_get_info(device_index)

            active_bytes = 0
            cache_bytes = 0
            peak_bytes = 0
            memory_stats = getattr(cuda, "memory_stats", None)
            if callable(memory_stats):
                try:
                    stats = memory_stats(device_index)
                    active_bytes = _cuda_memory_stat(
                        stats,
                        "active_bytes.all.current",
                    )
                    peak_bytes = _cuda_memory_stat(
                        stats,
                        "active_bytes.all.peak",
                    )
                    cache_bytes = _cuda_memory_stat(
                        stats,
                        "reserved_bytes.all.current",
                    )
                except Exception:
                    pass

            return DeviceMemorySnapshot(
                provider=self.provider,
                device=self.device,
                total_bytes=max(0, int(total_bytes)),
                available_bytes=max(0, int(free_bytes)),
                active_bytes=active_bytes,
                cache_bytes=cache_bytes,
                peak_bytes=peak_bytes,
            )
        except Exception:
            return None

    def configure_worker_environment(
        self,
        *,
        available_bytes: int,
        active_workers: int,
    ) -> dict[str, str]:
        """Return CUDA worker env vars when needed.

        PyTorch manages CUDA memory per process, so no per-worker env vars
        are required today. Worker sizing uses sampled VRAM instead.
        """
        _ = available_bytes, active_workers
        return {}

    def clear_cache(self) -> None:
        torch_module = self._resolve_torch()
        if torch_module is None:
            return
        cuda = getattr(torch_module, "cuda", None)
        if cuda is None:
            return
        empty_cache = getattr(cuda, "empty_cache", None)
        if not callable(empty_cache):
            return
        device_index = _cuda_device_index(self.device)
        if device_index is None:
            return
        try:
            device_ctx = getattr(cuda, "device", None)
            if callable(device_ctx):
                with device_ctx(device_index):
                    empty_cache()
            else:
                empty_cache()
        except Exception:
            return

    def estimated_worker_bytes(self, runtime_spec: TTSRuntimeSpec) -> int | None:
        if runtime_spec.engine == "kokoro":
            return 4 * 1024**3
        return None

    def to_dict(self) -> dict[str, str | None]:
        return {"provider": self.provider, "device": self.device}


class CombinedDeviceMemoryPolicy:
    provider = "combined"

    def __init__(
        self,
        *,
        tts_runtime_spec: TTSRuntimeSpec,
        cleaner_config: Any,
        torch_module: Any = None,
    ) -> None:
        from modules.tts.cleaner_config import CleanerConfig

        if not isinstance(cleaner_config, CleanerConfig):
            raise TypeError("cleaner_config must be a CleanerConfig instance")

        self.tts_runtime_spec = tts_runtime_spec
        self.cleaner_config = cleaner_config
        self.device = tts_runtime_spec.device
        self._torch_module = torch_module
        self._tts_policy = CUDADeviceMemoryPolicy(
            device=tts_runtime_spec.device,
            torch_module=torch_module,
        )
        self._cleaner_policy = CUDADeviceMemoryPolicy(
            device=cleaner_config.cuda_device,
            torch_module=torch_module,
        )

    @property
    def sampling_unavailable_reason(self) -> str | None:
        for policy in (self._tts_policy, self._cleaner_policy):
            reason = policy.sampling_unavailable_reason
            if reason is not None:
                return reason
        return None

    def sample(self) -> DeviceMemorySnapshot | None:
        snapshots = [
            self._tts_policy.sample(),
            self._cleaner_policy.sample(),
        ]
        valid_snapshots = [snapshot for snapshot in snapshots if snapshot is not None]
        if not valid_snapshots:
            return None

        limiting_snapshot = min(
            valid_snapshots,
            key=lambda snapshot: snapshot.available_bytes,
        )
        return DeviceMemorySnapshot(
            provider=self.provider,
            device=self.device,
            total_bytes=max(snapshot.total_bytes for snapshot in valid_snapshots),
            available_bytes=min(snapshot.available_bytes for snapshot in valid_snapshots),
            process_rss_bytes=max(
                snapshot.process_rss_bytes for snapshot in valid_snapshots
            ),
            active_bytes=limiting_snapshot.active_bytes,
            cache_bytes=limiting_snapshot.cache_bytes,
            peak_bytes=limiting_snapshot.peak_bytes,
        )

    def configure_worker_environment(
        self,
        *,
        available_bytes: int,
        active_workers: int,
    ) -> dict[str, str]:
        merged: dict[str, str] = {}
        for policy in (self._tts_policy, self._cleaner_policy):
            merged.update(
                policy.configure_worker_environment(
                    available_bytes=available_bytes,
                    active_workers=active_workers,
                )
            )
        return merged

    def clear_cache(self) -> None:
        self._tts_policy.clear_cache()
        self._cleaner_policy.clear_cache()

    def estimated_worker_bytes(self, runtime_spec: TTSRuntimeSpec) -> int | None:
        from modules.tts.cleaner_memory import estimate_cleaner_cuda_bytes

        total_estimate = 0
        tts_estimate = self._tts_policy.estimated_worker_bytes(runtime_spec)
        if tts_estimate is not None:
            total_estimate += tts_estimate
        if self.cleaner_config.backend.strip().lower() == "cuda":
            total_estimate += estimate_cleaner_cuda_bytes(
                self.cleaner_config.model_name,
                self.cleaner_config.cuda_quantization,
            )
        return total_estimate if total_estimate > 0 else None

    def to_dict(self) -> dict[str, str | None]:
        return {"provider": self.provider, "device": self.device}


def get_device_memory_policy(runtime_spec: TTSRuntimeSpec) -> DeviceMemoryPolicy:
    provider = runtime_spec.provider.strip().lower()
    if provider == "mlx":
        return MLXDeviceMemoryPolicy(device=runtime_spec.device)
    if provider == "cuda":
        return CUDADeviceMemoryPolicy(device=runtime_spec.device)
    return HostMemoryPolicy(device=runtime_spec.device)


def get_combined_device_memory_policy(
    tts_spec: TTSRuntimeSpec,
    cleaner_config: Any,
) -> DeviceMemoryPolicy:
    """Return a combined CUDA policy when TTS and cleaner share a device."""
    from modules.tts.cleaner_config import CleanerConfig

    if not isinstance(cleaner_config, CleanerConfig):
        raise TypeError("cleaner_config must be a CleanerConfig instance")

    tts_provider = tts_spec.provider.strip().lower()
    cleaner_backend = cleaner_config.backend.strip().lower()
    if (
        tts_provider == "cuda"
        and cleaner_backend == "cuda"
        and _same_cuda_device_index(tts_spec.device, cleaner_config.cuda_device)
    ):
        return CombinedDeviceMemoryPolicy(
            tts_runtime_spec=tts_spec,
            cleaner_config=cleaner_config,
        )
    return get_device_memory_policy(tts_spec)


def device_memory_policy_from_serialized(
    value: Mapping[str, object] | DeviceMemoryPolicy | None,
) -> DeviceMemoryPolicy | None:
    if value is None:
        return None
    if hasattr(value, "sample") and hasattr(value, "configure_worker_environment"):
        return value  # type: ignore[return-value]
    provider = str(value.get("provider") or "host").strip().lower()
    device_value = value.get("device")
    device = str(device_value).strip() if device_value is not None else None
    if device == "":
        device = None
    if provider == "mlx":
        return MLXDeviceMemoryPolicy(device=device)
    if provider == "cuda":
        return CUDADeviceMemoryPolicy(device=device)
    if provider == "host":
        return HostMemoryPolicy(device=device)
    if provider == "combined":
        return None
    raise ValueError(f"Unsupported device memory policy provider '{provider}'.")
