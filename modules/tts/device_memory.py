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


class CUDADeviceMemoryPolicy:
    provider = "cuda"
    sampling_unavailable_reason = "cuda device memory sampling unavailable"

    def __init__(self, *, device: str | None = None) -> None:
        self.device = device

    def sample(self) -> DeviceMemorySnapshot | None:
        return None

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


def get_device_memory_policy(runtime_spec: TTSRuntimeSpec) -> DeviceMemoryPolicy:
    provider = runtime_spec.provider.strip().lower()
    if provider == "mlx":
        return MLXDeviceMemoryPolicy(device=runtime_spec.device)
    if provider == "cuda":
        return CUDADeviceMemoryPolicy(device=runtime_spec.device)
    return HostMemoryPolicy(device=runtime_spec.device)


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
    raise ValueError(f"Unsupported device memory policy provider '{provider}'.")
