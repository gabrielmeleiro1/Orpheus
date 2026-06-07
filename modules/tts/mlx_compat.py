"""MLX/MLX-audio compatibility adapter helpers."""

from __future__ import annotations

import inspect
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional

MLX_AVAILABLE = False
MLX_IMPORT_ERROR: Optional[Exception] = None
_MLX_RUNTIME_CHECKED = False
mx = None
generate_audio = None
_load_tts_model_fn = None
_TQDM_LOCK_PATCHED = False
_GENERATE_AUDIO_SIGNATURE_PARAM_CACHE: dict[Any, frozenset[str]] = {}
_GENERATE_AUDIO_SIGNATURE_PARAM_CACHE_LOCK = threading.Lock()


class MLXAudioAdapter:
    """Normalized adapter over MLX/MLX-audio compatibility helpers."""

    def patch_load_weights_compat(self, module_cls: type) -> bool:
        return patch_mlx_load_weights_compat(module_cls)

    def patch_generation_result_compat(
        self,
        result_cls: type,
        default_sample_rate: int = 24000,
    ) -> bool:
        return patch_mlx_generation_result_compat(
            result_cls,
            default_sample_rate=default_sample_rate,
        )

    def inject_model_arg(
        self,
        kwargs: dict[str, Any],
        model_path: str,
        loaded_model: Any = None,
        generate_fn: Optional[Callable] = None,
    ) -> None:
        inject_generate_audio_model_arg(
            kwargs=kwargs,
            model_path=model_path,
            loaded_model=loaded_model,
            generate_fn=generate_fn,
        )

    def inject_output_args(
        self,
        kwargs: dict[str, Any],
        output_dir: str,
        file_prefix: str,
        generate_fn: Optional[Callable] = None,
    ) -> None:
        inject_generate_audio_output_args(
            kwargs=kwargs,
            output_dir=output_dir,
            file_prefix=file_prefix,
            generate_fn=generate_fn,
        )

    def runtime_error_message(self, import_error: Optional[Exception]) -> str:
        return mlx_runtime_error_message(import_error)

    def ensure_runtime(self) -> tuple[Any, Any]:
        return ensure_mlx_runtime()

    def ensure_tqdm_thread_lock(self) -> None:
        ensure_tqdm_thread_lock()


MLX_AUDIO_ADAPTER = MLXAudioAdapter()


def patch_mlx_load_weights_compat(module_cls: type) -> bool:
    """Patch MLX Module.load_weights to accept mlx-audio `weights=` keyword."""
    load_weights = getattr(module_cls, "load_weights", None)
    if load_weights is None:
        return False

    if getattr(load_weights, "_audiobook_weights_compat", False):
        return False

    try:
        sig = inspect.signature(load_weights)
    except (TypeError, ValueError):
        return False

    params = sig.parameters
    if "weights" in params or "file_or_weights" not in params:
        return False

    def _compat_load_weights(self, *args, **kwargs):  # noqa: ANN003
        if not args and "weights" in kwargs and "file_or_weights" not in kwargs:
            kwargs["file_or_weights"] = kwargs.pop("weights")
        return load_weights(self, *args, **kwargs)

    setattr(_compat_load_weights, "_audiobook_weights_compat", True)
    setattr(module_cls, "load_weights", _compat_load_weights)
    return True


def patch_mlx_generation_result_compat(
    result_cls: type,
    default_sample_rate: int = 24000,
) -> bool:
    """Patch GenerationResult.__init__ to tolerate missing `sample_rate`."""
    init_fn = getattr(result_cls, "__init__", None)
    if init_fn is None:
        return False

    if getattr(init_fn, "_audiobook_sample_rate_compat", False):
        return False

    try:
        sig = inspect.signature(init_fn)
    except (TypeError, ValueError):
        return False

    params = sig.parameters
    if "sample_rate" not in params:
        return False

    sample_rate_param = params["sample_rate"]
    if sample_rate_param.default is not inspect._empty:
        return False

    arg_names = [name for name in params if name != "self"]
    sample_rate_index = arg_names.index("sample_rate")

    def _compat_init(self, *args, **kwargs):  # noqa: ANN003
        if "sample_rate" not in kwargs and len(args) <= sample_rate_index:
            kwargs["sample_rate"] = default_sample_rate
        return init_fn(self, *args, **kwargs)

    setattr(_compat_init, "_audiobook_sample_rate_compat", True)
    setattr(result_cls, "__init__", _compat_init)
    return True


def inject_generate_audio_model_arg(
    kwargs: dict[str, Any],
    model_path: str,
    loaded_model: Any = None,
    generate_fn: Optional[Callable] = None,
) -> None:
    """Inject model kwarg compatible with installed mlx-audio API."""
    fn = generate_fn or generate_audio
    params = _get_generate_audio_signature_param_names(fn)
    if params is None:
        kwargs["model_path"] = model_path
        return

    if "model_path" in params:
        kwargs["model_path"] = model_path
    elif "model" in params:
        kwargs["model"] = loaded_model if loaded_model is not None else model_path
    else:
        kwargs["model_path"] = model_path


