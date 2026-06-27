from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orpheus.bench.policy import policy_bench_fixture_paths

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "benchmark_suite"
SUPPORTED_PRESETS = ("quick", "policy")


@dataclass(frozen=True)
class PresetSpec:
    name: str
    modes: tuple[str, ...]
    sources: tuple[Path, ...]
    defaults: dict[str, Any]


def _fixture_sources(*names: str) -> tuple[Path, ...]:
    return tuple(FIXTURE_ROOT / name for name in names)


PRESETS: dict[str, PresetSpec] = {
    "quick": PresetSpec(
        name="quick",
        modes=("parse", "clean"),
        sources=_fixture_sources("short_chapter.md", "footnotes_stub.md"),
        defaults={"chapter_limit": 1},
    ),
    "policy": PresetSpec(
        name="policy",
        modes=("parse", "clean"),
        sources=tuple(policy_bench_fixture_paths()),
        defaults={"min_extracted_chars": 1},
    ),
}


def get_preset(name: str) -> PresetSpec:
    if name not in PRESETS:
        supported = ", ".join(SUPPORTED_PRESETS)
        raise ValueError(f"unsupported preset: {name}; expected one of {supported}")
    return PRESETS[name]
