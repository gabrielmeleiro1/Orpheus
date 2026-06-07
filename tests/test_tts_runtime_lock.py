"""
Test TTS runtime lock patching for tqdm compatibility.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from modules.tts.engine import (
    _ensure_tqdm_thread_lock,
    _patch_mlx_load_weights_compat,
    _patch_mlx_generation_result_compat,
)


def _test_tqdm_thread_lock_patch_is_safe_and_idempotent() -> None:
    _ensure_tqdm_thread_lock()
    _ensure_tqdm_thread_lock()

    from tqdm import tqdm

    lock = tqdm.get_lock()
    assert lock is not None
    assert hasattr(lock, "acquire")
    assert hasattr(lock, "release")
    print("✓ tqdm lock patch is safe and idempotent")


def _test_mlx_load_weights_compat_patch_maps_weights_kwarg() -> None:
    class _ModuleLike:
        def __init__(self) -> None:
            self.last_call = None

        def load_weights(self, file_or_weights, strict=True):  # noqa: ANN001
            self.last_call = (file_or_weights, strict)
            return "ok"

    patched = _patch_mlx_load_weights_compat(_ModuleLike)
    assert patched is True

    model = _ModuleLike()
    result = model.load_weights(weights=[("decoder.weight", 1)], strict=False)
    assert result == "ok"
    assert model.last_call == ([("decoder.weight", 1)], False)

    # Idempotent: second patch attempt should be a no-op.
    assert _patch_mlx_load_weights_compat(_ModuleLike) is False
    print("✓ load_weights compat patch maps weights->file_or_weights")


def _test_mlx_load_weights_compat_patch_skips_legacy_signature() -> None:
    class _LegacyModuleLike:
        def load_weights(self, _weights, strict=True):  # noqa: ANN001, ARG002
            return "legacy"

    assert _patch_mlx_load_weights_compat(_LegacyModuleLike) is False
    print("✓ load_weights compat patch skips legacy signatures")


def _test_generation_result_compat_patch_adds_default_sample_rate() -> None:
    class _GenerationResultLike:
        def __init__(self, audio, samples, sample_rate, segment_idx):  # noqa: ANN001
            self.values = (audio, samples, sample_rate, segment_idx)

    patched = _patch_mlx_generation_result_compat(_GenerationResultLike, default_sample_rate=24000)
    assert patched is True

    item = _GenerationResultLike(audio=[0.0], samples=1, segment_idx=0)
    assert item.values[2] == 24000
    assert _patch_mlx_generation_result_compat(_GenerationResultLike, default_sample_rate=12345) is False
    print("✓ generation result compat patch injects default sample_rate")


def test_tts_runtime_lock() -> bool:
    print("\n" + "=" * 50)
    print("TTS RUNTIME LOCK TEST SUITE")
    print("=" * 50 + "\n")

    _test_tqdm_thread_lock_patch_is_safe_and_idempotent()
    _test_mlx_load_weights_compat_patch_maps_weights_kwarg()
    _test_mlx_load_weights_compat_patch_skips_legacy_signature()
    _test_generation_result_compat_patch_adds_default_sample_rate()

    print("\n" + "=" * 50)
    print("ALL TTS RUNTIME LOCK TESTS PASSED ✓")
    print("=" * 50 + "\n")
    return True


if __name__ == "__main__":
    success = test_tts_runtime_lock()
    sys.exit(0 if success else 1)