def inject_generate_audio_output_args(
    kwargs: dict[str, Any],
    output_dir: str,
    file_prefix: str,
    generate_fn: Optional[Callable] = None,
) -> None:
    """Inject output kwargs compatible with installed mlx-audio API."""
    fn = generate_fn or generate_audio
    params = _get_generate_audio_signature_param_names(fn)
    if params is None:
        kwargs["output_path"] = output_dir
        kwargs["file_prefix"] = file_prefix
        return

    if "output_path" in params:
        kwargs["output_path"] = output_dir
        kwargs["file_prefix"] = file_prefix
    else:
        kwargs["file_prefix"] = str(Path(output_dir) / file_prefix)


def _get_generate_audio_signature_param_names(fn: Optional[Callable]) -> Optional[frozenset[str]]:
    """Return cached generate_audio parameter names when introspection succeeds."""
    if fn is None:
        return None

    signature_target = _generate_audio_signature_target(fn)
    try:
        with _GENERATE_AUDIO_SIGNATURE_PARAM_CACHE_LOCK:
            cached_params = _GENERATE_AUDIO_SIGNATURE_PARAM_CACHE.get(signature_target)
    except TypeError:
        cached_params = None
    else:
        if cached_params is not None:
            return cached_params

    try:
        params = frozenset(inspect.signature(signature_target).parameters)
    except (TypeError, ValueError):
        return None

    try:
        with _GENERATE_AUDIO_SIGNATURE_PARAM_CACHE_LOCK:
            _GENERATE_AUDIO_SIGNATURE_PARAM_CACHE[signature_target] = params
    except TypeError:
        pass
    return params


def _generate_audio_signature_target(fn: Callable) -> Callable:
    side_effect = getattr(fn, "side_effect", None)
    if callable(side_effect):
        return side_effect
    return fn


def mlx_runtime_error_message(import_error: Optional[Exception]) -> str:
    return (
        "mlx-audio runtime unavailable. "
        f"Import error: {import_error}. "
        "Install a compatible set, for example: "
        "pip install 'mlx-audio==0.1.0' 'mlx-lm==0.24.1' 'mlx-vlm==0.3.0'."
    )


def load_tts_model(model_path: str):
    """Load and return a TTS model object using the installed mlx-audio runtime."""
    global _load_tts_model_fn

    ensure_mlx_runtime()
    patch_frozen_espeakng_loader_paths()
    if _load_tts_model_fn is None:
        from mlx_audio.tts.utils import load_model as load_model_fn

        _load_tts_model_fn = load_model_fn

    return _load_tts_model_fn(model_path=model_path)


def patch_frozen_espeakng_loader_paths() -> None:
    """Point espeakng-loader at bundled data when running from PyInstaller."""
    if not getattr(sys, "frozen", False):
        return

    try:
        import espeakng_loader
    except ImportError:
        return

    meipass_dir = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    package_dir = next(
        (
            candidate / "espeakng_loader"
            for candidate in (
                meipass_dir,
                meipass_dir / "_internal",
                Path(sys.executable).resolve().parent / "_internal",
            )
            if (candidate / "espeakng_loader").is_dir()
        ),
        meipass_dir / "espeakng_loader",
    )
    library_path = package_dir / "libespeak-ng.dylib"
    data_path = package_dir / "espeak-ng-data"
    if not library_path.is_file() or not data_path.is_dir():
        return

    espeakng_loader.get_library_path = lambda: str(library_path)
    espeakng_loader.get_data_path = lambda: str(data_path)


def ensure_mlx_runtime() -> tuple[Any, Any]:
    """Import MLX/MLX-Audio on demand and return `(mx, generate_audio)`."""
    global MLX_AVAILABLE, MLX_IMPORT_ERROR, _MLX_RUNTIME_CHECKED, mx, generate_audio

    if _MLX_RUNTIME_CHECKED:
        if not MLX_AVAILABLE:
            raise ImportError(mlx_runtime_error_message(MLX_IMPORT_ERROR))
        return mx, generate_audio

    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TQDM_DISABLE", "1")
    patch_frozen_espeakng_loader_paths()

    try:
        import mlx.core as mx_module
        import mlx.nn as nn_module
        from mlx_audio.tts.generate import generate_audio as generate_audio_fn
        from mlx_audio.tts.models.base import GenerationResult

        patch_mlx_load_weights_compat(nn_module.Module)
        patch_mlx_generation_result_compat(GenerationResult)
        mx = mx_module
        generate_audio = generate_audio_fn
        MLX_AVAILABLE = True
        MLX_IMPORT_ERROR = None
    except ImportError as exc:
        MLX_AVAILABLE = False
        MLX_IMPORT_ERROR = exc
    finally:
        _MLX_RUNTIME_CHECKED = True

    if not MLX_AVAILABLE:
        raise ImportError(mlx_runtime_error_message(MLX_IMPORT_ERROR))

    return mx, generate_audio


def ensure_tqdm_thread_lock() -> None:
    """Force tqdm to use a thread lock to avoid multiprocessing lock paths."""
    global _TQDM_LOCK_PATCHED

    if _TQDM_LOCK_PATCHED:
        return

    lock = threading.RLock()
    patched = False

    try:
        from tqdm import std as tqdm_std

        tqdm_std.tqdm.set_lock(lock)
        patched = True
    except Exception:
        pass

    try:
        from tqdm.auto import tqdm as tqdm_auto

        tqdm_auto.set_lock(lock)
        patched = True
    except Exception:
        pass

    _TQDM_LOCK_PATCHED = patched
