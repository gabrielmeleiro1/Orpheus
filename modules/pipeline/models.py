"""Shared pipeline data models and callback types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from modules.tts.cleaner_backends.registry import is_local_cleaner_backend
from modules.tts.cleaner_config import CleanerConfig
from modules.tts.runtime_spec import TTSRuntimeSpec
from modules.tts.runtime_options import TTSRuntimeOptions, resolve_tts_runtime
from modules.tts.device_memory import (
    DeviceMemoryPolicy,
    _same_cuda_device_index,
    device_memory_policy_from_serialized,
    get_combined_device_memory_policy,
)


def _detect_available_ram_gb() -> float:
    try:
        from .parallel import _detect_available_ram_gb as detect_available_ram_gb
    except ImportError:
        return 8.0
    return detect_available_ram_gb()


class PipelineStage(Enum):
    """Pipeline processing stages."""

    IDLE = "idle"
    INGESTING = "ingesting"
    CHUNKING = "chunking"
    CLEANING = "cleaning"
    SYNTHESIZING = "synthesizing"
    ENCODING = "encoding"
    PACKAGING = "packaging"
    COMPLETE = "complete"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class WorkerPlan:
    """Immutable chapter worker policy assembled with the pipeline config."""

    requested_workers: int = 1
    max_workers: int = 1
    enable_parallel: bool = False
    worker_mode: str = "auto"
    worker_auto_mode: str = "memory_safe"
    process_safe: bool = False
    use_memory_policy: bool = False
    enforce_vram_budget: bool = True
    initial_reason: Optional[str] = None


def _cuda_tts_cleaner_same_device_contention(config: "PipelineConfig") -> bool:
    tts_runtime = config.tts_runtime
    if tts_runtime is None:
        return False
    if tts_runtime.provider.strip().lower() != "cuda":
        return False
    if str(config.cleaner_backend or "").strip().lower() != "cuda":
        return False
    return _same_cuda_device_index(tts_runtime.device, config.cleaner_cuda_device)


def _local_gpu_model_contention(config: "PipelineConfig") -> bool:
    worker_mode = str(config.parallel_worker_mode or "auto").strip().lower()
    worker_auto_mode = str(config.tts_worker_auto_mode or "memory_safe").strip().lower()
    process_safe = bool(config.tts_process_safe)
    tts_runtime = config.tts_runtime
    cleaner_backend = str(config.cleaner_backend or "").strip().lower()
    if (
        tts_runtime is not None
        and tts_runtime.provider.strip().lower() == "cuda"
        and cleaner_backend == "cuda"
    ):
        return False
    return (
        worker_mode == "auto"
        and str(config.tts_engine or "").strip().lower() == "kokoro"
        and is_local_cleaner_backend(config.cleaner_backend)
        and not (worker_auto_mode == "cpu_cores" and process_safe)
    )


def build_worker_plan(config: "PipelineConfig") -> WorkerPlan:
    """Build the immutable worker policy for a pipeline config."""
    requested_workers = max(
        1,
        int(
            config.requested_parallel_chapters
            if config.requested_parallel_chapters is not None
            else config.max_parallel_chapters
            or 1
        ),
    )
    worker_mode = str(config.parallel_worker_mode or "auto").strip().lower()
    worker_auto_mode = str(config.tts_worker_auto_mode or "memory_safe").strip().lower()
    process_safe = bool(config.tts_process_safe)
    parallel_requested = bool(config.enable_parallel) and requested_workers > 1

    max_workers = requested_workers if parallel_requested else 1
    initial_reason: Optional[str] = None
    cuda_same_device_contention = (
        worker_mode == "auto"
        and not (worker_auto_mode == "cpu_cores" and process_safe)
        and _cuda_tts_cleaner_same_device_contention(config)
    )
    local_gpu_model_contention = _local_gpu_model_contention(config)

    if not parallel_requested:
        initial_reason = "parallel disabled"
    elif worker_mode == "manual":
        initial_reason = "manual worker count"
    elif worker_auto_mode == "cpu_cores" and process_safe:
        initial_reason = "process-safe CPU-core auto worker policy"
    elif worker_auto_mode == "sequential":
        max_workers = 1
        initial_reason = "backend sequential auto worker policy"
    elif cuda_same_device_contention:
        max_workers = 1
        initial_reason = "CUDA TTS + CUDA cleaner same-device contention guard"
    elif local_gpu_model_contention:
        max_workers = 1
        initial_reason = "local GPU model contention guard"
    elif config.memory_policy is not None:
        allowed_workers = getattr(config.memory_policy, "allowed_workers", None)
        if callable(allowed_workers):
            max_workers = max(1, int(allowed_workers(requested_workers)))
            if max_workers < requested_workers:
                initial_reason = (
                    getattr(config.memory_policy, "selected_worker_count_reason", None)
                    or getattr(config.memory_policy, "safe_mode_reason", None)
                    or "memory policy worker guard"
                )

    return WorkerPlan(
        requested_workers=requested_workers,
        max_workers=max(1, int(max_workers)),
        enable_parallel=parallel_requested,
        worker_mode=worker_mode,
        worker_auto_mode=worker_auto_mode,
        process_safe=process_safe,
        use_memory_policy=config.memory_policy is not None,
        enforce_vram_budget=not (
            worker_mode == "manual"
            or (worker_auto_mode == "cpu_cores" and process_safe)
        ),
        initial_reason=initial_reason,
    )


@dataclass
class PipelineConfig:
    """Configuration for conversion pipeline.

    Cleaner settings support local MLX (Apple Silicon), local CUDA (NVIDIA GPU),
    and remote API backends via ``cleaner_backend``.
    """

    conversion_mode: str = "audiobook"
    tts_engine: str = "kokoro"
    tts_model_name: Optional[str] = "mlx-community/Kokoro-82M"
    tts_quantization: str = "bf16"
    tts_runtime: Optional[TTSRuntimeSpec] = None
    tts_runtime_options: Optional[TTSRuntimeOptions] = None
    cleaner_model_name: str = "mlx-community/Llama-3.2-3B-Instruct-4bit"
    cleaner_backend: str = "mlx"
    cleaner_cuda_device: str = "cuda"
    cleaner_cuda_quantization: str = "4bit"
    cleaner_api_base_url: str = "https://api.openai.com/v1"
    cleaner_api_key_env: str = "OPENAI_API_KEY"
    cleaner_api_key: Optional[str] = None
    cleaner_api_model: Optional[str] = "gpt-5-nano"
    save_cleaning_debug: bool = False
    cleaner_skip_llm_when_not_needed: bool = True
    source_type: Optional[str] = None
    pdf_extraction_backend: str = "fitz"
    voice: str = "am_adam"
    speed: float = 1.0
    output_dir: Path = field(default_factory=lambda: Path("output"))
    temp_dir: Path = field(default_factory=lambda: Path("temp"))
    mp3_bitrate: str = "128k"
    emit_chapter_mp3_artifacts: bool = False
    enable_chapter_aac_packaging: bool = False
    enable_background_chapter_aac_encoding: bool = True
    normalize_volume: bool = True
    target_dbfs: float = -16.0
    chunk_size: int = 700
    chapter_start: int = 1

    # Parallel processing settings
    enable_parallel: bool = True
    max_parallel_chapters: int = 2
    requested_parallel_chapters: Optional[int] = None
    parallel_worker_mode: str = "auto"
    tts_worker_auto_mode: str = "memory_safe"
    tts_process_safe: bool = False
    enable_pipelining: bool = True
    enable_chunk_overlap: bool = False
    enable_tts_file_batch_results: bool = False
    tts_file_batch_results_auto_threshold_chunks: int = 64
    keep_tts_engine_warm: bool = True
    tts_idle_timeout_seconds: float = 300.0
    total_vram_gb: float = field(default_factory=_detect_available_ram_gb)
    cleaner_api_global_concurrency: int = 6
    cleaner_api_global_limiter: Optional[Any] = None
    shared_worker_pool_manager: Optional[Any] = None
    memory_policy: Optional[Any] = None
    device_memory_policy: Optional[DeviceMemoryPolicy] = None
    chapter_limit: Optional[int] = None
    pre_chunk_document_cleanup: bool = True
    skip_non_prose_sections: bool = True
    artifact_gated_cleaning: bool = True
    speech_normalization: bool = False
    worker_plan: WorkerPlan = field(init=False)

    def __post_init__(self) -> None:
        self.tts_runtime, self.tts_runtime_options = resolve_tts_runtime(
            tts_engine=self.tts_engine,
            tts_model_name=self.tts_model_name,
            tts_quantization=self.tts_quantization,
            tts_runtime=self.tts_runtime,
            tts_runtime_options=self.tts_runtime_options,
        )
        self.tts_engine = self.tts_runtime.engine
        self.tts_model_name = self.tts_runtime.resolved_model()
        self.tts_quantization = self.tts_runtime.quantization
        device_memory_policy = device_memory_policy_from_serialized(
            self.device_memory_policy
        )
        runtime_provider = self.tts_runtime.provider.strip().lower()
        resolved_cleaner_config = CleanerConfig(
            model_name=self.cleaner_model_name,
            backend=self.cleaner_backend,
            cuda_device=self.cleaner_cuda_device,
            cuda_quantization=self.cleaner_cuda_quantization,
        )
        if device_memory_policy is None or (
            device_memory_policy.provider not in {runtime_provider, "combined"}
        ):
            device_memory_policy = get_combined_device_memory_policy(
                self.tts_runtime,
                resolved_cleaner_config,
            )
        self.device_memory_policy = device_memory_policy
        self.max_parallel_chapters = max(1, int(self.max_parallel_chapters or 1))
        if self.requested_parallel_chapters is None:
            self.requested_parallel_chapters = self.max_parallel_chapters
        else:
            self.requested_parallel_chapters = max(
                1,
                int(self.requested_parallel_chapters or 1),
            )
        self.parallel_worker_mode = str(self.parallel_worker_mode or "auto").strip().lower()
        self.tts_worker_auto_mode = str(self.tts_worker_auto_mode or "memory_safe").strip().lower()
        self.tts_process_safe = bool(self.tts_process_safe)
        self.worker_plan = build_worker_plan(self)


@dataclass
class ChapterResult:
    """Result for a single chapter conversion."""

    chapter_number: int
    chapter_title: str
    wav_path: Optional[Path] = None
    mp3_path: Optional[Path] = None
    aac_path: Optional[Path] = None
    aac_future: Optional[Any] = None
    duration_ms: int = 0
    error: Optional[str] = None
    stage_timings: dict[str, float] = field(default_factory=dict)
    # PCM16 RMS of the streamed chapter WAV, recorded during synthesis so
    # encoding can derive the normalization gain without rescanning the file.
    pcm_rms: Optional[float] = None


@dataclass
class ConversionResult:
    """Result of full conversion pipeline."""

    success: bool
    title: str
    author: Optional[str]
    output_path: Optional[Path] = None
    chapters: list[ChapterResult] = field(default_factory=list)
    total_duration_ms: int = 0
    error: Optional[str] = None
    processing_time_seconds: float = 0.0


@dataclass(frozen=True)
class RepairedChapterUpdate:
    """Library metadata update produced by a successful chapter repair.

    ``mp3_path`` stores the durable per-chapter audio path (``.mp3`` or
    ``.m4a``). It is None only when repair produced no resolvable chapter
    file; persisting None clears a stale reference that triggered the repair.
    """

    chapter_id: int
    duration_ms: Optional[int]
    mp3_path: Optional[str]


@dataclass(frozen=True)
class RepairPlan:
    """Explicit inputs needed to repair and repackage a stored conversion."""

    source_path: Path
    stored_chapters: list[Any]
    title: str
    author: Optional[str]
    output_dir: Path
    chapters_dir: Path
    resolve_chapter_audio: Callable[[Any], Optional[Path]]
    cancel_requested: Callable[[], bool] | None = None


@dataclass
class RepairResult(ConversionResult):
    """Typed repair result with chapter metadata updates."""

    repaired_chapter_updates: list[RepairedChapterUpdate] = field(default_factory=list)


ProgressCallback = Callable[
    [PipelineStage, int, int, int, int, str, Optional[float], Optional[dict[str, Any]]],
    None,
]
VerboseCallback = Callable[[str, str], None]
ChapterProgressCallback = Callable[[int, int, str], None]
