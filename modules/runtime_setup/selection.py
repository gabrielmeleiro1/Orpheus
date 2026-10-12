"""Validate runtime profiles and active model selections."""

from __future__ import annotations

from typing import Iterable

from modules.runtime_profiles import RuntimeProfile, get_runtime_profile
from modules.runtime_setup.errors import SetupValidationError


def _normalize_identifier(value: str, *, kind: str) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        raise SetupValidationError(f"{kind} must not be empty.")
    if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in normalized):
        raise SetupValidationError(f"Invalid {kind} identifier: {value!r}.")
    return normalized


def _validated_profile(profile_id: str) -> RuntimeProfile:
    normalized = _normalize_identifier(profile_id, kind="profile")
    try:
        profile = get_runtime_profile(normalized)
    except ValueError as exc:
        raise SetupValidationError(str(exc)) from exc
    if not profile.is_compatible():
        reason = profile.incompatibility_reason() or "profile is unavailable"
        raise SetupValidationError(f"Runtime profile {profile.id!r} is unavailable: {reason}")
    return profile


def _validated_model_groups(
    profile: RuntimeProfile,
    model_groups: Iterable[str],
) -> tuple[str, ...]:
    normalized_groups = tuple(
        dict.fromkeys(
            _normalize_identifier(group_id, kind="model group") for group_id in model_groups
        )
    )
    unavailable = [
        group_id for group_id in normalized_groups if group_id not in profile.model_groups
    ]
    if unavailable:
        available = ", ".join(profile.model_groups) or "none"
        raise SetupValidationError(
            f"Model group {unavailable[0]!r} is not available for profile "
            f"{profile.id!r}. Allowed groups: {available}."
        )
    return normalized_groups


def require_voice_model(profile_id: str, model_groups: Iterable[str]) -> None:
    if profile_id != "base" and not any(group.startswith("tts-") for group in model_groups):
        raise SetupValidationError("Select the Kokoro voice model — it's required to create audio.")


def validate_model_selection(model_groups: Iterable[str]) -> None:
    """Installation may cache many models; an active setup chooses one cleaner."""
    if sum(group.startswith("cleaner-") for group in model_groups) > 1:
        raise SetupValidationError("Select one local cleaner. Other models can stay downloaded.")
