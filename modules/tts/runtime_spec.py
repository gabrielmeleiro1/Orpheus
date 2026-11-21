"""Canonical TTS runtime identity."""

from __future__ import annotations

import importlib.util
import json
import logging
import multiprocessing
import os
import platform
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Mapping

from modules.subprocess_utils import background_process_kwargs

LOGGER = logging.getLogger(__name__)

# (CUDA_VISIBLE_DEVICES, id(torch) or None for a child-process probe) ->
# (torch module or None, probe result). The module is kept so its id cannot be
# reused by a different object.
_CUDA_PROBE_CACHE: dict[tuple[str | None, int | None], tuple[object, bool]] = {}
_CUDA_PROBE_LOCK = threading.Lock()
# Cold torch import plus CUDA initialisation can take tens of seconds on Windows.
_CUDA_PROBE_TIMEOUT_SECONDS = 120.0
# Mirrors _launch_cuda_probe_kernel for a child interpreter; prints one JSON line.
_CUDA_PROBE_SCRIPT = """
import json
try:
    import torch
    if not torch.cuda.is_available():
        available = False
    else:
        available = torch.ones(1, device="cuda").add_(1).item() == 2
except Exception:
    available = False
print(json.dumps({"available": available is True}))
"""


def _launch_cuda_probe_kernel(torch: object) -> bool:
    try:
        if not torch.cuda.is_available():  # type: ignore[attr-defined]
            return False
        # Execute and synchronize a tiny kernel with the installed runtime.
        return torch.ones(1, device="cuda").add_(1).item() == 2  # type: ignore[attr-defined]
    except Exception:
        return False


def _cuda_probe_runs_in_process() -> bool:
    """True when launching the probe kernel in this process costs nothing extra.

    The first kernel launch creates a CUDA context of several hundred MB that
    lives as long as the process. That is free when CUDA is already initialized
    here, and harmless in spawned workers and one-shot ``python -c`` probes,
    which exist to use (or test) CUDA and exit. The long-lived backend asks
    a short-lived child instead.
    """
    cuda = getattr(sys.modules.get("torch"), "cuda", None)
    is_initialized = getattr(cuda, "is_initialized", None)
    if callable(is_initialized):
        try:
            if is_initialized():
                return True
        except Exception:
            pass
    if multiprocessing.parent_process() is not None:
        return True
    return list(getattr(sys, "argv", [])[:1]) == ["-c"]


def _torch_installed() -> bool:
    if "torch" in sys.modules:
        return True
    try:
        return importlib.util.find_spec("torch") is not None
    except (ImportError, ValueError):
        return False


def _cuda_probe_child(connection: object) -> None:
    """Spawned-process entry point: answer from this (disposable) process."""
    try:
        try:
            import torch
        except Exception:
            available = False
        else:
            available = _launch_cuda_probe_kernel(torch)
        connection.send(available is True)  # type: ignore[attr-defined]
    finally:
        connection.close()  # type: ignore[attr-defined]


def _probe_cuda_in_spawned_process(timeout_seconds: float) -> bool:
    """Frozen builds cannot run ``python -c``; use a spawned process instead."""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    worker = context.Process(target=_cuda_probe_child, args=(sender,))
    # A hung driver must not keep a closing backend alive.
    worker.daemon = True
    deadline = time.monotonic() + timeout_seconds
    try:
        worker.start()
        sender.close()
        if not receiver.poll(timeout_seconds):
            return False
        try:
            return receiver.recv() is True
        except EOFError:
            return False
    finally:
        if worker.pid is not None:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=0.5)
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=0.5)
        receiver.close()
        sender.close()


