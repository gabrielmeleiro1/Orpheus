"""Canonical TTS runtime identity."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping


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
    if lowered in {"mlx-audio", "mlx_audio"}:
        return "mlx"
    return lowered


KOKORO_MLX_DEFAULT_MODEL = "mlx-community/Kokoro-82M"
KOKORO_PACKAGE_DEFAULT_MODEL = "hexgrad/Kokoro-82M"
KOKORO_DEFAULT_MODEL = KOKORO_MLX_DEFAULT_MODEL

# CUDA provider option names and coercion keys (single source of truth).
CUDA_RUNTIME_OPTION_BOOL_KEYS = frozenset(
    {
        "cudnn_conv1d_pad_to_nc1d",
        "cudnn_conv_use_max_workspace",
        "do_copy_in_default_stream",
        "enable_cuda_graph",
        "prefer_nhwc",
        "use_tf32",
    }
)
CUDA_RUNTIME_OPTION_INT_KEYS = frozenset({"device_id", "gpu_mem_limit"})
CUDA_PROVIDER_OPTION_KEYS = CUDA_RUNTIME_OPTION_BOOL_KEYS | CUDA_RUNTIME_OPTION_INT_KEYS | frozenset(
    {
        "arena_extend_strategy",
        "cudnn_conv_algo_search",
        "user_compute_stream",
    }
)


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
            device=_normalize_text(device),
            runtime_options=cls.canonicalize_runtime_options(
                cls.coerce_legacy_runtime_options(options)
            ),
        )
        from modules.tts.runtime_options import validate_device, validate_quantization

        validate_device(spec.provider, spec.device)
        validate_quantization(spec.provider, spec.quantization)
        return spec

    @classmethod
    def from_options(
        cls,
        config: Any,
        runtime_options: Any,
    ) -> "TTSRuntimeSpec":
        from modules.tts.runtime_options import TTSRuntimeOptions

        engine = getattr(runtime_options, "tts_engine", None) or getattr(
            config,
            "tts_engine",
            "kokoro",
        )
        typed_options = TTSRuntimeOptions.from_runtime_options(runtime_options, config)
        return typed_options.to_runtime_spec(str(engine), config)

    @staticmethod
    def default_provider(engine: str) -> str:
        return "mlx" if str(engine).strip().lower() == "kokoro" else "local"

    @staticmethod
    def _coerce_bool_value(value: object) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized == "true":
                return True
            if normalized == "false":
                return False
        return bool(value)

    @staticmethod
    def _coerce_int_value(value: object) -> int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return int(str(value).strip())

    @classmethod
    def coerce_legacy_runtime_option_value(cls, key: str, value: object) -> object:
        if key in CUDA_RUNTIME_OPTION_INT_KEYS:
            return cls._coerce_int_value(value)
        if key in CUDA_RUNTIME_OPTION_BOOL_KEYS:
            return cls._coerce_bool_value(value)
        return value

    @classmethod
    def coerce_legacy_runtime_options(
        cls,
        options: Mapping[str, object] | None,
    ) -> dict[str, object]:
        if not options:
            return {}
        coerced: dict[str, object] = {}
        for key, value in options.items():
            normalized_key = str(key).strip()
            if not normalized_key:
                continue
            coerced[normalized_key] = cls.coerce_legacy_runtime_option_value(
                normalized_key,
                value,
            )
        return coerced

    @classmethod
    def coerce_legacy_runtime_options_from_tuples(
        cls,
        options: tuple[tuple[str, str], ...],
    ) -> tuple[tuple[str, object], ...]:
        return cls.canonicalize_runtime_options(
            cls.coerce_legacy_runtime_options(dict(options))
        )

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
        elif option_mapping is not None:
            runtime_option_mapping = cls.coerce_legacy_runtime_options(option_mapping)
        else:
            runtime_option_mapping = None
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
