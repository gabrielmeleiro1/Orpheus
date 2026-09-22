"""Tests for cleaner backend option metadata."""

from __future__ import annotations

from modules.tts.cleaner_backends.metadata import (
    build_cleaner_backend_option,
    list_cleaner_backend_options,
)
from modules.tts.cleaner_config import CUDA_CLEANER_MODEL_CHOICES, CUDA_CLEANER_QUANTIZATIONS


def test_build_cleaner_backend_option_cuda_includes_quantizations_and_device() -> None:
    option = build_cleaner_backend_option("cuda")

    assert option["name"] == "cuda"
    assert option["display_name"] == "Local CUDA"
    assert option["is_local"] is True
    assert option["requires_model"] is True
    assert option["requires_api_key"] is False
    assert option["models"] == CUDA_CLEANER_MODEL_CHOICES
    assert option["supported_quantizations"] == list(CUDA_CLEANER_QUANTIZATIONS)
    assert option["default_device"] == "cuda"


def test_build_cleaner_backend_option_mlx_has_no_cuda_specific_fields() -> None:
    option = build_cleaner_backend_option("mlx")

    assert option["name"] == "mlx"
    assert option["is_local"] is True
    assert "supported_quantizations" not in option
    assert "default_device" not in option


def test_build_cleaner_backend_option_api_requires_api_key() -> None:
    option = build_cleaner_backend_option("api")

    assert option["name"] == "api"
    assert option["is_local"] is False
    assert option["requires_api_key"] is True


def test_list_cleaner_backend_options_returns_all_registered_backends() -> None:
    options = list_cleaner_backend_options()
    names = [option["name"] for option in options]

    assert names == ["api", "basic", "cuda", "mlx"]
