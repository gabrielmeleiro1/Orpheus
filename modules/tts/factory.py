"""
TTS Engine Factory
==================
Factory pattern for creating TTS strategy instances.
Enables runtime selection of TTS engines.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from typing import Optional, Type

from modules.tts.strategies.base import TTSConfig, TTSStrategy


class TTSEngineFactory:
    """
    Factory for creating TTS strategy instances.

    Usage:
        engine = TTSEngineFactory.create("kokoro")
        engines = TTSEngineFactory.available_engines()
    """

    _strategies: dict[str, Type[TTSStrategy]] = {}

    _config_types: dict[str, Type[TTSConfig]] = {}

    _optional_loaded: bool = False

    @staticmethod
    def _static_kokoro_info() -> dict[str, object]:
        from modules.tts.voices import KOKORO_VOICE_CATALOG

        capabilities = {
            "supports_batching": True,
            "supports_streaming": True,
            "supports_native_batching": False,
            "batching_mode": "threaded",
            "supported_quantizations": ("bf16",),
            "supported_languages": ("en-US", "en-GB"),
            "supports_voice_cloning": False,
            "supports_engine_options": True,
            "is_local": True,
            "provider": "mlx",
            "supports_conversion": True,
            "worker_auto_mode": "sequential",
            "process_safe": False,
        }
        return {
            "id": "kokoro",
            "name": "Kokoro 82M",
            "display_name": "Kokoro 82M",
            "version": "1.0.0",
            "sample_rate": 24000,
            "capabilities": capabilities,
            "supported_providers": ["mlx", "cuda", "cpu"],
            "supported_quantizations": ["bf16"],
            "options_schema": {
                "quantization": {
                    "type": "string",
                    "enum": ["bf16"],
                    "default": "bf16",
                },
                "enable_streaming": {
                    "type": "boolean",
                    "default": True,
                },
            },
            "voices": [
                {
                    "id": str(entry["id"]),
                    "name": str(entry["name"]),
                    "language": str(entry["language"]),
                    "locale": str(entry.get("locale") or entry["language"]),
                    "gender": str(entry["gender"]),
                    "description": str(entry["description"]),
                    "style_tags": list(entry.get("style_tags", [])),
                    "sample_text": (
                        entry.get("sample_text")
                        if isinstance(entry.get("sample_text"), str)
                        else None
                    ),
                    "is_multilingual": bool(entry.get("is_multilingual", False)),
                }
                for entry in KOKORO_VOICE_CATALOG
            ],
        }

    @staticmethod
    def _build_config(config_class: Type[TTSConfig], config_kwargs: dict[str, object]) -> TTSConfig:
        """Build a strategy config, ignoring kwargs not supported by that config class."""
        if not is_dataclass(config_class):
            return config_class(**config_kwargs)

        supported_fields = {field.name for field in fields(config_class)}
        filtered_kwargs = {
            key: value
            for key, value in config_kwargs.items()
            if key in supported_fields
        }
        return config_class(**filtered_kwargs)

    @classmethod
    def _load_optional_strategies(cls) -> None:
        """Mark optional strategy loading complete.

        Kokoro is the only supported app TTS engine. This hook remains so older
        call sites that trigger lazy loading keep the same control flow.
        """
        if cls._optional_loaded:
            return
        from modules.tts.strategies.kokoro import KokoroConfig, KokoroTTSStrategy

        cls._strategies.setdefault("kokoro", KokoroTTSStrategy)
        cls._config_types.setdefault("kokoro", KokoroConfig)
        cls._optional_loaded = True

    @classmethod
    def create(
        cls,
        engine_type: str,
        config: Optional[TTSConfig] = None,
        **config_kwargs,
    ) -> TTSStrategy:
        """
        Create a TTS strategy instance.

        Args:
            engine_type: Engine identifier ('kokoro', etc.)
            config: Optional configuration object
            **config_kwargs: Configuration parameters if config not provided

        Returns:
            TTSStrategy instance

        Raises:
            ValueError: If engine_type is not registered
            TypeError: If config type doesn't match engine
        """
        cls._load_optional_strategies()
        engine_type = engine_type.lower()

        if engine_type not in cls._strategies:
            available = ", ".join(cls.available_engines())
            raise ValueError(
                f"Unknown TTS engine: '{engine_type}'. "
                f"Available engines: {available}"
            )

        strategy_class = cls._strategies[engine_type]

        if config is not None:
            expected_config_type = cls._config_types.get(engine_type, TTSConfig)
            if not isinstance(config, expected_config_type):
                raise TypeError(
                    f"Expected {expected_config_type.__name__} for {engine_type}, "
                    f"got {type(config).__name__}"
                )
            return strategy_class(config)

        if config_kwargs:
            config_type = cls._config_types.get(engine_type, TTSConfig)
            config = cls._build_config(config_type, config_kwargs)
            return strategy_class(config)

        return strategy_class()

    @classmethod
    def register(
        cls,
        name: str,
        strategy_class: Type[TTSStrategy],
        config_class: Type[TTSConfig] = TTSConfig,
    ) -> None:
        """
        Register a new TTS strategy.

        Args:
            name: Engine identifier
            strategy_class: Strategy class implementing TTSStrategy
            config_class: Configuration class for this strategy
        """
        cls._strategies[name.lower()] = strategy_class
        cls._config_types[name.lower()] = config_class

    @classmethod
    def available_engines(cls) -> list[str]:
        """
        List available engine names.

        Returns:
            List of registered engine identifiers
        """
        return sorted(set(cls._strategies) | {"kokoro"})

    @classmethod
    def available_conversion_engines(cls, provider: Optional[str] = None) -> list[str]:
        """List engines available for conversion, optionally filtered by provider."""
        normalized_provider = None
        if provider is not None:
            normalized_provider = str(provider).strip().lower()
            if normalized_provider in {"mlx-audio", "mlx_audio"}:
                normalized_provider = "mlx"
        engine_names: list[str] = []
        for engine_type in cls.available_engines():
            try:
                info = cls.get_engine_info(engine_type)
            except Exception:
                continue
            capabilities = info.get("capabilities", {})
            if not isinstance(capabilities, dict):
                continue
            if not capabilities.get("supports_conversion", True):
                continue
            supported_providers = info.get("supported_providers", ())
            if not isinstance(supported_providers, (list, tuple, set, frozenset)):
                supported_providers = ()
            normalized_supported_providers = {
                str(item).strip().lower()
                for item in supported_providers
                if str(item).strip()
            }
            engine_provider = capabilities.get("provider")
            if isinstance(engine_provider, str):
                engine_provider = engine_provider.strip().lower()
            if (
                normalized_provider is not None
                and engine_provider != normalized_provider
                and normalized_provider not in normalized_supported_providers
            ):
                continue
            engine_names.append(engine_type)
        return sorted(engine_names)

    @classmethod
    def get_engine_capabilities(cls, engine_type: str) -> dict[str, object]:
        """Return normalized capability metadata for an engine."""
        info = cls.get_engine_info(engine_type)
        capabilities = info.get("capabilities", {})
        return dict(capabilities) if isinstance(capabilities, dict) else {}

    @classmethod
    def get_engine_info(cls, engine_type: str) -> dict[str, object]:
        """
        Get information about an engine.

        Args:
            engine_type: Engine identifier

        Returns:
            Dict with engine info

        Raises:
            ValueError: If engine not found
        """
        engine_type = engine_type.lower()

        if engine_type == "kokoro" and engine_type not in cls._strategies:
            return cls._static_kokoro_info()

        cls._load_optional_strategies()

        if engine_type not in cls._strategies:
            raise ValueError(f"Unknown engine: {engine_type}")

        strategy_class = cls._strategies[engine_type]
        temp_instance = strategy_class()
        info = temp_instance.to_engine_info()
        info["id"] = engine_type
        if engine_type == "kokoro":
            info["supported_providers"] = ["mlx", "cuda", "cpu"]
        return info

    @classmethod
    def is_available(cls, engine_type: str) -> bool:
        """
        Check if an engine type is registered.

        Args:
            engine_type: Engine identifier

        Returns:
            True if engine is available
        """
        cls._load_optional_strategies()
        return engine_type.lower() in cls._strategies
