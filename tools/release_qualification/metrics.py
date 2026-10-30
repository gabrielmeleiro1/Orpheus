"""Lifecycle and performance budget evaluation for release qualification."""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .macos_processes import (
    ProcessSample,
    attribute_orpheus_tree,
    sample_processes,
)
from .model import PerformanceBudgets


@dataclass(frozen=True)
class BudgetDecision:
    passed: bool
    detail: str
    metrics: dict[str, object] = field(default_factory=dict)


def percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        raise ValueError("values must be non-empty")
    if pct < 0 or pct > 100:
        raise ValueError("percentile must be in [0, 100]")
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = math.ceil((pct / 100.0) * len(ordered)) - 1
    rank = min(max(rank, 0), len(ordered) - 1)
    return ordered[rank]


def median(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("values must be non-empty")
    ordered = sorted(float(value) for value in values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def evaluate_idle_cpu(
    cpu_percent_series: Sequence[float],
    *,
    budgets: PerformanceBudgets,
) -> BudgetDecision:
    med = median(cpu_percent_series)
    p95 = percentile(cpu_percent_series, 95)
    passed = med <= budgets.idle_cpu_median_percent and p95 <= budgets.idle_cpu_p95_percent
    detail = "ok" if passed else "idle CPU budget exceeded"
    return BudgetDecision(
        passed=passed,
        detail=detail,
        metrics={
            "cpu_median_percent": med,
            "cpu_p95_percent": p95,
            "sample_count": len(cpu_percent_series),
        },
    )


def evaluate_launch(
    *,
    budgets: PerformanceBudgets,
    mode: str,
    started_at: float,
    ready_at: float,
) -> BudgetDecision:
    elapsed = float(ready_at) - float(started_at)
    limit = budgets.warm_launch_seconds if mode == "warm" else budgets.cold_launch_seconds
    passed = elapsed <= limit
    return BudgetDecision(
        passed=passed,
        detail="ok" if passed else f"{mode} launch budget exceeded",
        metrics={
            "elapsed_seconds": elapsed,
            "budget_seconds": limit,
            "mode": mode,
        },
    )


def evaluate_shutdown(
    *,
    close_requested_at: float,
    window_closed_at: float | None,
    process_tree_exited_at: float | None,
    backend_alive: bool,
    remaining_pids: Sequence[int],
    budgets: PerformanceBudgets,
    monotonic_now: float | None = None,
) -> BudgetDecision:
    now = monotonic_now if monotonic_now is not None else time.monotonic()
    metrics: dict[str, object] = {
        "backend_alive": backend_alive,
        "remaining_pids": list(remaining_pids),
    }
    if window_closed_at is None:
        return BudgetDecision(
            passed=False,
            detail="window did not close",
            metrics=metrics,
        )
    window_close = float(window_closed_at) - float(close_requested_at)
    metrics["window_close_seconds"] = window_close
    if window_close > budgets.window_close_seconds:
        return BudgetDecision(
            passed=False,
            detail="window close budget exceeded",
            metrics=metrics,
        )
    if backend_alive or remaining_pids:
        metrics["observed_at"] = now
        return BudgetDecision(
            passed=False,
            detail="backend remained alive after window close",
            metrics=metrics,
        )
    if process_tree_exited_at is None:
        return BudgetDecision(
            passed=False,
            detail="process tree did not exit",
            metrics=metrics,
        )
    exit_elapsed = float(process_tree_exited_at) - float(close_requested_at)
    metrics["process_exit_seconds"] = exit_elapsed
    if exit_elapsed > budgets.process_exit_seconds:
        return BudgetDecision(
            passed=False,
            detail="process exit budget exceeded",
            metrics=metrics,
        )
    return BudgetDecision(passed=True, detail="ok", metrics=metrics)


def evaluate_soak_memory(
    rss_mib_series: Sequence[float],
    *,
    budgets: PerformanceBudgets,
    settle_samples: int = 10,
    startup_skip: int = 3,
) -> BudgetDecision:
    if len(rss_mib_series) < startup_skip + settle_samples:
        return BudgetDecision(
            passed=False,
            detail="insufficient soak samples",
            metrics={"sample_count": len(rss_mib_series)},
        )
    values = [float(value) for value in rss_mib_series]
    settled = values[startup_skip : startup_skip + settle_samples]
    baseline = median(settled)
    post = values[startup_skip + settle_samples :]
    if len(post) < 2:
        post = values[-settle_samples:]
    trend = _linear_endpoint(post)
    growth = trend - baseline
    allowed = max(
        float(budgets.soak_memory_growth_mib),
        baseline * (float(budgets.soak_memory_growth_percent) / 100.0),
    )
    passed = growth <= allowed
    return BudgetDecision(
        passed=passed,
        detail="ok" if passed else "soak memory growth exceeded",
        metrics={
            "baseline_rss_mib": baseline,
            "trend_rss_mib": trend,
            "growth_mib": growth,
            "allowed_growth_mib": allowed,
            "sample_count": len(rss_mib_series),
        },
    )


def _linear_endpoint(values: Sequence[float]) -> float:
    """Return the OLS-fitted value at the end of the series (trend, not a raw sample)."""
    n = len(values)
    if n == 1:
        return float(values[0])
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(float(value) for value in values) / n
    numerator = sum((x - mean_x) * (float(y) - mean_y) for x, y in zip(xs, values))
    denominator = sum((x - mean_x) ** 2 for x in xs) or 1.0
    slope = numerator / denominator
    intercept = mean_y - slope * mean_x
    return intercept + slope * (n - 1)


def write_jsonl_samples(
    path: Path,
    samples: Sequence[ProcessSample],
    *,
    extra: Mapping[str, object] | None = None,
    append: bool = False,
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    extras = dict(extra or {})
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8") as handle:
        for sample in samples:
            payload = {
                "pid": sample.pid,
                "ppid": sample.ppid,
                "pcpu": sample.pcpu,
                "rss_kib": sample.rss_kib,
                "comm": sample.comm,
                "args": sample.args,
                "start_time": sample.start_time,
                **extras,
            }
            handle.write(json.dumps(payload, sort_keys=True))
            handle.write("\n")


OBSERVER_SCENARIOS: tuple[str, ...] = (
    "warm-launch",
    "idle-dashboard",
    "shutdown",
    "soak",
)


@dataclass
class ObserverSession:
    scenario: str
    report_dir: Path
    budgets: PerformanceBudgets
    app_bundle: Path
    runtime_root: Path
    monotonic_clock: Callable[[], float] = time.monotonic
    sampler: Callable[[], list[ProcessSample]] = sample_processes
    sleep: Callable[[float], None] = time.sleep
    sample_interval_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.scenario not in OBSERVER_SCENARIOS:
            raise ValueError(f"unsupported observer scenario: {self.scenario}")
        self.report_dir = Path(self.report_dir)
        self.metrics_dir = self.report_dir / "metrics"
        self.metrics_dir.mkdir(parents=True, exist_ok=True)

    def _shutdown_sample_interval(self) -> float:
        """Poll shutdown transitions finer than window_close; keep idle/soak cadence.

        A 1.0s sleep plus ~0.15s ps overhead cannot resolve a 1.0s window-close
        budget. Derive a materially finer interval from the budget itself.
        """
        return min(
            self.sample_interval_seconds,
            self.budgets.window_close_seconds / 10.0,
        )

    def ready_marker(self) -> str:
        return f"ORPHEUS_OBSERVE_READY scenario={self.scenario}"

    def run(
        self,
        *,
        seconds: float | None = None,
        minutes: float | None = None,
        action_started_at: float | None = None,
        ui_ready_at: float | None = None,
    ) -> BudgetDecision:
        out = self.metrics_dir / f"{self.scenario}.jsonl"
        if out.exists():
            out.unlink()

        if self.scenario == "warm-launch":
            if action_started_at is None or ui_ready_at is None:
                raise ValueError("warm-launch requires action_started_at and ui_ready_at handshake")
            samples = self.sampler()
            tree = attribute_orpheus_tree(
                samples,
                app_bundle=self.app_bundle,
                runtime_root=self.runtime_root,
            )
            write_jsonl_samples(
                out,
                tree,
                extra={
                    "scenario": self.scenario,
                    "monotonic_ts": self.monotonic_clock(),
                    "phase": "post-ready",
                },
                append=True,
            )
            return evaluate_launch(
                budgets=self.budgets,
                mode="warm",
                started_at=action_started_at,
                ready_at=ui_ready_at,
            )

        started = self.monotonic_clock()
        if self.scenario == "idle-dashboard":
            duration = float(seconds if seconds is not None else 60.0)
            series = self._sample_series(
                out,
                duration_seconds=duration,
                value_fn=lambda tree: sum(proc.pcpu for proc in tree),
            )
            return evaluate_idle_cpu(series, budgets=self.budgets)

        if self.scenario == "shutdown":
            return self._observe_shutdown(
                out,
                observe_started_at=started,
                seconds=seconds,
                close_requested_at=action_started_at,
            )

        duration_minutes = float(minutes if minutes is not None else 30.0)
        rss_series = self._sample_series(
            out,
            duration_seconds=duration_minutes * 60.0,
            value_fn=lambda tree: sum(proc.rss_kib for proc in tree) / 1024.0,
        )
        return evaluate_soak_memory(rss_series, budgets=self.budgets)

    def _observe_shutdown(
        self,
        out: Path,
        *,
        observe_started_at: float,
        seconds: float | None,
        close_requested_at: float | None,
    ) -> BudgetDecision:
        """Stay active after the ready marker; measure an externally initiated quit."""
        timeout_seconds = float(
            seconds if seconds is not None else self.budgets.process_exit_seconds
        )
        wait_deadline = observe_started_at + timeout_seconds

        window_closed_at: float | None = None
        process_tree_exited_at: float | None = None
        measured_close_requested_at = close_requested_at
        last_ui_alive_at = observe_started_at
        sample_index = 0
        last_tree: list[ProcessSample] = []
        saw_live_tree = False

        while True:
            now = self.monotonic_clock()
            samples = self.sampler()
            tree = attribute_orpheus_tree(
                samples,
                app_bundle=self.app_bundle,
                runtime_root=self.runtime_root,
            )
            last_tree = tree
            write_jsonl_samples(
                out,
                tree,
                extra={
                    "scenario": self.scenario,
                    "monotonic_ts": now,
                    "sample_index": sample_index,
                    "phase": "shutdown-observe",
                },
                append=True,
            )
            sample_index += 1

            ui_procs = [proc for proc in tree if not _is_backend_process(proc)]

            if not saw_live_tree and not tree:
                return BudgetDecision(
                    passed=False,
                    detail="process tree already empty at observe start",
                    metrics={"remaining_pids": []},
                )

            if tree:
                saw_live_tree = True

            # Detect UI dismissal / full exit from the prior live baseline — never
            # invent window_closed_at at observe start while the tree is still alive.
            if saw_live_tree and not ui_procs and window_closed_at is None:
                window_closed_at = now
                if measured_close_requested_at is None:
                    measured_close_requested_at = last_ui_alive_at

            if saw_live_tree and not tree:
                process_tree_exited_at = now
                if window_closed_at is None:
                    window_closed_at = now
                if measured_close_requested_at is None:
                    measured_close_requested_at = last_ui_alive_at
                break

            if ui_procs:
                last_ui_alive_at = now

            if (
                window_closed_at is not None
                and measured_close_requested_at is not None
                and now - measured_close_requested_at > self.budgets.process_exit_seconds
            ):
                break

            if now >= wait_deadline and window_closed_at is None:
                return BudgetDecision(
                    passed=False,
                    detail="shutdown observe timeout waiting for external quit",
                    metrics={
                        "remaining_pids": [proc.pid for proc in tree],
                        "observe_seconds": now - observe_started_at,
                        "timeout_seconds": timeout_seconds,
                    },
                )

            if now >= wait_deadline and process_tree_exited_at is None:
                break

            remaining = wait_deadline - self.monotonic_clock()
            if remaining <= 0:
                break
            self.sleep(min(self._shutdown_sample_interval(), remaining))

        now = self.monotonic_clock()
        remaining_pids = tuple(proc.pid for proc in last_tree)
        backend_alive = any(_is_backend_process(proc) for proc in last_tree)
        if measured_close_requested_at is None:
            return BudgetDecision(
                passed=False,
                detail="shutdown observe timeout waiting for external quit",
                metrics={
                    "remaining_pids": list(remaining_pids),
                    "observe_seconds": now - observe_started_at,
                    "timeout_seconds": timeout_seconds,
                },
            )
        return evaluate_shutdown(
            close_requested_at=measured_close_requested_at,
            window_closed_at=window_closed_at,
            process_tree_exited_at=process_tree_exited_at,
            backend_alive=backend_alive,
            remaining_pids=remaining_pids,
            budgets=self.budgets,
            monotonic_now=now,
        )

    def _sample_series(
        self,
        out: Path,
        *,
        duration_seconds: float,
        value_fn: Callable[[list[ProcessSample]], float],
    ) -> list[float]:
        series: list[float] = []
        started = self.monotonic_clock()
        deadline = started + duration_seconds
        while True:
            now = self.monotonic_clock()
            if now >= deadline and series:
                break
            samples = self.sampler()
            tree = attribute_orpheus_tree(
                samples,
                app_bundle=self.app_bundle,
                runtime_root=self.runtime_root,
            )
            series.append(float(value_fn(tree)))
            write_jsonl_samples(
                out,
                tree,
                extra={
                    "scenario": self.scenario,
                    "monotonic_ts": now,
                    "sample_index": len(series) - 1,
                },
                append=True,
            )
            now = self.monotonic_clock()
            if now >= deadline:
                break
            remaining = deadline - now
            self.sleep(min(self.sample_interval_seconds, remaining))
        return series


def _is_backend_process(sample: ProcessSample) -> bool:
    text = f"{sample.args} {sample.comm}".lower()
    return "orpheus-desktop-backend" in text or "setup-helper" in text