def _probe_cuda_in_child_process(timeout_seconds: float = _CUDA_PROBE_TIMEOUT_SECONDS) -> bool:
    """Run the kernel-launch probe in a short-lived child; any failure is False."""
    if not _torch_installed():
        return False
    try:
        if getattr(sys, "frozen", False):
            return _probe_cuda_in_spawned_process(timeout_seconds)
        completed = subprocess.run(
            [sys.executable, "-c", _CUDA_PROBE_SCRIPT],
            **background_process_kwargs(),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except Exception:
        LOGGER.warning("CUDA availability probe failed; using CPU.", exc_info=True)
        return False
    lines = (completed.stdout or "").strip().splitlines()
    try:
        payload = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError:
        payload = None
    if not isinstance(payload, dict):
        LOGGER.warning(
            "CUDA availability probe exited with code %s and no result; using CPU. %s",
            completed.returncode,
            (completed.stderr or "").strip()[-500:],
        )
        return False
    return payload.get("available") is True


def _normalize_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _normalize_device(value: object) -> str | None:
    normalized = _normalize_text(value)
    if normalized is not None and normalized.lower() == "auto":
        return None
    return normalized


def canonical_provider(provider: str | None) -> str | None:
    """Normalize provider aliases to canonical runtime names."""
    normalized = _normalize_text(provider)
    if normalized is None:
        return None
    lowered = normalized.lower()
    if lowered == "auto":
        return None
    if lowered in {"mlx-audio", "mlx_audio"}:
        return "mlx"
    return lowered


KOKORO_MLX_DEFAULT_MODEL = "mlx-community/Kokoro-82M"
# The MLX engine appends the quantization suffix to the base name at load time
# (engine_runtime.get_model_path); the suffixless base is NOT a real Hub repo.
# Downloads and cache checks must target the concrete default-variant repo.
KOKORO_MLX_DEFAULT_MODEL_REPO = f"{KOKORO_MLX_DEFAULT_MODEL}-bf16"
KOKORO_PACKAGE_DEFAULT_MODEL = "hexgrad/Kokoro-82M"
CLEANER_MLX_DEFAULT_MODEL = "mlx-community/Llama-3.2-3B-Instruct-4bit"
CLEANER_CUDA_DEFAULT_MODEL = "Qwen/Qwen2.5-1.5B-Instruct"

# Provider options a runtime actually reads: only the package (CPU/CUDA)
# runtime's opt-in decoder compilation.
PACKAGE_RUNTIME_OPTION_KEYS = frozenset({"torch_compile"})


@dataclass(frozen=True)
class TTSRuntimeSpec:
    """Immutable identity for a concrete TTS runtime."""

    provider: str
    engine: str
    model: str | None
    quantization: str
    device: str | None = None
    runtime_options: tuple[tuple[str, object], ...] = ()

    @property
    def options(self) -> tuple[tuple[str, str], ...]:
        """Stable string cache tuples derived from typed runtime_options."""
        return self.canonicalize_options(dict(self.runtime_options))

    @classmethod
    def from_legacy(
        cls,
        engine: str,
        quantization: str,
        *,
        model: str | None = None,
        provider: str | None = None,
        device: str | None = None,
        options: Mapping[str, object] | None = None,
    ) -> "TTSRuntimeSpec":
        normalized_engine = (_normalize_text(engine) or "kokoro").lower()
        normalized_provider = canonical_provider(
            _normalize_text(provider) or cls.default_provider(normalized_engine)
        ) or cls.default_provider(normalized_engine)
        normalized_quantization = (_normalize_text(quantization) or "bf16").lower()
        spec = cls(
            provider=normalized_provider,
            engine=normalized_engine,
            model=_normalize_text(model),
            quantization=normalized_quantization,
            device=_normalize_device(device),
            runtime_options=cls.canonicalize_runtime_options(options),
        )
        from modules.tts.runtime_options import validate_device, validate_quantization

        validate_device(spec.provider, spec.device)
        validate_quantization(spec.provider, spec.quantization)
        return spec

    @staticmethod
    def mlx_supported_on_current_platform() -> bool:
        return (
            platform.system() == "Darwin"
            and platform.machine().lower() in {"arm64", "aarch64"}
        )

    @staticmethod
    def cuda_available_on_current_platform() -> bool:
        # torch.cuda.is_available() alone can be true on a broken driver, so
        # the answer comes from a real kernel launch, once per visible-device
        # set: here when that is free, otherwise in a short-lived child.
        visible_devices = os.environ.get("CUDA_VISIBLE_DEVICES")
        with _CUDA_PROBE_LOCK:
            if _cuda_probe_runs_in_process():
                try:
                    import torch
                except Exception:
                    return False
                key: tuple[str | None, int | None] = (visible_devices, id(torch))
                cached = _CUDA_PROBE_CACHE.get(key)
                if cached is not None and cached[0] is torch:
                    return cached[1]
                available = _launch_cuda_probe_kernel(torch)
                _CUDA_PROBE_CACHE[key] = (torch, available)
                return available
            key = (visible_devices, None)
            cached = _CUDA_PROBE_CACHE.get(key)
            if cached is not None:
                return cached[1]
            available = _probe_cuda_in_child_process()
            _CUDA_PROBE_CACHE[key] = (None, available)
            return available

    @staticmethod
    def reset_cuda_probe_cache() -> None:
        """Forget memoized CUDA probe results (tests and device changes)."""
        with _CUDA_PROBE_LOCK:
            _CUDA_PROBE_CACHE.clear()

    @classmethod
    def default_provider(cls, engine: str) -> str:
        if str(engine).strip().lower() == "kokoro":
            if cls.mlx_supported_on_current_platform():
                return "mlx"
            return "cuda" if cls.cuda_available_on_current_platform() else "cpu"
        return "local"

    @staticmethod
    def _stable_option_value(value: object) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            return "null"
        if isinstance(value, (str, int, float)):
            return str(value).strip()
        try:
            return json.dumps(value, sort_keys=True, separators=(",", ":"))
        except TypeError as exc:
            raise ValueError(
                f"TTS runtime option value for {type(value).__name__} is not stable"
            ) from exc

    @classmethod
    def canonicalize_options(
        cls,
        options: Mapping[str, object] | None,
    ) -> tuple[tuple[str, str], ...]:
        if not options:
            return ()
        canonical: list[tuple[str, str]] = []
        for key, value in options.items():
            normalized_key = str(key).strip()
            if not normalized_key:
                continue
            canonical.append((normalized_key, cls._stable_option_value(value)))
        return tuple(sorted(canonical))

    @classmethod
    def canonicalize_runtime_options(
        cls,
        options: Mapping[str, object] | None,
    ) -> tuple[tuple[str, object], ...]:
        if not options:
            return ()
        canonical: list[tuple[str, object]] = []
        for key, value in options.items():
            normalized_key = str(key).strip()
            if not normalized_key:
                continue
            cls._stable_option_value(value)
            canonical.append((normalized_key, value))
        return tuple(sorted(canonical))

    def resolved_model(self) -> str | None:
        """Return the concrete model identity used for load and cache keys."""
        if self.model is not None:
            return self.model
        if self.engine == "kokoro":
            if self.provider in {"cuda", "cpu"}:
                return KOKORO_PACKAGE_DEFAULT_MODEL
            return KOKORO_MLX_DEFAULT_MODEL
        return None

    def cache_key(self) -> tuple[str, str, str | None, str, str | None, tuple[tuple[str, str], ...]]:
        return (
            self.provider,
            self.engine,
            self.resolved_model(),
            self.quantization,
            self.device,
            self.options,
        )

    def engine_config_kwargs(self) -> dict[str, str]:
        """Derive engine identity fields from this spec only."""
        resolved_model = self.resolved_model()
        if resolved_model is None:
            raise ValueError(
                f"TTS runtime spec for engine '{self.engine}' has no resolved model"
            )
        return {
            "model_name": resolved_model,
            "quantization": self.quantization,
            "runtime_cache_key": repr(self.cache_key()),
        }

    def to_dict(self) -> dict[str, object]:
        """Serialize this runtime spec for process boundaries."""
        return {
            "provider": self.provider,
            "engine": self.engine,
            "model": self.model,
            "quantization": self.quantization,
            "device": self.device,
            "options": dict(self.options),
            "runtime_options": dict(self.runtime_options),
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
    ) -> "TTSRuntimeSpec":
        """Rehydrate a runtime spec from a serialized payload."""
        options = payload.get("options")
        option_mapping = options if isinstance(options, Mapping) else None
        runtime_options = payload.get("runtime_options")
        if isinstance(runtime_options, Mapping):
            runtime_option_mapping = runtime_options
            if option_mapping is not None:
                legacy_options = cls.canonicalize_options(option_mapping)
                current_options = cls.canonicalize_options(runtime_option_mapping)
                if legacy_options != current_options:
                    raise ValueError(
                        "Serialized TTS runtime options disagree across "
                        "options and runtime_options."
                    )
        else:
            runtime_option_mapping = option_mapping
        normalized_engine = (_normalize_text(payload.get("engine")) or "kokoro").lower()
        normalized_provider = canonical_provider(
            _normalize_text(payload.get("provider")) or cls.default_provider(normalized_engine)
        ) or cls.default_provider(normalized_engine)
        spec = cls(
            provider=normalized_provider,
            engine=normalized_engine,
            model=_normalize_text(payload.get("model")),
            quantization=(_normalize_text(payload.get("quantization")) or "bf16").lower(),
            device=_normalize_device(payload.get("device")),
            runtime_options=cls.canonicalize_runtime_options(runtime_option_mapping),
        )
        from modules.tts.runtime_options import validate_device, validate_quantization

        validate_device(spec.provider, spec.device)
        validate_quantization(spec.provider, spec.quantization)
        return spec

    @classmethod
    def from_serialized(
        cls,
        value: object,
    ) -> "TTSRuntimeSpec":
        """Accept either an existing spec or a serialized mapping."""
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            return cls.from_dict(value)
        raise TypeError(f"Cannot build TTSRuntimeSpec from {type(value).__name__}")

    def factory_kwargs(self) -> dict[str, object]:
        kwargs: dict[str, object] = {
            "quantization": self.quantization,
            "provider": self.provider,
            "device": self.device,
            "runtime_options": dict(self.runtime_options),
            "runtime_spec": self,
        }
        if self.model is not None:
            kwargs["model_name"] = self.model
        return kwargs

    def label(self) -> str:
        model = f":{self.model}" if self.model else ""
        device = f"@{self.device}" if self.device else ""
        return f"{self.provider}/{self.engine}{model}-{self.quantization}{device}"
