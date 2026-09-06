"""CUDA cleaner memory estimation tests."""

from __future__ import annotations

from modules.tts.cleaner_config import CUDA_CLEANER_MODEL_CHOICES, CUDA_CLEANER_QUANTIZATIONS

GIB = 1024**3


def test_estimate_cleaner_cuda_bytes_returns_table_values_for_curated_models():
    from modules.tts.cleaner_memory import estimate_cleaner_cuda_bytes

    expected = {
        ("meta-llama/Llama-3.2-3B-Instruct", "4bit"): 3 * GIB,
        ("meta-llama/Llama-3.2-3B-Instruct", "8bit"): 4 * GIB,
        ("meta-llama/Llama-3.2-3B-Instruct", "fp16"): 6 * GIB,
        ("meta-llama/Llama-3.2-1B-Instruct", "4bit"): 2 * GIB,
        ("meta-llama/Llama-3.2-1B-Instruct", "8bit"): 3 * GIB,
        ("meta-llama/Llama-3.2-1B-Instruct", "fp16"): 4 * GIB,
        ("Qwen/Qwen2.5-1.5B-Instruct", "4bit"): 2 * GIB,
        ("Qwen/Qwen2.5-1.5B-Instruct", "8bit"): 3 * GIB,
        ("Qwen/Qwen2.5-1.5B-Instruct", "fp16"): 4 * GIB,
    }

    for model_name in CUDA_CLEANER_MODEL_CHOICES:
        for quantization in CUDA_CLEANER_QUANTIZATIONS:
            assert estimate_cleaner_cuda_bytes(model_name, quantization) == expected[
                (model_name, quantization)
            ]


def test_estimate_cleaner_cuda_bytes_unknown_model_returns_conservative_default():
    from modules.tts.cleaner_memory import (
        DEFAULT_UNKNOWN_CLEANER_CUDA_BYTES,
        estimate_cleaner_cuda_bytes,
    )

    assert (
        estimate_cleaner_cuda_bytes("unknown/model", "4bit")
        == DEFAULT_UNKNOWN_CLEANER_CUDA_BYTES
    )
    assert DEFAULT_UNKNOWN_CLEANER_CUDA_BYTES == 6 * GIB


def test_estimate_cleaner_cuda_bytes_normalizes_quantization_case():
    from modules.tts.cleaner_memory import estimate_cleaner_cuda_bytes

    assert estimate_cleaner_cuda_bytes(
        "meta-llama/Llama-3.2-3B-Instruct",
        "FP16",
    ) == 6 * GIB
