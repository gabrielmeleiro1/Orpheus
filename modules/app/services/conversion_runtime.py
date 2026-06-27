"""Runtime option resolution for conversion pipeline jobs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from modules.app.config import AppConfig
from modules.contracts.conversion_runtime_options import ConversionRuntimeOptions
from modules.pipeline.memory_policy import MemorySnapshot, SafeMemoryPolicy
from modules.pipeline.models import PipelineConfig
from modules.pipeline.worker_recommendation import detect_cpu_core_workers
from modules.storage.models import ConversionMode
from modules.tts.api_rate_limit import GlobalAPILimiter
from modules.tts.cleaner_backends.registry import get_cleaner_backend
from modules.tts.cleaner_config import (
    CleanerConfig,
    normalize_cleaner_backend,
    normalize_cuda_device,
    normalize_cuda_quantization,
    resolve_cleaner_model_name,
)
from modules.tts.runtime_options import TTSRuntimeOptions, resolve_tts_runtime
from modules.tts.runtime_spec import TTSRuntimeSpec
from modules.tts.device_memory import DeviceMemoryPolicy, get_combined_device_memory_policy


def _memory_snapshot_from_device_policy(
    device_memory_policy: DeviceMemoryPolicy,
) -> MemorySnapshot | None:
    snapshot = device_memory_policy.sample()
    if snapshot is None:
        return None
    return MemorySnapshot(
        total_bytes=snapshot.total_bytes,
        available_bytes=snapshot.available_bytes,
        process_rss_bytes=snapshot.process_rss_bytes,
        mlx_active_bytes=snapshot.active_bytes,
        mlx_cache_bytes=snapshot.cache_bytes,
        mlx_peak_bytes=snapshot.peak_bytes,
    )


@dataclass(frozen=True)
class NormalizedConversionRuntimeOptions:
    voice: str
    speed: float
    tts_engine: str
    tts_quantization: str
    tts_model_name: Optional[str]
    tts_runtime: TTSRuntimeSpec
    tts_runtime_options: TTSRuntimeOptions
    cleaner_model: Optional[str]
    cleaner_backend: str
    cleaner_cuda_device: str
    cleaner_cuda_quantization: str
    cleaner_api_model: Optional[str]
    cleaner_api_key: Optional[str]
    enable_parallel: bool
    max_parallel_chapters: int
    parallel_worker_mode: str
    worker_auto_mode: str
    process_safe: bool
    memory_policy: Optional[SafeMemoryPolicy]
    device_memory_policy: DeviceMemoryPolicy


class RuntimeOptionsResolver:
    """Coerce, validate, normalize, and materialize runtime options."""

    def __init__(
        self,
        *,
        config: AppConfig,
        cleaner_api_limiter: GlobalAPILimiter,
        runtime_validator: Callable[[str, Optional[str]], None],
        shared_worker_pool_manager: Optional[Any] = None,
    ) -> None:
        self._config = config
        self._cleaner_api_limiter = cleaner_api_limiter
        self._runtime_validator = runtime_validator
        self._shared_worker_pool_manager = shared_worker_pool_manager

    def validate_requested_tts_runtime(
        self,
        *,
        runtime_options: ConversionRuntimeOptions,
    ) -> None:
        tts_runtime = self.resolve_tts_runtime_spec(
            runtime_options=runtime_options,
        )
        self.validate_conversion_tts_engine(tts_runtime.engine)
        try:
            self._runtime_validator(
                tts_runtime.engine,
                tts_runtime.quantization,
                runtime_spec=tts_runtime,
            )
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc

    def _resolve_tts_runtime_pair(
        self,
        *,
        runtime_options: Optional[ConversionRuntimeOptions] = None,
        tts_engine: Optional[str] = None,
        tts_quantization: Optional[str] = None,
        tts_options: Optional[dict[str, Any]] = None,
    ) -> tuple[TTSRuntimeSpec, TTSRuntimeOptions]:
        if runtime_options is None:
            runtime_options = ConversionRuntimeOptions(
                tts_engine=tts_engine,
                tts_quantization=tts_quantization,
                tts_options=tts_options,
            )
        typed_options = TTSRuntimeOptions.from_runtime_options(runtime_options, self._config)
        engine = runtime_options.tts_engine or self._config.tts_engine
        return resolve_tts_runtime(
            tts_engine=engine,
            tts_model_name=self._config.tts_model_name,
            tts_quantization=(
                runtime_options.tts_quantization or self._config.tts_quantization
            ),
            tts_runtime=None,
            tts_runtime_options=typed_options,
        )

    def resolve_tts_runtime_spec(
        self,
        *,
        runtime_options: Optional[ConversionRuntimeOptions] = None,
        tts_engine: Optional[str] = None,
        tts_quantization: Optional[str] = None,
        tts_options: Optional[dict[str, Any]] = None,
    ) -> TTSRuntimeSpec:
        spec, _options = self._resolve_tts_runtime_pair(
            runtime_options=runtime_options,
            tts_engine=tts_engine,
            tts_quantization=tts_quantization,
            tts_options=tts_options,
        )
        return spec

    def resolve_tts_runtime_options(
        self,
        *,
        runtime_options: Optional[ConversionRuntimeOptions] = None,
        tts_engine: Optional[str] = None,
        tts_quantization: Optional[str] = None,
        tts_options: Optional[dict[str, Any]] = None,
    ) -> tuple[str, str]:
        spec = self.resolve_tts_runtime_spec(
            runtime_options=runtime_options,
            tts_engine=tts_engine,
            tts_quantization=tts_quantization,
            tts_options=tts_options,
        )
        return spec.engine, spec.quantization

    def normalize(
        self,
        runtime_options: ConversionRuntimeOptions,
    ) -> NormalizedConversionRuntimeOptions:
        tts_runtime, tts_runtime_options = self._resolve_tts_runtime_pair(
            runtime_options=runtime_options,
        )
        effective_tts_engine = tts_runtime.engine
        effective_tts_quantization = tts_runtime.quantization
        effective_tts_model_name = tts_runtime.resolved_model()

        effective_cleaner_backend = normalize_cleaner_backend(
            runtime_options.cleaner_backend or self._config.cleaner_backend
        )

        effective_cleaner_api_model = (
            runtime_options.cleaner_api_model
            if runtime_options.cleaner_api_model is not None
            else self._config.cleaner_api_model
        )

        # CUDA-only request fields are normalized only for the cuda backend.
        if effective_cleaner_backend == "cuda":
            cuda_device_source = (
                runtime_options.cleaner_cuda_device
                if runtime_options.cleaner_cuda_device is not None
                else self._config.cleaner_cuda_device
            )
            cuda_quant_source = (
                runtime_options.cleaner_cuda_quantization
                if runtime_options.cleaner_cuda_quantization is not None
                else self._config.cleaner_cuda_quantization
            )
            effective_cuda_device = normalize_cuda_device(cuda_device_source)
            effective_cuda_quantization = normalize_cuda_quantization(cuda_quant_source)
        else:
            effective_cuda_device = self._config.cleaner_cuda_device
            effective_cuda_quantization = self._config.cleaner_cuda_quantization

        from modules.tts.factory import TTSEngineFactory

        tts_capabilities = TTSEngineFactory.get_engine_capabilities(effective_tts_engine)
        worker_auto_mode = (
            str(tts_capabilities.get("worker_auto_mode") or "memory_safe").strip().lower()
        )
        process_safe = bool(tts_capabilities.get("process_safe", False))
        normalized_worker_mode = str(runtime_options.parallel_worker_mode or "auto").strip().lower()

        requested_max_parallel = (
            max(1, int(runtime_options.max_parallel_chapters))
            if runtime_options.max_parallel_chapters is not None
            else self._config.max_parallel_chapters
        )
        requested_enable_parallel = (
            bool(runtime_options.enable_parallel)
            if runtime_options.enable_parallel is not None
            else self._config.enable_parallel
        )
        if normalized_worker_mode == "auto" and worker_auto_mode == "cpu_cores" and process_safe:
            requested_max_parallel = detect_cpu_core_workers()
            if runtime_options.enable_parallel is None:
                requested_enable_parallel = requested_max_parallel > 1
        if requested_max_parallel <= 1:
            requested_enable_parallel = False

        memory_policy = None
        resolved_cleaner_model = resolve_cleaner_model_name(
            backend=effective_cleaner_backend,
            requested_model=runtime_options.cleaner_model,
            configured_model=self._config.cleaner_model_name,
        )
        cleaner_config = CleanerConfig(
            model_name=resolved_cleaner_model,
            backend=effective_cleaner_backend,
            cuda_device=effective_cuda_device,
            cuda_quantization=effective_cuda_quantization,
        )
        device_memory_policy = get_combined_device_memory_policy(
            tts_runtime,
            cleaner_config,
        )
        if requested_enable_parallel and worker_auto_mode != "cpu_cores":
            estimated_worker_bytes = device_memory_policy.estimated_worker_bytes(
                tts_runtime,
            )
            memory_policy = SafeMemoryPolicy(
                memory_sampler=lambda: _memory_snapshot_from_device_policy(
                    device_memory_policy,
                ),
                estimated_worker_bytes=estimated_worker_bytes,
                enable_warmup_calibration=normalized_worker_mode == "auto",
                unavailable_reason=(
                    device_memory_policy.sampling_unavailable_reason
                    or "memory measurement unavailable"
                ),
            )

        return NormalizedConversionRuntimeOptions(
            voice=runtime_options.voice,
            speed=runtime_options.speed,
            tts_engine=effective_tts_engine,
            tts_quantization=effective_tts_quantization,
            tts_model_name=effective_tts_model_name,
            tts_runtime=tts_runtime,
            tts_runtime_options=tts_runtime_options,
            cleaner_model=runtime_options.cleaner_model,
            cleaner_backend=effective_cleaner_backend,
            cleaner_cuda_device=effective_cuda_device,
            cleaner_cuda_quantization=effective_cuda_quantization,
            cleaner_api_model=effective_cleaner_api_model,
            cleaner_api_key=(
                runtime_options.cleaner_api_key.strip()
                if runtime_options.cleaner_api_key and runtime_options.cleaner_api_key.strip()
                else None
            ),
            enable_parallel=requested_enable_parallel,
            max_parallel_chapters=requested_max_parallel,
            parallel_worker_mode=normalized_worker_mode,
            worker_auto_mode=worker_auto_mode,
            process_safe=process_safe,
            memory_policy=memory_policy,
            device_memory_policy=device_memory_policy,
        )

    def create_pipeline_config(
        self,
        *,
        conversion_mode: ConversionMode | str = ConversionMode.AUDIOBOOK,
        runtime_options: ConversionRuntimeOptions,
    ) -> PipelineConfig:
        normalized_options = self.normalize(runtime_options)
        resolved_conversion_mode = (
            conversion_mode.value
            if isinstance(conversion_mode, ConversionMode)
            else str(conversion_mode or ConversionMode.AUDIOBOOK.value)
        )
        return PipelineConfig(
            conversion_mode=resolved_conversion_mode,
            tts_engine=normalized_options.tts_engine,
            tts_model_name=normalized_options.tts_model_name,
            tts_quantization=normalized_options.tts_quantization,
            tts_runtime=normalized_options.tts_runtime,
            tts_runtime_options=normalized_options.tts_runtime_options,
            cleaner_model_name=resolve_cleaner_model_name(
                backend=normalized_options.cleaner_backend,
                requested_model=normalized_options.cleaner_model,
                configured_model=self._config.cleaner_model_name,
            ),
            cleaner_backend=normalized_options.cleaner_backend,
            cleaner_cuda_device=normalized_options.cleaner_cuda_device,
            cleaner_cuda_quantization=normalized_options.cleaner_cuda_quantization,
            cleaner_api_base_url=self._config.cleaner_api_base_url,
            cleaner_api_key_env=self._config.cleaner_api_key_env,
            cleaner_api_key=normalized_options.cleaner_api_key,
            cleaner_api_model=normalized_options.cleaner_api_model,
            cleaner_api_global_concurrency=self._config.cleaner_api_global_concurrency,
            cleaner_api_global_limiter=(
                self._cleaner_api_limiter.semaphore
                if get_cleaner_backend(normalized_options.cleaner_backend).requires_api_key
                else None
            ),
            shared_worker_pool_manager=self._shared_worker_pool_manager,
            save_cleaning_debug=self._config.save_cleaning_debug,
            pdf_extraction_backend=self._config.pdf_extraction_backend,
            voice=normalized_options.voice,
            speed=normalized_options.speed,
            output_dir=self._config.output_dir,
            temp_dir=self._config.temp_dir,
            mp3_bitrate=self._config.mp3_bitrate,
            emit_chapter_mp3_artifacts=getattr(
                self._config,
                "emit_chapter_mp3_artifacts",
                False,
            ),
            normalize_volume=self._config.normalize_volume,
            target_dbfs=self._config.target_dbfs,
            chunk_size=self._config.chunk_size,
            enable_tts_file_batch_results=self._config.enable_tts_file_batch_results,
            keep_tts_engine_warm=self._config.keep_tts_engine_warm,
            tts_idle_timeout_seconds=self._config.tts_idle_timeout_seconds,
            enable_parallel=normalized_options.enable_parallel,
            max_parallel_chapters=normalized_options.max_parallel_chapters,
            requested_parallel_chapters=normalized_options.max_parallel_chapters,
            parallel_worker_mode=normalized_options.parallel_worker_mode,
            tts_worker_auto_mode=normalized_options.worker_auto_mode,
            tts_process_safe=normalized_options.process_safe,
            memory_policy=normalized_options.memory_policy,
            device_memory_policy=normalized_options.device_memory_policy,
        )

    @staticmethod
    def validate_conversion_tts_engine(tts_engine: str) -> None:
        from modules.tts.factory import TTSEngineFactory

        conversion_engines = TTSEngineFactory.available_conversion_engines()
        if tts_engine in conversion_engines:
            return
        available = ", ".join(conversion_engines) or "none"
        raise ValueError(
            f"TTS engine '{tts_engine}' is not available for MLX conversion. "
            f"Available engines: {available}"
        )
