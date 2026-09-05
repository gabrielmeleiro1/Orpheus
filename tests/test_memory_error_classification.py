"""Memory recovery must not hide unrelated provider failures."""
from types import SimpleNamespace

import pytest

from modules.pipeline.retry import is_memory_pressure_error as pipeline_memory_error
from modules.pipeline.retry import should_degrade_sequential_from_memory_errors
from modules.pipeline.worker_errors import classify_worker_startup_error
from modules.tts.cleaner_cuda import _reraise_cuda_runtime_error
from modules.tts.cleaner_errors import CleanerRuntimeMemoryError, is_memory_pressure_error


@pytest.mark.parametrize('message', [
    'No such file: /models/bloom/config.json',
    'Permission denied: /models/memory/config.json',
    'Metal device is not supported',
    'VRAM monitor unavailable',
    'failed to load /models/oom/config.json',
    'memory mapped file is corrupt',
])
def test_unrelated_provider_errors_do_not_select_memory_recovery(message):
    assert not is_memory_pressure_error(message)
    assert not pipeline_memory_error(message)
    assert not should_degrade_sequential_from_memory_errors([SimpleNamespace(error=message)])
    original = RuntimeError(message)
    with pytest.raises(RuntimeError) as raised:
        _reraise_cuda_runtime_error(original)
    assert raised.value is original
    status = classify_worker_startup_error(original, provider='cuda', device='cuda:0', phase='worker_init')
    assert status.error_type != 'WorkerRuntimeMemoryError'


@pytest.mark.parametrize('message', [
    'CUDA out of memory. Tried to allocate 2 GiB',
    'MLX out of memory while allocating Metal buffer',
    'MPS backend out of memory',
    'DefaultCPUAllocator: can\'t allocate memory: you tried to allocate 1024 bytes',
    'cannot allocate memory',
    'std::bad_alloc',
    'cudaErrorMemoryAllocation',
    'CUBLAS_STATUS_ALLOC_FAILED',
    'OOM during model load',
    'insufficient VRAM for weights',
    'memory pressure detected',
    '[E300] VRAM overflow: VRAM overflow - model too large for available memory (kokoro)',
])
def test_known_provider_memory_signatures_keep_recovery(message):
    assert is_memory_pressure_error(message)
    assert pipeline_memory_error(message)
    with pytest.raises(CleanerRuntimeMemoryError):
        _reraise_cuda_runtime_error(RuntimeError(message))
    assert classify_worker_startup_error(RuntimeError(message), provider='mlx', device=None, phase='load').error_type == 'WorkerRuntimeMemoryError'


def test_typed_memory_error_needs_no_message():
    with pytest.raises(CleanerRuntimeMemoryError):
        _reraise_cuda_runtime_error(MemoryError())
    assert classify_worker_startup_error(MemoryError(), provider='cpu', device='cpu', phase='load').error_type == 'WorkerRuntimeMemoryError'


@pytest.mark.parametrize('message', ['VRAM monitor unavailable', 'memory mapped file is corrupt'])
def test_synthesis_does_not_relabel_unrelated_errors_as_memory(message):
    from modules.errors import SynthesisError
    from modules.tts.engine import TTSEngine

    engine = TTSEngine.__new__(TTSEngine)
    engine._model_path = 'test'

    def fail():
        raise RuntimeError(message)

    with pytest.raises(SynthesisError, match=message):
        engine._synthesize_with_retries(fail, max_retries=1)
