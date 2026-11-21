"""Canonical TTS runtime identity."""

from __future__ import annotations

import json
import platform
from dataclasses import dataclass
from typing import Mapping


def _normalize_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


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
            device=None if str(device).strip().lower() == "auto" else _normalize_text(device),
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
        try:
            import torch
        except Exception:
            return False
        try:
            if not torch.cuda.is_available():
                return False
            # Execute and synchronize a tiny kernel with the installed runtime.
            return torch.ones(1, device="cuda").add_(1).item() == 2
        except Exception:
            return False

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
            device=_normalize_text(payload.get("device")),
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
