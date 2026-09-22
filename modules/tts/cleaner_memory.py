"""Conservative CUDA cleaner memory estimates."""

from __future__ import annotations

GIB = 1024**3

DEFAULT_UNKNOWN_CLEANER_CUDA_BYTES = 6 * GIB
CLEANER_CUDA_LOAD_HEADROOM_BYTES = GIB

_CLEANER_CUDA_MEMORY_BYTES: dict[tuple[str, str], int] = {
    ("meta-llama/Llama-3.2-3B-Instruct", "4bit"): 3 * GIB,
    ("meta-llama/Llama-3.2-3B-Instruct", "8bit"): 4 * GIB,
    ("meta-llama/Llama-3.2-3B-Instruct", "fp16"): 6 * GIB,
    ("meta-llama/Llama-3.2-1B-Instruct", "4bit"): 2 * GIB,
    ("meta-llama/Llama-3.2-1B-Instruct", "8bit"): 3 * GIB,
    ("meta-llama/Llama-3.2-1B-Instruct", "fp16"): 4 * GIB,
    # Weight bytes (0.5/1/2 per parameter) plus 1 GiB runtime overhead,
    # rounded up to GiB, as for the 1.5B model.
    ("Qwen/Qwen2.5-3B-Instruct", "4bit"): 3 * GIB,
    ("Qwen/Qwen2.5-3B-Instruct", "8bit"): 4 * GIB,
    ("Qwen/Qwen2.5-3B-Instruct", "fp16"): 7 * GIB,
    ("Qwen/Qwen2.5-1.5B-Instruct", "4bit"): 2 * GIB,
    ("Qwen/Qwen2.5-1.5B-Instruct", "8bit"): 3 * GIB,
    ("Qwen/Qwen2.5-1.5B-Instruct", "fp16"): 4 * GIB,
}


def estimate_cleaner_cuda_bytes(model_name: str, quantization: str) -> int:
    """Return a conservative CUDA VRAM estimate for a cleaner model."""
    normalized_model = str(model_name or "").strip()
    normalized_quantization = str(quantization or "").strip().lower()
    return _CLEANER_CUDA_MEMORY_BYTES.get(
        (normalized_model, normalized_quantization),
        DEFAULT_UNKNOWN_CLEANER_CUDA_BYTES,
    )
