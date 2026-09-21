"""Opt-in, offline Qwen inference checks on a real NVIDIA GPU.

Run with ORPHEUS_TEST_CUDA_CLEANER=1 and pytest -m gpu --no-cov on this file.
Missing dependencies, weights, or hardware fail an explicitly requested run.
"""

import os

import pytest

from modules.tts.cleaner import TextCleaner
from modules.tts.cleaner_config import CleanerConfig, DEFAULT_CUDA_MODEL


pytestmark = [
    pytest.mark.gpu,
    pytest.mark.slow,
    pytest.mark.skipif(
        os.getenv("ORPHEUS_TEST_CUDA_CLEANER") != "1",
        reason="Set ORPHEUS_TEST_CUDA_CLEANER=1 to run real local inference",
    ),
]


@pytest.mark.parametrize("quantization", ["4bit", "8bit", "fp16"])
@pytest.mark.parametrize("model_id", [DEFAULT_CUDA_MODEL, "Qwen/Qwen2.5-3B-Instruct"])
def test_qwen_cuda_repairs_artifacts_and_preserves_clean_text(monkeypatch, quantization, model_id):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    from huggingface_hub import snapshot_download
    import bitsandbytes as bnb
    import torch
    from scripts.download_models import pinned_revision_for

    model_path = snapshot_download(
        repo_id=model_id,
        revision=pinned_revision_for(model_id),
        local_files_only=True,
    )
    cleaner = TextCleaner(CleanerConfig(
        backend="cuda", model_name=model_path, cuda_device="cuda",
        cuda_quantization=quantization, cache_model=False, max_tokens=256,
    ))
    clean_text = "The traveler opened the old wooden door and stepped into the quiet room."
    damaged = "Iam tired. Ibegin again. Iwish the wall would stop moving."
    expected = "I am tired. I begin again. I wish the wall would stop moving."
    try:
        cleaner.load()
        assert next(cleaner._model.parameters()).device.type == "cuda"
        # Check real layer/weight types: successful text generation alone would
        # also pass if the loader silently ignored the requested precision.
        layers = list(cleaner._model.modules())
        four_bit = [layer for layer in layers if isinstance(layer, bnb.nn.Linear4bit)]
        eight_bit = [layer for layer in layers if isinstance(layer, bnb.nn.Linear8bitLt)]
        if quantization == "4bit":
            assert four_bit and not eight_bit
            assert all(layer.weight.dtype == torch.uint8 for layer in four_bit)
            assert all(layer.weight.quant_state is not None for layer in four_bit)
        elif quantization == "8bit":
            assert eight_bit and not four_bit
            assert all(layer.weight.dtype == torch.int8 for layer in eight_bit)
        else:
            assert not four_bit and not eight_bit
            assert all(weight.dtype == torch.float16 for weight in cleaner._model.parameters())
        print(
            f"{model_id} {quantization}: {len(four_bit)} 4-bit layers, {len(eight_bit)} 8-bit layers, "
            f"{cleaner._model.get_memory_footprint() / 1024**2:.1f} MiB model footprint"
        )
        trace = cleaner.clean_with_trace(damaged)
        assert trace.mode == "llm_success", trace
        assert trace.llm_error is None
        assert trace.cleaned_text == expected

        # Exercise generation on clean prose too, instead of only testing the gate.
        cleaner.config.skip_llm_when_not_needed = False
        trace = cleaner.clean_with_trace(clean_text)
        # Larger models may paraphrase even with conservative instructions.
        # The real preservation guard must reject that output and keep the input.
        assert trace.mode in {"llm_success", "hallucination_fallback"}, trace
        assert trace.llm_output
        if trace.mode == "hallucination_fallback":
            assert "preservation guard" in trace.llm_error.lower()
            print(f"{model_id} {quantization}: preservation guard kept clean prose unchanged")
        if model_id == DEFAULT_CUDA_MODEL:
            assert trace.mode == "llm_success", trace
        assert trace.cleaned_text == clean_text
    finally:
        cleaner.unload(keep_in_cache=False)
