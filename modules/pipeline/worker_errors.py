"""Worker lifecycle errors and startup probe status."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Optional


class StartupErrorKind:
    """String-compatible startup error classifications for serialization."""

    RUNTIME_LOAD = "WorkerRuntimeLoadError"
    RUNTIME_MEMORY = "WorkerRuntimeMemoryError"
    RUNTIME_UNAVAILABLE = "WorkerRuntimeUnavailableError"
    INFRASTRUCTURE = "WorkerInfrastructureError"


class StartupFailureAction(Enum):
    """Policy action selected from a worker startup failure."""

    FALLBACK_MEMORY = "fallback_memory"
    FAIL_FAST_MEMORY = "fail_fast_memory"
    FAIL_FAST_UNAVAILABLE = "fail_fast_unavailable"
    FALLBACK_INFRASTRUCTURE = "fallback_infrastructure"
    FAIL_FAST_LOAD = "fail_fast_load"
    RE_RAISE = "re_raise"


RUNTIME_UNAVAILABLE_MARKERS = (
    "runtime unavailable",
    "not registered",
    "not installed",
    "import error",
    "no module named",
    "cuda driver",
    "no cuda",
    "cudnn",
    "onnxruntime",
    "device not found",
    "cannot access gpu",
    "gpu device",
)


@dataclass(frozen=True)
class WorkerStartupStatus:
    """Serializable result from a worker startup probe."""

    success: bool
    provider: str
    device: Optional[str]
    error_type: Optional[str]
    message: Optional[str]
    phase: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkerStartupStatus:
        return cls(
            success=bool(data.get("success")),
            provider=str(data.get("provider") or "unknown"),
            device=data.get("device"),
            error_type=data.get("error_type"),
            message=data.get("message"),
            phase=data.get("phase"),
        )

    def failure_label(self) -> str:
        label = self.provider or "unknown"
        if self.device:
            label = f"{label}/{self.device}"
        return label

    def _labeled_meta(self) -> str:
        label = self.failure_label()
        meta = self.error_type or self.phase
        if meta:
            return f"{label} [{meta}]"
        return label

    def format_failure(
        self,
        *,
        fallback_message: str = "worker startup probe failed",
        include_meta: bool = True,
    ) -> str:
        """Canonical startup failure text for snapshots, logs, and exceptions."""
        if include_meta:
            return f"{self._labeled_meta()}: {self.message or fallback_message}"
        return f"{self.failure_label()}: {self.message or fallback_message}"

    def format_failure_detail(self, detail: str) -> str:
        """Progress-style startup failure text: ``'{detail}: {label} [{meta}]'``."""
        return f"{detail}: {self._labeled_meta()}"


def _canonical_provider(provider: str | None) -> str:
    from modules.tts.runtime_spec import canonical_provider

    return canonical_provider(provider) or "unknown"


def is_runtime_unavailable_error(error: str) -> bool:
    """Return True when an error indicates provider/device/runtime unavailability."""
    lowered = str(error or "").lower()
    return any(marker in lowered for marker in RUNTIME_UNAVAILABLE_MARKERS)


def is_infrastructure_startup_status(status: WorkerStartupStatus) -> bool:
    """Return True when startup failure looks process-pool infrastructure related."""
    from .retry import is_infrastructure_runtime_error

    return is_infrastructure_runtime_error(status.message or "")


def classify_worker_startup_error(
    exc: BaseException,
    *,
    provider: str,
    device: Optional[str],
    phase: str,
) -> WorkerStartupStatus:
    """Classify a worker initialization failure into a serializable startup status."""
    from modules.tts.runtimes.errors import (
        TTSRuntimeMemoryError,
        TTSRuntimeUnavailable,
    )

    from .retry import is_infrastructure_runtime_error, is_memory_pressure_error

    message = str(exc)
    error_type = StartupErrorKind.RUNTIME_LOAD

    if isinstance(
        exc,
        (
            WorkerRuntimeMemoryError,
            TTSRuntimeMemoryError,
        ),
    ):
        error_type = StartupErrorKind.RUNTIME_MEMORY
    elif isinstance(
        exc,
        (
            WorkerRuntimeUnavailableError,
            TTSRuntimeUnavailable,
        ),
    ):
        error_type = StartupErrorKind.RUNTIME_UNAVAILABLE
    elif is_memory_pressure_error(message):
        error_type = StartupErrorKind.RUNTIME_MEMORY
    elif is_runtime_unavailable_error(message):
        error_type = StartupErrorKind.RUNTIME_UNAVAILABLE
    elif is_infrastructure_runtime_error(message):
        error_type = StartupErrorKind.INFRASTRUCTURE

    return WorkerStartupStatus(
        success=False,
        provider=_canonical_provider(provider),
        device=device,
        error_type=error_type,
        message=message,
        phase=phase,
    )


def is_warmup_failure_non_fatal(
    exc: BaseException,
    *,
    provider: str,
) -> bool:
    """
    Return True when a TTS warmup failure should not fail worker startup.

    CUDA and memory/driver/device failures are always fatal.
    """
    if _canonical_provider(provider) == "cuda":
        return False

    status = classify_worker_startup_error(
        exc,
        provider=provider,
        device=None,
        phase="tts_warmup",
    )
    return status.error_type not in {
        StartupErrorKind.RUNTIME_MEMORY,
        StartupErrorKind.RUNTIME_UNAVAILABLE,
        StartupErrorKind.INFRASTRUCTURE,
    }


def resolve_startup_failure_action(
    status: Optional[WorkerStartupStatus],
    *,
    memory_degrade_allowed: bool,
) -> StartupFailureAction:
    """Select the startup failure policy action for a probe status."""
    if status is None:
        return StartupFailureAction.RE_RAISE

    if status.error_type == StartupErrorKind.RUNTIME_MEMORY:
        if memory_degrade_allowed:
            return StartupFailureAction.FALLBACK_MEMORY
        return StartupFailureAction.FAIL_FAST_MEMORY

    if status.error_type == StartupErrorKind.RUNTIME_UNAVAILABLE:
        return StartupFailureAction.FAIL_FAST_UNAVAILABLE

    if (
        status.error_type == StartupErrorKind.INFRASTRUCTURE
        or is_infrastructure_startup_status(status)
    ):
        return StartupFailureAction.FALLBACK_INFRASTRUCTURE

    if status.error_type == StartupErrorKind.RUNTIME_LOAD:
        return StartupFailureAction.FAIL_FAST_LOAD

    return StartupFailureAction.RE_RAISE


def startup_status_from_failure(
    status: WorkerStartupStatus,
) -> WorkerStartupStatus:
    """Normalize a stored failure status for startup probes."""
    if status.success:
        return status
    if status.error_type:
        return status
    return classify_worker_startup_error(
        RuntimeError(status.message or "worker initialization failed"),
        provider=status.provider,
        device=status.device,
        phase=status.phase or "worker_init",
    )


class WorkerStartupError(Exception):
    """Raised when one or more workers fail startup validation."""

    def __init__(
        self,
        message: str,
        *,
        statuses: Optional[list[WorkerStartupStatus]] = None,
    ) -> None:
        super().__init__(message)
        self.statuses = list(statuses or [])

    @property
    def primary_status(self) -> Optional[WorkerStartupStatus]:
        return self.statuses[0] if self.statuses else None


class WorkerRuntimeLoadError(Exception):
    """Raised when a worker cannot load its runtime or models."""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[WorkerStartupStatus] = None,
    ) -> None:
        super().__init__(message)
        self.status = status


class WorkerRuntimeMemoryError(Exception):
    """Raised when a worker fails due to insufficient device memory."""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[WorkerStartupStatus] = None,
    ) -> None:
        super().__init__(message)
        self.status = status


class WorkerRuntimeUnavailableError(Exception):
    """Raised when the selected provider/device/runtime is unavailable."""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[WorkerStartupStatus] = None,
    ) -> None:
        super().__init__(message)
        self.status = status


class WorkerCancelledError(Exception):
    """Raised when parallel worker execution is cancelled."""
