"""Immutable configuration and report state for local release qualification."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping
import tomllib


REQUIRED_PHASES: tuple[str, ...] = (
    "preflight",
    "source_validation",
    "sidecar_validation",
    "bundle_validation",
    "lifecycle_performance",
    "fault_persistence",
    "computer_use",
    "evidence_finalization",
)

_RELEASE_KEYS = frozenset(
    {
        "product_name",
        "bundle_id",
        "minimum_macos",
        "architecture",
        "runtime_profiles",
    }
)
_BUDGET_KEYS = frozenset(
    {
        "cold_launch_seconds",
        "warm_launch_seconds",
        "window_close_seconds",
        "process_exit_seconds",
        "idle_cpu_median_percent",
        "idle_cpu_p95_percent",
        "soak_memory_growth_mib",
        "soak_memory_growth_percent",
    }
)
_TOP_LEVEL_KEYS = frozenset({"release", "budgets"})


class PhaseState(str, Enum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    INCOMPLETE = "incomplete"


class ReleaseState(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    INCOMPLETE = "incomplete"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_version_tuple(text: str, *, error_cls: type[Exception]) -> tuple[int, ...]:
    """Parse the leading numeric components of a dotted version string.

    Callers own the failure type, so a preflight gate and an artifact
    inspection keep raising their own error classes.
    """
    parts: list[int] = []
    for chunk in str(text).strip().split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    if not parts:
        raise error_cls(f"unable to parse version: {text!r}")
    return tuple(parts)


@dataclass(frozen=True)
class PerformanceBudgets:
    cold_launch_seconds: float
    warm_launch_seconds: float
    window_close_seconds: float
    process_exit_seconds: float
    idle_cpu_median_percent: float
    idle_cpu_p95_percent: float
    soak_memory_growth_mib: float
    soak_memory_growth_percent: float

    def __post_init__(self) -> None:
        numeric_fields = (
            self.cold_launch_seconds,
            self.warm_launch_seconds,
            self.window_close_seconds,
            self.process_exit_seconds,
            self.idle_cpu_median_percent,
            self.idle_cpu_p95_percent,
            self.soak_memory_growth_mib,
            self.soak_memory_growth_percent,
        )
        if any(value <= 0 for value in numeric_fields):
            raise ValueError("performance budgets must be positive")
        if self.warm_launch_seconds >= self.cold_launch_seconds:
            raise ValueError("warm_launch_seconds must be less than cold_launch_seconds")
        if self.window_close_seconds >= self.process_exit_seconds:
            raise ValueError("window_close_seconds must be less than process_exit_seconds")
        if self.idle_cpu_median_percent >= self.idle_cpu_p95_percent:
            raise ValueError(
                "idle_cpu_median_percent must be less than idle_cpu_p95_percent"
            )


@dataclass(frozen=True)
class QualificationConfig:
    product_name: str
    bundle_id: str
    minimum_macos: str
    architecture: str
    runtime_profiles: tuple[str, ...]
    budgets: PerformanceBudgets

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> QualificationConfig:
        unknown_top = set(data) - _TOP_LEVEL_KEYS
        if unknown_top:
            raise ValueError(f"unknown configuration sections: {sorted(unknown_top)}")
        if "release" not in data or "budgets" not in data:
            raise ValueError("configuration requires [release] and [budgets] sections")

        release = dict(data["release"])
        budgets = dict(data["budgets"])

        unknown_release = set(release) - _RELEASE_KEYS
        if unknown_release:
            raise ValueError(f"unknown release keys: {sorted(unknown_release)}")
        unknown_budgets = set(budgets) - _BUDGET_KEYS
        if unknown_budgets:
            raise ValueError(f"unknown budget keys: {sorted(unknown_budgets)}")
        missing_release = _RELEASE_KEYS - set(release)
        if missing_release:
            raise ValueError(f"missing release keys: {sorted(missing_release)}")
        missing_budgets = _BUDGET_KEYS - set(budgets)
        if missing_budgets:
            raise ValueError(f"missing budget keys: {sorted(missing_budgets)}")

        profiles = release["runtime_profiles"]
        if not isinstance(profiles, list) or not profiles:
            raise ValueError("runtime_profiles must be a non-empty list")

        return cls(
            product_name=str(release["product_name"]),
            bundle_id=str(release["bundle_id"]),
            minimum_macos=str(release["minimum_macos"]),
            architecture=str(release["architecture"]),
            runtime_profiles=tuple(str(item) for item in profiles),
            budgets=PerformanceBudgets(
                cold_launch_seconds=float(budgets["cold_launch_seconds"]),
                warm_launch_seconds=float(budgets["warm_launch_seconds"]),
                window_close_seconds=float(budgets["window_close_seconds"]),
                process_exit_seconds=float(budgets["process_exit_seconds"]),
                idle_cpu_median_percent=float(budgets["idle_cpu_median_percent"]),
                idle_cpu_p95_percent=float(budgets["idle_cpu_p95_percent"]),
                soak_memory_growth_mib=float(budgets["soak_memory_growth_mib"]),
                soak_memory_growth_percent=float(budgets["soak_memory_growth_percent"]),
            ),
        )

    def require_version(self, version: str) -> str:
        cleaned = version.strip()
        if not cleaned:
            raise ValueError("version must be non-empty")
        return cleaned


def load_qualification_config(path: Path | str) -> QualificationConfig:
    config_path = Path(path)
    with config_path.open("rb") as handle:
        data = tomllib.load(handle)
    return QualificationConfig.from_mapping(data)


@dataclass(frozen=True)
class PhaseRecord:
    name: str
    state: PhaseState
    detail: str
    updated_at: str


@dataclass
class QualificationReport:
    release_id: str
    commit: str
    created_at: str
    required_phases: tuple[str, ...]
    phases: dict[str, PhaseRecord] = field(default_factory=dict)

    @classmethod
    def new(cls, *, release_id: str, commit: str) -> QualificationReport:
        created_at = _utc_now_iso()
        phases = {
            name: PhaseRecord(
                name=name,
                state=PhaseState.NOT_STARTED,
                detail="",
                updated_at=created_at,
            )
            for name in REQUIRED_PHASES
        }
        return cls(
            release_id=release_id,
            commit=commit,
            created_at=created_at,
            required_phases=REQUIRED_PHASES,
            phases=phases,
        )

    def set_phase(self, name: str, state: PhaseState, detail: str) -> None:
        if name not in self.required_phases:
            raise ValueError(f"unknown phase: {name}")
        self.phases[name] = PhaseRecord(
            name=name,
            state=state,
            detail=detail,
            updated_at=_utc_now_iso(),
        )

    @property
    def release_state(self) -> ReleaseState:
        states = [self.phases[name].state for name in self.required_phases]
        if any(state is PhaseState.FAILED for state in states):
            return ReleaseState.FAILED
        if all(state is PhaseState.PASSED for state in states):
            return ReleaseState.PASSED
        return ReleaseState.INCOMPLETE
