"""Typed TTS runtime option parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from modules.tts.quantization import CANONICAL_QUANTIZATIONS, QUANTIZATION_ALIASES
from modules.tts.runtime_spec import (
    CUDA_PROVIDER_OPTION_KEYS,
    KOKORO_PACKAGE_DEFAULT_MODEL,
    TTSRuntimeSpec,
    canonical_provider,
)


_TOP_LEVEL_KEYS = {
    "model_name",
    "tts_model_name",
    "quantization",
    "provider",
    "device",
    "dtype",
    "compile_mode",
    "batch_policy",
    "model_path",
    "provider_options",
}

_QUANTIZATION_ALIASES = frozenset(QUANTIZATION_ALIASES)
GENERIC_TTS_QUANTIZATIONS = CANONICAL_QUANTIZATIONS | _QUANTIZATION_ALIASES
CUDA_TTS_QUANTIZATIONS = (
    (CANONICAL_QUANTIZATIONS - {"6bit"}) | _QUANTIZATION_ALIASES | frozenset({"fp16", "fp32"})
)

PROVIDER_QUANTIZATIONS: dict[str, frozenset[str]] = {
    "mlx": GENERIC_TTS_QUANTIZATIONS,
    "local": GENERIC_TTS_QUANTIZATIONS,
    "cuda": CUDA_TTS_QUANTIZATIONS,
}


def _text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _payload_value(payload: Mapping[str, object], key: str) -> str | None:
    return _text(payload.get(key))


def _validate_known_top_level_keys(payload: Mapping[str, object]) -> None:
    for key in payload:
        normalized_key = str(key).strip()
        if normalized_key not in _TOP_LEVEL_KEYS:
            raise ValueError(
                f"Unsupported top-level TTS option '{normalized_key}'. "
                "Put provider-specific settings under provider_options."
            )


def _quantizations_for_provider(provider: str | None) -> frozenset[str]:
    normalized_provider = canonical_provider(provider)
    if normalized_provider is None:
        return GENERIC_TTS_QUANTIZATIONS
    return PROVIDER_QUANTIZATIONS.get(normalized_provider, GENERIC_TTS_QUANTIZATIONS)


def _engine_supported_quantizations(engine: str) -> frozenset[str] | None:
    from modules.tts.factory import TTSEngineFactory

    try:
        capabilities = TTSEngineFactory.get_engine_capabilities(str(engine).strip().lower())
    except ValueError:
        return None
    engine_quantizations = capabilities.get("supported_quantizations")
    if not isinstance(engine_quantizations, (list, tuple, frozenset, set)):
        return None
    normalized = frozenset(str(quantization).strip().lower() for quantization in engine_quantizations)
    return normalized or None


def validate_quantization(
    provider: str | None,
    quantization: str | None,
    *,
    engine: str | None = None,
) -> None:
    """Validate provider/quantization combinations, optionally intersecting engine support."""
    normalized_quantization = _text(quantization)
    if normalized_quantization is None:
        return

    normalized_provider = canonical_provider(provider)
    allowed = _quantizations_for_provider(normalized_provider)
    if engine is not None:
        engine_allowed = _engine_supported_quantizations(engine)
        if engine_allowed is not None:
            allowed = allowed & engine_allowed

    lowered_quantization = normalized_quantization.lower()
    if lowered_quantization in allowed:
        return

    if normalized_provider == "cuda":
        scope = "CUDA"
    elif normalized_provider is not None:
        scope = f"{normalized_provider} provider"
    else:
        scope = "TTS runtime"
    available = ", ".join(sorted(allowed))
    raise ValueError(
        f"Unsupported {scope} quantization '{quantization}'. Supported values: {available}"
    )


def validate_quantization_for_provider(
    provider: str | None,
    quantization: str | None,
) -> None:
    """Backward-compatible provider-only quantization validation."""
    validate_quantization(provider, quantization)


def validate_device(provider: str | None, device: str | None) -> None:
    """Validate provider/device shape without probing hardware."""
    normalized_provider = canonical_provider(provider)
    normalized_device = _text(device)
    if normalized_device is None:
        return
    if normalized_provider == "cuda" and not re.fullmatch(r"cuda(?::\d+)?", normalized_device):
        raise ValueError("CUDA device must be 'cuda' or 'cuda:<index>'.")
    if normalized_provider is not None and normalized_provider != "cuda":
        if re.fullmatch(r"cuda(?::\d+)?", normalized_device):
            raise ValueError(
                f"{normalized_provider} provider cannot use CUDA device '{normalized_device}'."
            )


def _provider_from_provider_options(
    provider_options: Mapping[str, object],
) -> str | None:
    nested = _text(provider_options.get("provider"))
    return canonical_provider(nested)


def _infer_provider_from_device(device: str | None) -> str | None:
    normalized_device = _text(device)
    if normalized_device is None:
        return None
    if re.fullmatch(r"cuda(?::\d+)?", normalized_device):
        return "cuda"
    raise ValueError(
        f"Cannot infer TTS provider from device '{normalized_device}'. "
        "Set provider explicitly."
    )


def _strip_provider_from_options(
    provider_options: Mapping[str, object],
) -> dict[str, object]:
    stripped = dict(provider_options)
    stripped.pop("provider", None)
    return stripped


def validate_provider_options(
    provider: str | None,
    options: Mapping[str, object] | tuple[tuple[str, str], ...] | None,
) -> None:
    """Validate provider-specific option names."""
    normalized_provider = canonical_provider(provider)
    if not options:
        return
    keys = (
        [key for key, _value in options]
        if isinstance(options, tuple)
        else [str(key).strip() for key in options]
    )
    if normalized_provider != "cuda":
        for key in keys:
            if key in CUDA_PROVIDER_OPTION_KEYS:
                scope = normalized_provider or "unspecified"
                raise ValueError(
                    f"CUDA provider option '{key}' is not supported for {scope} provider."
                )
        return
    for key in keys:
        if key not in CUDA_PROVIDER_OPTION_KEYS:
            raise ValueError(f"Unsupported CUDA provider option '{key}'.")


@dataclass(frozen=True)
class TTSRuntimeOptions:
    """Canonical typed representation of public TTS runtime options."""

    model_name: str | None = None
    quantization: str | None = None
    provider: str | None = None
    device: str | None = None
    dtype: str | None = None
    compile_mode: str | None = None
    batch_policy: str | None = None
    model_path: str | None = None
    runtime_provider_options: tuple[tuple[str, object], ...] = ()

    @property
    def provider_options(self) -> tuple[tuple[str, str], ...]:
        """Stable string tuples derived from typed runtime_provider_options."""
        return TTSRuntimeSpec.canonicalize_options(dict(self.runtime_provider_options))

    @classmethod
    def from_serialized(cls, value: object) -> "TTSRuntimeOptions":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError(f"Cannot build TTSRuntimeOptions from {type(value).__name__}")
        provider_options = value.get("provider_options")
        provider_options_mapping = provider_options if isinstance(provider_options, Mapping) else {}
        runtime_provider_options = value.get("runtime_provider_options")
        if isinstance(runtime_provider_options, Mapping):
            runtime_provider_options_mapping = runtime_provider_options
        elif provider_options_mapping:
            runtime_provider_options_mapping = TTSRuntimeSpec.coerce_legacy_runtime_options(
                provider_options_mapping
            )
        else:
            runtime_provider_options_mapping = {}
        return cls(
            model_name=_text(value.get("model_name")),
            quantization=_text(value.get("quantization")),
            provider=canonical_provider(_text(value.get("provider"))),
            device=_text(value.get("device")),
            dtype=_text(value.get("dtype")),
            compile_mode=_text(value.get("compile_mode")),
            batch_policy=_text(value.get("batch_policy")),
            model_path=_text(value.get("model_path")),
            runtime_provider_options=TTSRuntimeSpec.canonicalize_runtime_options(
                runtime_provider_options_mapping
            ),
        )

    @classmethod
    def from_payload(
        cls,
        tts_options: Mapping[str, object] | None,
    ) -> "TTSRuntimeOptions":
        payload = tts_options or {}
        _validate_known_top_level_keys(payload)
        provider = canonical_provider(_payload_value(payload, "provider"))
        provider_options_payload = _mapping(payload.get("provider_options")) or {}
        runtime_provider_options = TTSRuntimeSpec.canonicalize_runtime_options(
            provider_options_payload
        )
        device = _payload_value(payload, "device")
        quantization = _payload_value(payload, "quantization")
        validate_device(provider, device)
        resolved = cls(
            model_name=(
                _payload_value(payload, "model_name")
                or _payload_value(payload, "tts_model_name")
            ),
            quantization=quantization,
            provider=provider,
            device=device,
            dtype=_payload_value(payload, "dtype"),
            compile_mode=_payload_value(payload, "compile_mode"),
            batch_policy=_payload_value(payload, "batch_policy"),
            model_path=_payload_value(payload, "model_path"),
            runtime_provider_options=runtime_provider_options,
        ).with_resolved_provider()
        validate_quantization(resolved.provider, resolved.quantization)
        validate_provider_options(resolved.provider, resolved.provider_options)
        return resolved

    @classmethod
    def from_runtime_options(
        cls,
        runtime_options: Any,
        config: Any,
    ) -> "TTSRuntimeOptions":
        payload = getattr(runtime_options, "tts_options", None)
        parsed = cls.from_payload(payload if isinstance(payload, Mapping) else None)
        provider = canonical_provider(
            getattr(runtime_options, "tts_provider", None)
            or parsed.provider
            or getattr(config, "tts_provider", None)
        )
        if parsed.provider is not None and provider is not None and parsed.provider != provider:
            raise ValueError(
                "Conflicting TTS provider values across provider, device, "
                "and provider_options."
            )
        device = (
            _text(getattr(runtime_options, "tts_device", None))
            or parsed.device
            or _text(getattr(config, "tts_device", None))
        )
        quantization = (
            _text(getattr(runtime_options, "tts_quantization", None))
            or parsed.quantization
            or _text(getattr(config, "tts_quantization", None))
        )
        resolved = cls(
            model_name=parsed.model_name or _text(getattr(config, "tts_model_name", None)),
            quantization=quantization,
            provider=provider,
            device=device,
            dtype=parsed.dtype,
            compile_mode=parsed.compile_mode,
            batch_policy=parsed.batch_policy,
            model_path=parsed.model_path,
            runtime_provider_options=parsed.runtime_provider_options,
        ).with_resolved_provider()
        validate_quantization(resolved.provider, resolved.quantization)
        validate_provider_options(resolved.provider, resolved.provider_options)
        return resolved

    def with_resolved_provider(self) -> "TTSRuntimeOptions":
        """Hoist provider from provider_options and infer CUDA from device when needed."""
        provider = self.provider
        runtime_options_mapping = dict(self.runtime_provider_options)

        nested_runtime_provider = _provider_from_provider_options(runtime_options_mapping)
        if nested_runtime_provider is not None:
            if provider is None:
                provider = nested_runtime_provider
            elif provider != nested_runtime_provider:
                raise ValueError(
                    "Conflicting TTS provider values across provider, device, "
                    "and provider_options."
                )

        if provider is None:
            provider = _infer_provider_from_device(self.device)

        runtime_options_mapping = _strip_provider_from_options(runtime_options_mapping)
        validate_device(provider, self.device)
        return TTSRuntimeOptions(
            model_name=self.model_name,
            quantization=self.quantization,
            provider=provider,
            device=self.device,
            dtype=self.dtype,
            compile_mode=self.compile_mode,
            batch_policy=self.batch_policy,
            model_path=self.model_path,
            runtime_provider_options=TTSRuntimeSpec.canonicalize_runtime_options(
                runtime_options_mapping
            ),
        )

    @classmethod
    def from_runtime_spec(cls, spec: TTSRuntimeSpec) -> "TTSRuntimeOptions":
        options_mapping = dict(spec.runtime_options)
        dtype = _text(options_mapping.pop("dtype", None))
        compile_mode = _text(options_mapping.pop("compile_mode", None))
        batch_policy = _text(options_mapping.pop("batch_policy", None))
        model_path = _text(options_mapping.pop("model_path", None))
        return cls(
            model_name=spec.model,
            quantization=spec.quantization,
            provider=spec.provider,
            device=spec.device,
            dtype=dtype,
            compile_mode=compile_mode,
            batch_policy=batch_policy,
            model_path=model_path,
            runtime_provider_options=TTSRuntimeSpec.canonicalize_runtime_options(
                options_mapping
            ),
        )

    @classmethod
    def from_legacy_pipeline_fields(
        cls,
        *,
        tts_engine: str,
        tts_model_name: str | None,
        tts_quantization: str,
    ) -> "TTSRuntimeOptions":
        return cls(
            model_name=tts_model_name,
            quantization=tts_quantization,
        ).with_resolved_provider()

    def _spec_options(self) -> dict[str, object]:
        options = dict(self.runtime_provider_options)
        if self.dtype is not None:
            options["dtype"] = self.dtype
        if self.compile_mode is not None:
            options["compile_mode"] = self.compile_mode
        if self.batch_policy is not None:
            options["batch_policy"] = self.batch_policy
        if self.model_path is not None:
            options["model_path"] = self.model_path
        return options

    def to_dict(self) -> dict[str, object]:
        return {
            "model_name": self.model_name,
            "quantization": self.quantization,
            "provider": self.provider,
            "device": self.device,
            "dtype": self.dtype,
            "compile_mode": self.compile_mode,
            "batch_policy": self.batch_policy,
            "model_path": self.model_path,
            "provider_options": dict(self.provider_options),
            "runtime_provider_options": dict(self.runtime_provider_options),
        }

    def to_runtime_spec(self, engine: str, config: Any) -> TTSRuntimeSpec:
        resolved = self.with_resolved_provider()
        model_name = resolved.model_name
        if (
            model_name is None
            and str(engine or "").strip().lower() == "kokoro"
            and resolved.provider in {"cuda", "cpu"}
        ):
            model_name = KOKORO_PACKAGE_DEFAULT_MODEL
        return TTSRuntimeSpec.from_legacy(
            engine,
            resolved.quantization or getattr(config, "tts_quantization", "bf16"),
            model=model_name,
            provider=resolved.provider,
            device=resolved.device,
            options=resolved._spec_options(),
        )


def _runtime_option_conflicts(
    runtime_spec: TTSRuntimeSpec,
    runtime_options: TTSRuntimeOptions,
) -> list[str]:
    conflicts: list[str] = []
    if (
        runtime_options.quantization is not None
        and runtime_options.quantization != runtime_spec.quantization
    ):
        conflicts.append("quantization")
    if runtime_options.provider is not None and runtime_options.provider != runtime_spec.provider:
        conflicts.append("provider")
    if runtime_options.device is not None and runtime_options.device != runtime_spec.device:
        conflicts.append("device")
    if runtime_options.model_name is not None and runtime_options.model_name != runtime_spec.model:
        conflicts.append("model_name")
    runtime_option_tuples = TTSRuntimeSpec.canonicalize_runtime_options(
        runtime_options._spec_options()
    )
    spec_option_tuples = TTSRuntimeSpec.canonicalize_runtime_options(
        dict(runtime_spec.runtime_options)
    )
    if runtime_option_tuples != spec_option_tuples:
        conflicts.append("runtime_options")
    return conflicts


def resolve_tts_runtime(
    *,
    tts_engine: str,
    tts_model_name: str | None,
    tts_quantization: str,
    tts_runtime: TTSRuntimeSpec | None,
    tts_runtime_options: "TTSRuntimeOptions | None",
) -> tuple[TTSRuntimeSpec, TTSRuntimeOptions]:
    """Resolve a single canonical runtime spec and typed options pair."""
    if tts_runtime is not None:
        if tts_runtime_options is not None:
            resolved_options = tts_runtime_options.with_resolved_provider()
            conflicts = _runtime_option_conflicts(tts_runtime, resolved_options)
            if conflicts:
                raise ValueError(
                    "tts_runtime_options disagree with tts_runtime for "
                    f"{', '.join(conflicts)}"
                )
            if resolved_options == tts_runtime_options:
                return tts_runtime, tts_runtime_options
            return tts_runtime, resolved_options
        return tts_runtime, TTSRuntimeOptions.from_runtime_spec(tts_runtime)

    if tts_runtime_options is not None:
        resolved_options = tts_runtime_options.with_resolved_provider()
    else:
        resolved_options = TTSRuntimeOptions.from_legacy_pipeline_fields(
            tts_engine=tts_engine,
            tts_model_name=tts_model_name,
            tts_quantization=tts_quantization,
        )

    config_like = type("_ConfigLike", (), {"tts_quantization": tts_quantization})()
    derived_spec = resolved_options.to_runtime_spec(tts_engine, config_like)
    final_options = TTSRuntimeOptions.from_runtime_spec(derived_spec)
    return derived_spec, final_options
