"""Voice preview/runtime discovery service."""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Optional

from modules.app.config import AppConfig
from modules.app.voice_recommender import VoiceRecommender
from modules.tts.runtime_options import TTSRuntimeOptions
from modules.tts.runtime_spec import TTSRuntimeSpec

if TYPE_CHECKING:
    from modules.tts.strategies.base import TTSStrategy


class _SoundFileProxy:
    def write(self, *args: Any, **kwargs: Any) -> Any:
        import soundfile

        return soundfile.write(*args, **kwargs)


sf = _SoundFileProxy()


class VoiceService:
    """Owns preview/runtime validation lifecycle and voice discovery."""

    def __init__(
        self,
        config: AppConfig,
        *,
        tts_factory: Any = None,
        voice_recommender: Optional[VoiceRecommender] = None,
    ) -> None:
        if tts_factory is None:
            from modules.tts.factory import TTSEngineFactory

            tts_factory = TTSEngineFactory
        self._config = config
        self._tts_factory = tts_factory
        self._voice_recommender = voice_recommender or VoiceRecommender(
            api_base_url=config.cleaner_api_base_url,
            api_key_env=config.cleaner_api_key_env,
            api_model=config.cleaner_api_model,
        )
        self._preview_engine: Optional[TTSStrategy] = None
        self._preview_runtime_key: Optional[tuple[object, ...]] = None
        self._validated_tts_runtimes: set[tuple[object, ...]] = set()
        self._runtime_validation_lock = threading.RLock()
        self._perf_metrics: dict[str, Any] = {
            "runtime_validation_hits": 0,
            "runtime_validation_misses": 0,
            "last_runtime_validation_ms": None,
            "preview_generation_count": 0,
        }

    @property
    def runtime_validation_cache_size(self) -> int:
        with self._runtime_validation_lock:
            return len(self._validated_tts_runtimes)

    def metrics_snapshot(self) -> dict[str, Any]:
        with self._runtime_validation_lock:
            return dict(self._perf_metrics)

    def ensure_preview_engine(
        self,
        engine_name: Optional[str] = None,
        quantization: Optional[str] = None,
        tts_provider: Optional[str] = None,
        tts_device: Optional[str] = None,
        tts_options: Optional[dict[str, Any]] = None,
        runtime_spec: Optional[TTSRuntimeSpec] = None,
    ) -> TTSStrategy:
        spec = runtime_spec or self._runtime_spec_from_preview_options(
            engine_name=engine_name,
            tts_quantization=quantization,
            tts_provider=tts_provider,
            tts_device=tts_device,
            tts_options=tts_options,
        )
        runtime_key = spec.cache_key()

        with self._runtime_validation_lock:
            if self._preview_engine is None or self._preview_runtime_key != runtime_key:
                if self._preview_engine is not None:
                    self._preview_engine.unload()
                self._preview_engine = self._create_tts_strategy(spec)
                self._preview_engine.load()
                self._preview_runtime_key = runtime_key
                return self._preview_engine

            if not self._preview_engine.is_loaded:
                self._preview_engine.load()
            return self._preview_engine

    def validate_tts_runtime(
        self,
        tts_engine: str,
        tts_quantization: Optional[str] = None,
        *,
        tts_provider: Optional[str] = None,
        tts_device: Optional[str] = None,
        tts_options: Optional[dict[str, Any]] = None,
        runtime_spec: Optional[TTSRuntimeSpec] = None,
    ) -> None:
        """Validate runtime dependencies and cache successful checks."""
        spec = runtime_spec or self._runtime_spec_from_preview_options(
            engine_name=tts_engine,
            tts_quantization=tts_quantization,
            tts_provider=tts_provider,
            tts_device=tts_device,
            tts_options=tts_options,
        )
        runtime_key = spec.cache_key()
        with self._runtime_validation_lock:
            if runtime_key in self._validated_tts_runtimes:
                self._perf_metrics["runtime_validation_hits"] += 1
                return
            self._perf_metrics["runtime_validation_misses"] += 1

            using_preview_engine = (
                self._preview_engine is not None
                and self._preview_runtime_key == runtime_key
                and self._preview_engine.is_loaded
            )
            strategy = self._preview_engine if using_preview_engine else self._create_tts_strategy(spec)

            started_at = time.perf_counter()
            try:
                strategy.load()
                voices = strategy.supported_voices
                probe_voice = voices[0].id if voices else "am_adam"
                strategy.synthesize("runtime check", voice=probe_voice, speed=1.0)
                self._validated_tts_runtimes.add(runtime_key)
                self._perf_metrics["last_runtime_validation_ms"] = round(
                    (time.perf_counter() - started_at) * 1000,
                    2,
                )
            except Exception as exc:
                raise RuntimeError(
                    f"TTS runtime check failed ({spec.label()}): {exc}"
                ) from exc
            finally:
                if not using_preview_engine:
                    strategy.unload(keep_in_cache=False)

    def generate_voice_preview(
        self,
        voice: str,
        speed: float = 1.0,
        text: Optional[str] = None,
        engine_name: Optional[str] = None,
        tts_quantization: Optional[str] = None,
        tts_provider: Optional[str] = None,
        tts_device: Optional[str] = None,
        tts_options: Optional[dict[str, Any]] = None,
        runtime_spec: Optional[TTSRuntimeSpec] = None,
    ) -> Path:
        if text is None:
            text = (
                "A few light taps upon the pane made him turn to the window. "
                "It had begun to snow again. He watched sleepily the flakes, "
                "silver and dark, falling obliquely against the lamplight."
            )

        spec = runtime_spec or self._runtime_spec_from_preview_options(
            engine_name=engine_name,
            tts_quantization=tts_quantization,
            tts_provider=tts_provider,
            tts_device=tts_device,
            tts_options=tts_options,
        )

        with self._runtime_validation_lock:
            preview_engine = self.ensure_preview_engine(
                runtime_spec=spec,
            )

            if not preview_engine.validate_voice(voice):
                raise ValueError(
                    f"Voice '{voice}' not supported. "
                    f"Available: {[v.id for v in preview_engine.supported_voices]}"
                )

            audio = preview_engine.synthesize(text, voice, speed)
            with tempfile.NamedTemporaryFile(
                suffix=".wav",
                delete=False,
                dir=self._config.temp_dir,
            ) as temp_file:
                temp_path = Path(temp_file.name)
            try:
                sf.write(str(temp_path), audio, preview_engine.sample_rate)
            except Exception:
                temp_path.unlink(missing_ok=True)
                raise
            self._perf_metrics["preview_generation_count"] += 1
            return temp_path

    def _create_tts_strategy(self, spec: TTSRuntimeSpec) -> TTSStrategy:
        return self._tts_factory.create(
            spec.engine,
            **spec.factory_kwargs(),
        )

    def _runtime_spec_from_preview_options(
        self,
        *,
        engine_name: Optional[str],
        tts_quantization: Optional[str],
        tts_provider: Optional[str],
        tts_device: Optional[str],
        tts_options: Optional[dict[str, Any]],
    ) -> TTSRuntimeSpec:
        engine = engine_name or self._config.tts_engine
        typed_options = TTSRuntimeOptions.from_runtime_options(
            SimpleNamespace(
                tts_engine=engine_name,
                tts_quantization=tts_quantization,
                tts_provider=tts_provider,
                tts_device=tts_device,
                tts_options=tts_options,
            ),
            self._config,
        )
        return typed_options.to_runtime_spec(str(engine), self._config)

    def get_available_voices(self, engine_name: Optional[str] = None) -> list[dict]:
        selected_engine = (engine_name or self._config.tts_engine).lower()
        try:
            engine = self._tts_factory.create(selected_engine)
        except Exception:
            return []

        return [
            {
                "id": voice.id,
                "name": voice.name,
                "language": voice.language,
                "locale": voice.locale or voice.language,
                "gender": voice.gender,
                "description": voice.description,
                "style_tags": list(voice.style_tags),
                "sample_text": voice.sample_text,
                "is_multilingual": bool(voice.is_multilingual),
            }
            for voice in engine.supported_voices
        ]

    def suggest_voice_for_book(
        self,
        source_path: Path,
        engine_name: Optional[str] = None,
    ) -> dict[str, Any]:
        if not source_path.exists():
            raise ValueError(f"Source file not found: {source_path}")

        selected_engine = (engine_name or self._config.tts_engine).lower()
        voices = self.get_available_voices(engine_name=selected_engine)
        if not voices:
            raise RuntimeError(f"No voices available for engine '{selected_engine}'")

        recommendation = self._voice_recommender.suggest_for_book(
            source_path=source_path,
            voices=voices,
        )
        recommendation["engine"] = selected_engine
        recommendation["source_path"] = str(source_path)
        recommendation["available_voice_count"] = len(voices)
        return recommendation

    def get_available_tts_engines(self) -> list[dict]:
        engines: list[dict] = []
        for engine_name in self._tts_factory.available_engines():
            info = self._tts_factory.get_engine_info(engine_name)
            capabilities = info.get("capabilities", {})
            if not isinstance(capabilities, dict) or not capabilities.get("supports_conversion", True):
                continue
            engines.append(
                {
                    "id": engine_name,
                    "name": info["display_name"],
                    "display_name": info["display_name"],
                    "available_for_conversion": True,
                    "voices": info["voices"],
                    "capabilities": capabilities,
                    "supported_quantizations": info.get("supported_quantizations", []),
                    "options_schema": info.get("options_schema", {}),
                }
            )
        return engines

    def get_available_cleaner_models(self) -> list[str]:
        from modules.tts.cleaner import list_cleaner_models

        models = list_cleaner_models()
        if self._config.cleaner_model_name not in models:
            models.insert(0, self._config.cleaner_model_name)
        return models

    def get_available_api_cleaner_models(self) -> list[str]:
        from modules.tts.cleaner import list_api_cleaner_models

        models = list_api_cleaner_models()
        if self._config.cleaner_api_model and self._config.cleaner_api_model not in models:
            models.insert(0, self._config.cleaner_api_model)
        return models

    def cleanup(self) -> None:
        with self._runtime_validation_lock:
            if self._preview_engine is not None:
                self._preview_engine.unload()
                self._preview_engine = None
                self._preview_runtime_key = None
