"""Typed runtime context for pipeline stage collaborators."""

from __future__ import annotations

import time
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .models import PipelineStage


class PipelineRuntimeProtocol(Protocol):
    """Boundary exposed to stage helpers instead of the raw pipeline object."""

    @property
    def config(self) -> Any: ...

    @property
    def state(self) -> Any: ...

    @property
    def is_running(self) -> bool: ...

    @property
    def is_parallel(self) -> bool: ...

    @property
    def tts_engine(self) -> Any | None: ...

    def check_cancelled(self) -> bool: ...

    def notify_progress(self, **kwargs: Any) -> None: ...

    def log_verbose(self, message: str, log_type: str = "info") -> None: ...

    def set_stage(self, stage: PipelineStage) -> None: ...

    def is_tts_engine_loaded(self) -> bool: ...

    def tts_sample_rate(self, default: int = 24000) -> int: ...

    def supports_file_batch_results(self) -> bool: ...

    def synthesize_speech(self, *, text: str, voice: str, speed: float) -> Any: ...

    def synthesize_batch(self, *, items: list[Any], progress_callback: Any) -> list[Any]: ...

    def add_processed_chars(self, chars: int) -> None: ...

    def set_tts_engine(self, tts_engine: Any) -> None: ...

    def set_text_cleaner(self, text_cleaner: Any) -> None: ...

    def set_parallel_execution_active(self, active: bool) -> None: ...

    def parallel_execution_active(self) -> bool: ...

    def selected_parallel_chapters(self, fallback: int = 1) -> int: ...

    def set_selected_parallel_chapters(self, count: int) -> None: ...

    def set_selected_parallel_worker_count_reason(self, reason: str | None) -> None: ...

    def track_temp_cleanup_path(self, path: Path) -> None: ...

    def performance_snapshot(
        self,
        performance: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def begin_active_chapter(self, chapter_idx: int, title: str) -> None: ...

    def finish_active_chapter(self, chapter_idx: int, *, failed: bool) -> None: ...

    def record_tts_throughput(self, chars: int, elapsed_seconds: float) -> None: ...

    def record_encode_throughput(self, elapsed_seconds: float) -> None: ...

    def parallel_config_max_workers(self, fallback: int = 1) -> int: ...

    def shared_pool_fingerprint(self) -> tuple[Any, ...]: ...

    def build_worker_init_config(self) -> dict[str, Any]: ...

    def memory_policy_reason(self) -> str | None: ...


@dataclass(slots=True)
class PipelineRunState:
    """Mutable execution state for one conversion pipeline instance."""

    stage: PipelineStage = PipelineStage.IDLE
    cancelled: bool = False
    start_time: float | None = None
    chars_processed: int = 0
    total_chars: int = 0
    tts_engine: Any | None = None
    text_cleaner: Any | None = None
    tts_idle_timer: threading.Timer | None = None
    tts_idle_lock: threading.RLock = field(default_factory=threading.RLock)
    parallel_execution_active: bool = False
    selected_parallel_chapters: int = 1
    selected_parallel_worker_count_reason: str | None = None
    performance_active_chapters: dict[int, str] = field(default_factory=dict)
    performance_completed_chapters: int = 0
    performance_failed_chapters: int = 0
    performance_tts_chars: int = 0
    performance_tts_seconds: float = 0.0
    performance_encode_chapters: int = 0
    performance_encode_seconds: float = 0.0
    temp_cleanup_paths: set[Path] = field(default_factory=set)
    chapter_aac_executor: ThreadPoolExecutor | None = None

    def reset_for_conversion(self, *, selected_parallel_chapters: int) -> None:
        if self.chapter_aac_executor is not None:
            self.chapter_aac_executor.shutdown(wait=True)
            self.chapter_aac_executor = None
        self.start_time = time.time()
        self.chars_processed = 0
        self.total_chars = 0
        self.parallel_execution_active = False
        self.temp_cleanup_paths.clear()
        self.selected_parallel_chapters = max(1, int(selected_parallel_chapters or 1))
        self.selected_parallel_worker_count_reason = None
        self.performance_active_chapters.clear()
        self.performance_completed_chapters = 0
        self.performance_failed_chapters = 0
        self.performance_tts_chars = 0
        self.performance_tts_seconds = 0.0
        self.performance_encode_chapters = 0
        self.performance_encode_seconds = 0.0


@dataclass(slots=True)
class PipelineRuntime:
    """Narrow execution context shared by pipeline stage collaborators."""

    config: Any
    state: PipelineRunState
    progress: Any
    get_progress_callback: Callable[[], Any]
    get_verbose_callback: Callable[[], Any]
    is_parallel_enabled: Callable[[], bool]
    parallel_config_provider: Callable[[], Any]
    shared_pool_fingerprint_builder: Callable[[], tuple[Any, ...]]
    worker_init_config_builder: Callable[[], dict[str, Any]]

    @property
    def parallel_config(self) -> Any:
        return self.parallel_config_provider()

    @property
    def is_running(self) -> bool:
        return self.state.stage not in (
            PipelineStage.IDLE,
            PipelineStage.COMPLETE,
            PipelineStage.ERROR,
            PipelineStage.CANCELLED,
        )

    @property
    def is_parallel(self) -> bool:
        return bool(self.is_parallel_enabled())

    @property
    def tts_engine(self) -> Any | None:
        return self.state.tts_engine

    @property
    def text_cleaner(self) -> Any | None:
        return self.state.text_cleaner

    def check_cancelled(self) -> bool:
        if self.state.cancelled:
            self.state.stage = PipelineStage.CANCELLED
            return True
        return False

    def notify_progress(self, **kwargs: Any) -> None:
        performance = kwargs.pop("performance", None)
        self.progress.notify(
            callback=self.get_progress_callback(),
            stage=self.state.stage,
            performance=self.performance_snapshot(performance),
            **kwargs,
        )

    def log_verbose(self, message: str, log_type: str = "info") -> None:
        callback = self.get_verbose_callback()
        if callback:
            callback(message, log_type)

    def set_stage(self, stage: PipelineStage) -> None:
        self.state.stage = stage

    def is_tts_engine_loaded(self) -> bool:
        tts_engine = self.tts_engine
        return tts_engine is not None and bool(getattr(tts_engine, "is_loaded", False))

    def set_parallel_execution_active(self, active: bool) -> None:
        self.state.parallel_execution_active = bool(active)

    def parallel_execution_active(self) -> bool:
        return bool(self.state.parallel_execution_active)

    def selected_parallel_chapters(self, fallback: int = 1) -> int:
        return max(1, int(self.state.selected_parallel_chapters or fallback or 1))

    def set_selected_parallel_chapters(self, count: int) -> None:
        self.state.selected_parallel_chapters = max(1, int(count or 1))

    def set_selected_parallel_worker_count_reason(self, reason: str | None) -> None:
        self.state.selected_parallel_worker_count_reason = reason

    def track_temp_cleanup_path(self, path: Path) -> None:
        try:
            resolved = Path(path)
            resolved.relative_to(self.config.temp_dir)
        except (TypeError, ValueError):
            return
        if resolved.parent.name == "chapter_masters" or resolved.parent.name.startswith(
            "ch_"
        ):
            resolved = resolved.parent
        self.state.temp_cleanup_paths.add(resolved)

    def tts_sample_rate(self, default: int = 24000) -> int:
        return int(getattr(self.tts_engine, "sample_rate", default) or default)

    def supports_file_batch_results(self) -> bool:
        return bool(getattr(self.tts_engine, "supports_file_batch_results", False))

    def synthesize_speech(self, *, text: str, voice: str, speed: float) -> Any:
        if self.tts_engine is None:
            raise RuntimeError("TTS engine is not initialized")
        return self.tts_engine.synthesize(text=text, voice=voice, speed=speed)

    def synthesize_batch(self, *, items: list[Any], progress_callback: Any) -> list[Any]:
        if self.tts_engine is None:
            raise RuntimeError("TTS engine is not initialized")
        return self.tts_engine.synthesize_batch(
            items=items,
            progress_callback=progress_callback,
        )

    def add_processed_chars(self, chars: int) -> None:
        self.progress.add_processed_chars(chars)

    def set_tts_engine(self, tts_engine: Any) -> None:
        self.state.tts_engine = tts_engine

    def set_text_cleaner(self, text_cleaner: Any) -> None:
        self.state.text_cleaner = text_cleaner

    def begin_active_chapter(self, chapter_idx: int, title: str) -> None:
        self.state.performance_active_chapters[chapter_idx] = title

    def finish_active_chapter(self, chapter_idx: int, *, failed: bool) -> None:
        self.state.performance_active_chapters.pop(chapter_idx, None)
        if failed:
            self.state.performance_failed_chapters += 1
        else:
            self.state.performance_completed_chapters += 1

    def record_tts_throughput(self, chars: int, elapsed_seconds: float) -> None:
        if elapsed_seconds <= 0 or chars <= 0:
            return
        self.state.performance_tts_chars += int(chars)
        self.state.performance_tts_seconds += float(elapsed_seconds)

    def record_encode_throughput(self, elapsed_seconds: float) -> None:
        if elapsed_seconds <= 0:
            return
        self.state.performance_encode_chapters += 1
        self.state.performance_encode_seconds += float(elapsed_seconds)

    def parallel_config_max_workers(self, fallback: int = 1) -> int:
        parallel_config = self.parallel_config
        if parallel_config is None:
            return max(1, int(fallback or 1))
        return max(1, int(getattr(parallel_config, "max_workers", fallback) or 1))

    def shared_pool_fingerprint(self) -> tuple[Any, ...]:
        return self.shared_pool_fingerprint_builder()

    def build_worker_init_config(self) -> dict[str, Any]:
        return self.worker_init_config_builder()

    def memory_policy_reason(self) -> str | None:
        memory_policy = self.config.memory_policy
        if memory_policy is None:
            return None
        reason = getattr(memory_policy, "safe_mode_reason", None)
        manual_worker_mode = self.config.worker_plan.worker_mode == "manual"
        if manual_worker_mode and reason != "memory pressure error":
            return None
        return str(reason) if reason else None

    def performance_snapshot(
        self,
        performance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot = dict(performance or {})
        snapshot.setdefault(
            "pipeline_mode",
            "parallel" if self.state.parallel_execution_active else "sequential",
        )
        configured_workers = self.parallel_config_max_workers(
            self.config.worker_plan.max_workers
        )
        selected_workers = self.selected_parallel_chapters(configured_workers)
        snapshot.setdefault("max_workers", configured_workers)
        snapshot.setdefault(
            "requested_parallel_chapters",
            self.config.worker_plan.requested_workers,
        )
        snapshot.setdefault("effective_parallel_chapters", selected_workers)
        snapshot.setdefault("parallel_worker_mode", self.config.worker_plan.worker_mode)
        safe_mode_reason = self.memory_policy_reason()
        if safe_mode_reason:
            snapshot.setdefault("memory_safe_mode", True)
            snapshot.setdefault("safe_mode_reason", safe_mode_reason)
        else:
            snapshot.setdefault("memory_safe_mode", False)

        memory_policy = self.config.memory_policy
        if memory_policy is not None:
            snapshot.setdefault(
                "warmup_memory_delta_bytes",
                getattr(memory_policy, "measured_warmup_delta_bytes", None),
            )
            snapshot.setdefault(
                "estimated_per_worker_memory_bytes",
                getattr(memory_policy, "estimated_per_worker_memory_bytes", None),
            )
            snapshot.setdefault(
                "selected_worker_count_reason",
                self.state.selected_parallel_worker_count_reason
                or getattr(memory_policy, "selected_worker_count_reason", None),
            )

        snapshot.setdefault(
            "completed_chapters",
            self.state.performance_completed_chapters,
        )
        snapshot.setdefault("failed_chapters", self.state.performance_failed_chapters)
        active_titles = list(self.state.performance_active_chapters.values())
        snapshot.setdefault("active_chapter_titles", active_titles)
        snapshot.setdefault("running_chapters", len(active_titles))
        snapshot.setdefault("active_workers", min(selected_workers, len(active_titles)))

        if self.state.start_time is not None:
            elapsed_seconds = max(0.0, time.time() - self.state.start_time)
            snapshot.setdefault("elapsed_seconds", elapsed_seconds)
            completed_total = max(0, self.state.performance_completed_chapters)
            if elapsed_seconds > 0 and completed_total > 0:
                snapshot.setdefault(
                    "encoder_chapters_per_second",
                    completed_total / elapsed_seconds,
                )
        if (
            self.state.performance_encode_seconds > 0
            and self.state.performance_encode_chapters > 0
        ):
            snapshot["encoder_chapters_per_second"] = (
                self.state.performance_encode_chapters
                / self.state.performance_encode_seconds
            )

        eta_seconds = self.progress.estimate_eta()
        if eta_seconds is not None:
            snapshot.setdefault("eta_seconds", eta_seconds)

        cleaner = self.state.text_cleaner
        if cleaner is not None:
            try:
                stats = cleaner.stats_snapshot()
                elapsed_seconds = max(1e-6, time.time() - (self.state.start_time or time.time()))
                snapshot.setdefault(
                    "cleaner_chunks_per_second",
                    int(stats.get("total_chunks", 0)) / elapsed_seconds,
                )
            except Exception:
                pass

        get_metrics = getattr(self.state.tts_engine, "get_metrics", None)
        if callable(get_metrics):
            try:
                metrics = get_metrics()
                chars_per_second = metrics.get("chars_per_second")
                if chars_per_second is not None:
                    snapshot.setdefault("tts_chars_per_second", chars_per_second)
            except Exception:
                pass
        if self.state.performance_tts_seconds > 0 and self.state.performance_tts_chars > 0:
            snapshot["tts_chars_per_second"] = (
                self.state.performance_tts_chars / self.state.performance_tts_seconds
            )

        return snapshot
