"""
Pipeline Module
================
Conversion pipeline orchestration for audiobook creation.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {
    # Core pipeline
    "ConversionPipeline": ("modules.pipeline.orchestrator", "ConversionPipeline"),
    "PipelineStage": ("modules.pipeline.models", "PipelineStage"),
    "PipelineConfig": ("modules.pipeline.models", "PipelineConfig"),
    "ConversionResult": ("modules.pipeline.models", "ConversionResult"),
    "ChapterResult": ("modules.pipeline.models", "ChapterResult"),
    "RepairedChapterUpdate": (
        "modules.pipeline.models",
        "RepairedChapterUpdate",
    ),
    "RepairPlan": ("modules.pipeline.models", "RepairPlan"),
    "RepairResult": ("modules.pipeline.models", "RepairResult"),
    "worker_recommendation": (
        "modules.pipeline.worker_recommendation",
        "worker_recommendation",
    ),
    # Parallel processing components
    "ParallelConfig": ("modules.pipeline.parallel", "ParallelConfig"),
    "VRAMBudget": ("modules.pipeline.parallel", "VRAMBudget"),
    "get_optimal_worker_count": (
        "modules.pipeline.parallel",
        "get_optimal_worker_count",
    ),
    "create_parallel_config": ("modules.pipeline.parallel", "create_parallel_config"),
    "ParallelPipelineOrchestrator": (
        "modules.pipeline.parallel",
        "ParallelPipelineOrchestrator",
    ),
    "SharedWorkerPoolManager": (
        "modules.pipeline.pool_manager",
        "SharedWorkerPoolManager",
    ),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute_name = _EXPORTS[name]
    if name == "worker_recommendation":
        value = import_module(module_name)
    else:
        value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value
