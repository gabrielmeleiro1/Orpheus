"""CUDA runtime and model-cache helpers for TextCleaner."""

from __future__ import annotations

import gc
import logging
import threading
from typing import Any, Callable, Optional

from modules.tts.cleaner_trace import run_local_batch_async
from modules.tts.model_snapshots import installed_model_path

from modules.tts.cleaner_config import (
    CUDA_CLEANER_QUANTIZATIONS,
    list_cuda_cleaner_models,
    normalize_cuda_device,
    normalize_cuda_quantization,
)
from modules.tts.cleaner_errors import (
    CleanerRuntimeMemoryError,
    CleanerRuntimeUnavailable,
    is_memory_pressure_error,
)

TORCH_CUDA_AVAILABLE = False
TORCH_CUDA_IMPORT_ERROR: Optional[Exception] = None
_TORCH_CUDA_RUNTIME_CHECKED = False
_TORCH_CUDA_RUNTIME_LOCK = threading.Lock()
torch = None
logger = logging.getLogger(__name__)


def cuda_runtime_error_message(import_error: Optional[Exception]) -> str:
    return (
        "CUDA cleaner runtime unavailable. "
        f"Error: {import_error}. "
        "Install PyTorch with CUDA support and ensure a compatible GPU is available."
    )


def ensure_cuda_runtime() -> tuple[Any]:
    """Import PyTorch on demand and verify CUDA availability."""
    if not _TORCH_CUDA_RUNTIME_CHECKED:
        with _TORCH_CUDA_RUNTIME_LOCK:
            if not _TORCH_CUDA_RUNTIME_CHECKED:
                _import_cuda_runtime()
    if not TORCH_CUDA_AVAILABLE:
        raise ImportError(cuda_runtime_error_message(TORCH_CUDA_IMPORT_ERROR))
    return (torch,)


def _import_cuda_runtime() -> None:
    global TORCH_CUDA_AVAILABLE, TORCH_CUDA_IMPORT_ERROR, _TORCH_CUDA_RUNTIME_CHECKED, torch

    try:
        import torch as torch_module

        if not torch_module.cuda.is_available():
            raise ImportError("CUDA is not available on this system.")

        torch = torch_module
        TORCH_CUDA_AVAILABLE = True
        TORCH_CUDA_IMPORT_ERROR = None
    except ImportError as exc:
        TORCH_CUDA_AVAILABLE = False
        TORCH_CUDA_IMPORT_ERROR = exc
    finally:
        _TORCH_CUDA_RUNTIME_CHECKED = True


def _cuda_cache_key(cleaner: Any) -> str:
    device = normalize_cuda_device(cleaner.config.cuda_device)
    quantization = normalize_cuda_quantization(cleaner.config.cuda_quantization)
    return f"{cleaner.config.model_name}|{device}|{quantization}"


def _is_cuda_oom_error(exc: BaseException, torch_module: Any) -> bool:
    cuda_module = getattr(torch_module, "cuda", None)
    if cuda_module is not None:
        oom_error = getattr(cuda_module, "OutOfMemoryError", None)
        if oom_error is not None and isinstance(exc, oom_error):
            return True
    return is_memory_pressure_error(str(exc))


def _reraise_cuda_runtime_error(exc: BaseException, *, torch_module: Any | None = None) -> None:
    if isinstance(exc, (CleanerRuntimeUnavailable, CleanerRuntimeMemoryError)):
        raise exc
    if isinstance(exc, MemoryError) or (
        torch_module is not None and _is_cuda_oom_error(exc, torch_module)
    ):
        raise CleanerRuntimeMemoryError(str(exc)) from exc
    if isinstance(exc, ImportError):
        raise CleanerRuntimeUnavailable(cuda_runtime_error_message(exc)) from exc
    if is_memory_pressure_error(str(exc)):
        raise CleanerRuntimeMemoryError(str(exc)) from exc
    raise exc


_cleaner_model_cache: dict[str, tuple[Any, Any]] = {}
_cleaner_cache_lock = threading.RLock()


def get_cached_cleaner(cache_key: str) -> Optional[tuple[Any, Any]]:
    """Get cleaner model from cache if available."""
    with _cleaner_cache_lock:
        return _cleaner_model_cache.get(cache_key)


def set_cached_cleaner(cache_key: str, model: Any, tokenizer: Any) -> None:
    """Cache cleaner model (single-entry cache)."""
    with _cleaner_cache_lock:
        _cleaner_model_cache.clear()
        _cleaner_model_cache[cache_key] = (model, tokenizer)


_cleaner_model_locks: dict[str, threading.RLock] = {}


