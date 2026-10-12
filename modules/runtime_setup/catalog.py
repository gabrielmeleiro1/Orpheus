"""Curated model groups and pinned snapshot revisions."""

from __future__ import annotations

import re
from typing import Iterable, Mapping

from modules.tts.runtime_spec import (
    CLEANER_CUDA_DEFAULT_MODEL,
    CLEANER_MLX_DEFAULT_MODEL,
    KOKORO_MLX_DEFAULT_MODEL_REPO,
    KOKORO_PACKAGE_DEFAULT_MODEL,
)


DEFAULT_MODEL_IDS = (KOKORO_PACKAGE_DEFAULT_MODEL,)
_REVISION_RE = re.compile(r"[0-9a-f]{40,64}")
MODEL_GROUPS: Mapping[str, tuple[str, ...]] = {
    "tts-mlx": (KOKORO_MLX_DEFAULT_MODEL_REPO,),
    "tts-cpu-cuda": (KOKORO_PACKAGE_DEFAULT_MODEL,),
    "cleaner-mlx": (CLEANER_MLX_DEFAULT_MODEL,),
    "cleaner-cuda": (CLEANER_CUDA_DEFAULT_MODEL,),
    "cleaner-cuda-large": ("Qwen/Qwen2.5-3B-Instruct",),
    "cleaner-mlx-small": ("mlx-community/Llama-3.2-1B-Instruct-4bit",),
}

# Pinned Hugging Face commit revisions for every curated model. Each download is
# fetched at exactly this commit, so a fresh install is reproducible and an
# upstream repo update (or compromise) cannot silently change what ships. Bump a
# SHA here deliberately when adopting a new model revision.
MODEL_REVISIONS: Mapping[str, str] = {
    KOKORO_MLX_DEFAULT_MODEL_REPO: "a71e4d38b236d968966a2002c4c895dbd12b1c3c",
    KOKORO_PACKAGE_DEFAULT_MODEL: "f3ff3571791e39611d31c381e3a41a3af07b4987",
    CLEANER_MLX_DEFAULT_MODEL: "7f0dc925e0d0afb0322d96f9255cfddf2ba5636e",
    CLEANER_CUDA_DEFAULT_MODEL: "989aa7980e4cf806f80c7fef2b1adb7bc71aa306",
    "Qwen/Qwen2.5-3B-Instruct": "aa8e72537993ba99e69dfaafa59ed015b17504d1",
    "mlx-community/Llama-3.2-1B-Instruct-4bit": "08231374eeacb049a0eade7922910865b8fce912",
}



def _normalize_model_id(model_id: str) -> str:
    normalized_model_id = model_id.strip()
    if "://" in normalized_model_id:
        raise ValueError(
            f"Model must be a Hugging Face repository id, not a URL: {model_id!r}."
        )
    return normalized_model_id


def pinned_revision_for(model_id: str) -> str | None:
    """Return the pinned commit revision for a curated model, if one exists."""
    return MODEL_REVISIONS.get(_normalize_model_id(model_id))


def _deduplicate_model_ids(model_ids: Iterable[str]) -> tuple[str, ...]:
    """Normalize model ids and preserve their first-seen order."""
    normalized_ids = (_normalize_model_id(model_id) for model_id in model_ids)
    return tuple(dict.fromkeys(model_id for model_id in normalized_ids if model_id))


def resolve_model_groups(group_ids: Iterable[str]) -> tuple[str, ...]:
    """Resolve allowlisted model groups to stable, deduplicated model ids."""
    resolved: list[str] = []
    for group_id in group_ids:
        normalized_group_id = group_id.strip().lower()
        try:
            resolved.extend(MODEL_GROUPS[normalized_group_id])
        except KeyError as exc:
            available = ", ".join(MODEL_GROUPS)
            raise ValueError(
                f"Unknown model group {group_id!r}. Available groups: {available}."
            ) from exc
    return _deduplicate_model_ids(resolved)