def get_cleaner_model_lock(cache_key: str) -> threading.RLock:
    """Return the use-lock shared by every cleaner that can hold this model."""
    with _cleaner_cache_lock:
        lock = _cleaner_model_locks.get(cache_key)
        if lock is None:
            lock = _cleaner_model_locks[cache_key] = threading.RLock()
        return lock


def _best_effort_empty_cuda_cache() -> None:
    if TORCH_CUDA_AVAILABLE and torch is not None:
        try:
            cuda_module = getattr(torch, "cuda", None)
            if cuda_module is not None:
                cuda_module.empty_cache()
        except Exception:
            pass


def clear_cleaner_cache() -> None:
    """Clear cleaner cache and best-effort free CUDA memory."""
    with _cleaner_cache_lock:
        _cleaner_model_cache.clear()
        gc.collect()
        _best_effort_empty_cuda_cache()
        gc.collect()


def _evict_cached_cleaners() -> None:
    """Free a cached model before loading its replacement, not after."""
    with _cleaner_cache_lock:
        if _cleaner_model_cache:
            clear_cleaner_cache()


def discard_cached_cleaner(cache_key: str, model: Any) -> None:
    """Drop the cache entry only when it holds ``model``; keep another cleaner's."""
    with _cleaner_cache_lock:
        cached = _cleaner_model_cache.get(cache_key)
        if cached is not None and cached[0] is model:
            del _cleaner_model_cache[cache_key]


def release_cleaner_memory() -> None:
    """Best-effort return of freed model memory to the device."""
    with _cleaner_cache_lock:
        gc.collect()
        _best_effort_empty_cuda_cache()
        gc.collect()


def _runtime_unavailable(exc: ImportError) -> CleanerRuntimeUnavailable:
    message = str(exc)
    if not message.startswith("CUDA cleaner runtime unavailable."):
        message = cuda_runtime_error_message(exc)
    return CleanerRuntimeUnavailable(message)


def _resolve_device_map(device: str) -> dict[str, str]:
    # Pin the whole model to one GPU: "auto" would spread it across every
    # visible GPU or offload to CPU, outside the per-device memory plan.
    return {"": "cuda:0" if device == "cuda" else device}


def _load_cuda_model(cleaner: Any, torch_module: Any) -> tuple[Any, Any]:
    from transformers import AutoModelForCausalLM, AutoTokenizer

    model_name = installed_model_path(cleaner.config.model_name)
    device = normalize_cuda_device(cleaner.config.cuda_device)
    quantization = normalize_cuda_quantization(cleaner.config.cuda_quantization)
    device_map = _resolve_device_map(device)

    tokenizer = AutoTokenizer.from_pretrained(model_name, local_files_only=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    if quantization in {"4bit", "8bit"}:
        try:
            from transformers import BitsAndBytesConfig
        except ImportError as exc:
            raise CleanerRuntimeUnavailable(
                "CUDA cleaner quantization requires bitsandbytes. "
                "Install it with: pip install bitsandbytes"
            ) from exc

        bnb_kwargs: dict[str, Any] = {
            "load_in_4bit": quantization == "4bit",
            "load_in_8bit": quantization == "8bit",
        }
        if quantization == "4bit":
            # The default fp32 compute dtype upcasts every 4-bit matmul.
            bnb_kwargs["bnb_4bit_compute_dtype"] = torch_module.float16
        bnb_config = BitsAndBytesConfig(**bnb_kwargs)
        model = AutoModelForCausalLM.from_pretrained(
            model_name,
            quantization_config=bnb_config,
            device_map=device_map,
            local_files_only=True,
        )
    else:
        model = AutoModelForCausalLM.from_pretrained(
            model_name,
            dtype=torch_module.float16,
            device_map=device_map,
            local_files_only=True,
        )

    return model, tokenizer


def _cancel_stopping_criteria(cancel_requested: Any, torch_module: Any) -> Any:
    """Stop decoding between tokens once the conversion is cancelled.

    A truncated response fails the cleaner's length guard, so the chunk falls
    back to rule-based text; the caller then observes the cancellation.
    """
    from transformers import StoppingCriteria, StoppingCriteriaList

    class _CancelRequested(StoppingCriteria):
        def __call__(self, input_ids, scores, **kwargs):
            del scores, kwargs
            return torch_module.full(
                (input_ids.shape[0],),
                bool(cancel_requested()),
                dtype=torch_module.bool,
                device=input_ids.device,
            )

    return StoppingCriteriaList([_CancelRequested()])


def generate_cuda_response(cleaner: Any, prompt: str) -> str:
    """Generate cleaned text via local CUDA transformers runtime."""
    with cleaner._model_lock:
        try:
            torch_module, = ensure_cuda_runtime()
        except ImportError as exc:
            raise _runtime_unavailable(exc) from exc

        tokenizer = cleaner._tokenizer
        model = cleaner._model
        if getattr(tokenizer, "chat_template", None):
            # Instruct models need their conversation delimiters and assistant
            # prefix; a raw completion prompt can produce repeated instructions.
            inputs = tokenizer.apply_chat_template(
                [{"role": "user", "content": prompt}],
                add_generation_prompt=True,
                tokenize=True,
                return_dict=True,
                return_tensors="pt",
            )
        else:
            inputs = tokenizer(prompt, return_tensors="pt")
        device = next(model.parameters()).device
        inputs = {key: value.to(device) for key, value in inputs.items()}

        generation_kwargs: dict[str, object] = {
            # Cleanup must be repeatable even with legacy nonzero temperatures.
            "do_sample": False,
            "num_beams": 1,
            "max_new_tokens": cleaner.config.max_tokens,
            "pad_token_id": (
                tokenizer.pad_token_id
                if tokenizer.pad_token_id is not None
                else tokenizer.eos_token_id
            ),
        }
        cancel_requested = getattr(cleaner, "generation_cancel_requested", None)
        if callable(cancel_requested):
            generation_kwargs["stopping_criteria"] = _cancel_stopping_criteria(
                cancel_requested, torch_module,
            )
        try:
            output_ids = model.generate(**inputs, **generation_kwargs)
        except Exception as exc:
            _reraise_cuda_runtime_error(exc, torch_module=torch_module)

        prompt_length = inputs["input_ids"].shape[-1]
        generated_ids = output_ids[0, prompt_length:]
        return tokenizer.decode(generated_ids, skip_special_tokens=True)


class CleanerCUDAStrategy:
    """CUDA cleaner backend strategy for TextCleaner facade."""

    name = "cuda"
    is_local = True
    requires_model = True
    requires_api_key = False
    display_name = "Local CUDA"
    backend = "cuda"
    supported_quantizations = CUDA_CLEANER_QUANTIZATIONS
    default_device = "cuda"

    @classmethod
    def list_models(cls) -> list[str]:
        return list_cuda_cleaner_models()

    def load(self, cleaner: Any) -> None:
        try:
            torch_module, = ensure_cuda_runtime()
        except ImportError as exc:
            raise _runtime_unavailable(exc) from exc

        if cleaner._loaded:
            return

        cache_key = _cuda_cache_key(cleaner)
        # Cleaners sharing a cached model must serialize generation on it.
        cleaner._model_lock = get_cleaner_model_lock(cache_key)
        if cleaner.config.cache_model:
            cached = get_cached_cleaner(cache_key)
            if cached is not None:
                installed_model_path(cleaner.config.model_name)
                cleaner._model, cleaner._tokenizer = cached
                cleaner._loaded = True
                logger.info("Text cleaner loaded from cache: %s", cache_key)
                return
            _evict_cached_cleaners()

        logger.info(
            "Loading text cleaner: %s (device=%s, quantization=%s)",
            cleaner.config.model_name,
            cleaner.config.cuda_device,
            cleaner.config.cuda_quantization,
        )
        try:
            cleaner._model, cleaner._tokenizer = _load_cuda_model(cleaner, torch_module)
        except Exception as exc:
            _reraise_cuda_runtime_error(exc, torch_module=torch_module)

        cleaner._loaded = True

        if cleaner.config.cache_model:
            set_cached_cleaner(cache_key, cleaner._model, cleaner._tokenizer)

        logger.info("Text cleaner loaded")

    async def clean_batch_with_trace_async(
        self,
        cleaner: Any,
        texts: list[str],
        cancel_requested: Callable[[], bool] | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> list[Any]:
        return await run_local_batch_async(cleaner, texts, cancel_requested, progress_callback)

    def generate_llm_response(self, cleaner: Any, prompt: str) -> str:
        return generate_cuda_response(cleaner, prompt)

    def unload(self, cleaner: Any, *, keep_in_cache: bool = True) -> None:
        if not cleaner._loaded:
            return
        model = cleaner._model
        cleaner._model = None
        cleaner._tokenizer = None
        cleaner._loaded = False

        if not keep_in_cache:
            discard_cached_cleaner(_cuda_cache_key(cleaner), model)
            del model
            release_cleaner_memory()

        logger.info("Text cleaner unloaded (cached: %s)", keep_in_cache)

    def clear_cache(self, cleaner: Any) -> None:
        _ = cleaner
        clear_cleaner_cache()
        logger.info("Cleaner model cache cleared")
