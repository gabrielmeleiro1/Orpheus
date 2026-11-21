"""Package-backed Kokoro runtime adapter for CPU/CUDA hosts."""

from __future__ import annotations

import gc
import logging
import os
import sys
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

import numpy as np

from modules.errors import SynthesisError, TTSInstallationError
from modules.tts import model_cache
from modules.tts.model_snapshots import installed_model_path, require_local_phonemizer
from modules.tts.cleaner_errors import is_memory_pressure_error
from modules.tts.runtimes.errors import TTSRuntimeMemoryError, TTSRuntimeUnavailable
from modules.tts.runtimes.kokoro_base import KokoroRuntimeConfig, KokoroRuntimeFactory
from modules.tts.strategies.base import (
    SynthesisItem,
    SynthesisProgressCallback,
    SynthesisResult,
    has_no_speakable_text,
)
from modules.tts.voices import kokoro_language_code

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    import torch

_TORCH_COMPILE_ENV_VAR = "AUDIOBOOK_TTS_TORCH_COMPILE"
_BLANK_TEXT_SILENCE_MS = 50


@dataclass
class _SharedKokoroModel:
    """Cache entry: one loaded KModel and the per-language pipelines over it.

    Runtimes whose default voices differ only in language share the weights;
    every access happens under the cache key's model lock.
    """

    model: object
    pipelines: dict[str, object] = field(default_factory=dict)


def _is_out_of_memory(exc: BaseException) -> bool:
    """Return True for host or CUDA allocation failures."""
    if isinstance(exc, MemoryError):
        return True
    cuda = getattr(sys.modules.get("torch"), "cuda", None)
    oom_error = getattr(cuda, "OutOfMemoryError", None)
    if isinstance(oom_error, type) and isinstance(exc, oom_error):
        return True
    # CUDA/cuBLAS/allocator messages ("cudaErrorMemoryAllocation",
    # "CUBLAS_STATUS_ALLOC_FAILED", "std::bad_alloc") as well as "out of memory".
    return is_memory_pressure_error(str(exc))


def _torch_compile_requested(runtime_options: dict[str, object] | None) -> bool:
    # An explicit runtime option wins in both directions; the env var only
    # applies when the option is absent.
    if runtime_options and "torch_compile" in runtime_options:
        return str(runtime_options["torch_compile"]).strip().lower() in ("1", "true", "yes")
    return os.environ.get(_TORCH_COMPILE_ENV_VAR, "").strip().lower() in ("1", "true", "yes")


class PackageKokoroRuntime:
    """Adapter around the upstream ``kokoro.KPipeline`` package."""

    supports_file_batch_results = False

    def __init__(self, config: KokoroRuntimeConfig) -> None:
        self._config = config
        self.provider = config.runtime_spec.provider
        self.sample_rate = config.sample_rate
        self._pipeline = None
        self._shared: _SharedKokoroModel | None = None
        self._language_pipelines: dict[str, object] = {}
        self._last_cached_pipeline = None
        self._model_dir: Path | None = None
        self._chunks_processed = 0
        self._total_chunk_time_ms = 0.0

    @property
    def is_loaded(self) -> bool:
        return self._pipeline is not None

    @property
    def chunks_processed(self) -> int:
        return self._chunks_processed

    @property
    def average_chunk_time_ms(self) -> float:
        if self._chunks_processed <= 0:
            return 0.0
        return self._total_chunk_time_ms / self._chunks_processed

    def _cache_key(self) -> str:
        # The language is left out: pipelines for every language share the
        # loaded weights (see _SharedKokoroModel).
        compiled = _torch_compile_requested(self._config.runtime_options)
        kwargs = self._pipeline_kwargs()
        kwargs.pop("lang_code", None)
        return (
            f"kokoro-package:{kwargs!r}:"
            f"{self._config.runtime_spec.cache_key()!r}:compile={compiled}"
        )

    def load(self) -> None:
        if self._pipeline is not None:
            return

        cache_key = self._cache_key()
        # Hold the use-lock across lookup and load so concurrent runtimes for
        # the same key cannot load duplicate copies of the weights.
        with model_cache.get_model_lock(cache_key):
            shared = model_cache.get_cached_model(cache_key)
            if shared is not None:
                self._attach(shared)
                logger.info("Package Kokoro runtime loaded from cache")
                return
            self._load_uncached(cache_key)

    def _attach(self, shared: _SharedKokoroModel) -> None:
        language_code = kokoro_language_code(self._config.default_voice)
        pipeline = shared.pipelines.get(language_code)
        if pipeline is None:
            from kokoro import KPipeline

            pipeline = KPipeline(lang_code=language_code, model=shared.model)
            shared.pipelines[language_code] = pipeline
        self._shared = shared
        self._language_pipelines = shared.pipelines
        self._pipeline = pipeline
        self._last_cached_pipeline = shared

    def _load_uncached(self, cache_key: str) -> None:
        from modules.hf_cache import configure_hf_cache
        from modules.tts.mlx_compat import patch_espeakng_loader_paths

        configure_hf_cache()
        # Spawned workers must pick up the launcher's speech data location
        # before kokoro imports the phonemizer and locks in its paths.
        patch_espeakng_loader_paths()
        try:
            from kokoro import KPipeline
        except ImportError as exc:
            raise TTSRuntimeUnavailable(
                "Package Kokoro runtime is unavailable. Install kokoro>=0.9.4."
            ) from exc

        kwargs = self._pipeline_kwargs()
        try:
            local_model_dir = self._local_model_dir()
            from kokoro import KModel

            model_path = local_model_dir / "kokoro-v1_0.pth"
            config_path = local_model_dir / "config.json"
            kwargs["model"] = KModel(
                repo_id="hexgrad/Kokoro-82M",
                config=str(config_path),
                model=str(model_path),
            ).to(self._config.runtime_spec.device or self.provider).eval()
            kwargs.pop("repo_id", None)
            require_local_phonemizer()
        except TTSInstallationError:
            raise
        except Exception as exc:
            raise self._load_error(exc) from exc

        try:
            pipeline = KPipeline(**kwargs)
        except TypeError:
            dropped_kwargs = [
                key for key in ("repo_id", "device") if key in kwargs
            ]
            for key in dropped_kwargs:
                kwargs.pop(key, None)
            if dropped_kwargs:
                logger.warning(
                    "Installed kokoro package does not accept KPipeline kwargs %s; "
                    "retrying without them",
                    ", ".join(dropped_kwargs),
                )
            pipeline = KPipeline(**kwargs)
        except Exception as exc:
            raise self._load_error(exc) from exc

        shared = _SharedKokoroModel(
            model=kwargs["model"],
            pipelines={kokoro_language_code(self._config.default_voice): pipeline},
        )
        self._shared = shared
        self._language_pipelines = shared.pipelines
        self._pipeline = pipeline
        self._maybe_compile_pipeline_model()
        model_cache.set_cached_model(cache_key, shared)
        self._last_cached_pipeline = shared

    @staticmethod
    def _load_error(exc: BaseException) -> RuntimeError:
        # Memory failures must stay distinguishable so worker startup can
        # retry with fewer workers instead of reporting the runtime missing.
        if _is_out_of_memory(exc):
            return TTSRuntimeMemoryError(f"Package Kokoro runtime ran out of memory while loading: {exc}")
        return TTSRuntimeUnavailable(f"Package Kokoro runtime failed to load: {exc}")

    def _maybe_compile_pipeline_model(self) -> None:
        """Optionally torch.compile the vocoder decoder (opt-in, CUDA-oriented).

        Compilation trades a one-time warmup cost (tens of seconds, amortized by
        the inductor on-disk cache) for ~1.4x faster synthesis, so it only pays
        off for longer conversions. Only the decoder is compiled: prosody and
        durations stay eager and unchanged, while the compiled vocoder draws a
        different (statistically equivalent) noise realization for aspiration.

        torch.compile() itself is lazy — backend failures (no triton, unusable
        inductor) only surface at the first forward — so a probe synthesis runs
        before the pipeline is published, and any failure restores the eager
        decoder.
        """
        if not _torch_compile_requested(self._config.runtime_options):
            return
        model = getattr(self._pipeline, "model", None)
        if model is None:
            return
        eager_decoder = model.decoder
        try:
            import torch

            model.decoder = torch.compile(model.decoder, dynamic=True)
            probe_voice = self._voice_arg(self._config.default_voice)
            for _ in self._pipeline("compile check", voice=probe_voice):
                pass
            logger.info("Package Kokoro runtime enabled torch.compile for the decoder")
        except Exception as exc:
            model.decoder = eager_decoder
            logger.warning("torch.compile unavailable; using eager synthesis: %s", exc)

    def _local_model_dir(self) -> Path:
        # Snapshot resolution re-verifies the download receipt (tens of
        # milliseconds); voices are resolved per chunk, so verify once per load.
        if self._model_dir is not None:
            return self._model_dir
        model_name = self._config.runtime_spec.resolved_model()
        if model_name is None:
            raise TTSInstallationError("No model configured. Reinstall the model from Setup.")
        candidate = Path(installed_model_path(model_name))
        if candidate.is_dir():
            self._model_dir = candidate
            return candidate
        raise TTSInstallationError(
            f"Model {model_name} is not installed. Reinstall the model from Setup."
        )

    def _pipeline_kwargs(self) -> dict[str, object]:
        kwargs: dict[str, object] = {
            "lang_code": kokoro_language_code(self._config.default_voice),
            "repo_id": self._config.runtime_spec.resolved_model(),
        }
        kwargs["device"] = self._config.runtime_spec.device or self.provider
        return kwargs

    def _voice_arg(self, voice: str) -> str | torch.Tensor:
        if voice.endswith(".pt"):
            return self._pipeline.load_single_voice(voice)
        local_model_dir = self._local_model_dir()
        voice_paths = [
            local_model_dir / "voices" / f"{name}.pt" for name in voice.split(",")
        ]
        for voice_path in voice_paths:
            if not voice_path.is_file():
                raise TTSInstallationError(
                    f"Voice {voice_path.stem} is missing from the installed snapshot "
                    f"for {self._config.runtime_spec.resolved_model()}. Reinstall the model from Setup."
                )
        # Keep intact filenames out of KPipeline's comma-delimited blend parser.
        packs = [self._pipeline.load_single_voice(str(path)) for path in voice_paths]
        if len(packs) == 1:
            return packs[0]

        import torch

        return torch.mean(torch.stack(packs), dim=0)

    def _write_output_audio(self, output_path: Path, audio: np.ndarray) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import soundfile as sf
        except ModuleNotFoundError:
            pcm = np.clip(np.asarray(audio, dtype=np.float32).reshape(-1), -1.0, 1.0)
            pcm16 = (pcm * 32767.0).astype(np.int16, copy=False)
            with wave.open(str(output_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(self.sample_rate)
                wav_file.writeframes(pcm16.tobytes())
            return

        sf.write(str(output_path), audio, self.sample_rate)

    def unload(self, *, keep_in_cache: bool = True) -> None:
        entry = self._shared if self._shared is not None else self._pipeline
        if entry is None:
            entry = self._last_cached_pipeline
        if not keep_in_cache and entry is not None:
            model_cache.discard_cached_model_if_same(
                self._cache_key(),
                entry,
            )
            self._last_cached_pipeline = None
        elif keep_in_cache and entry is not None:
            self._last_cached_pipeline = entry
        self._pipeline = None
        self._shared = None
        self._language_pipelines = {}
        self._model_dir = None
        if not keep_in_cache and entry is not None:
            # Buffers are only reclaimable once this runtime holds no reference.
            del entry
            model_cache.release_device_memory()

    def warmup(
        self,
        *,
        voice: str | None = None,
        speed: float = 1.0,
        probe_text: str = "warmup check",
    ) -> None:
        self.synthesize(probe_text, voice or self._config.default_voice, speed)

    def synthesize(
        self,
        text: str,
        voice: str,
        speed: float = 1.0,
        output_path: Path | None = None,
    ) -> np.ndarray:
        if self._pipeline is None:
            raise RuntimeError("Model not loaded. Call load() first.")

        started_at = time.perf_counter()
        if has_no_speakable_text(text):
            # Keep blank chunks structurally valid for the pipeline. The MLX
            # adapter gets this behavior from TTSEngine; package-backed
            # runtimes need to provide the same short scene-break silence.
            frames = max(1, int(self.sample_rate * _BLANK_TEXT_SILENCE_MS / 1000))
            audio = np.zeros(frames, dtype=np.float32)
            if output_path is not None:
                self._write_output_audio(output_path, audio)
            self._chunks_processed += 1
            self._total_chunk_time_ms += (time.perf_counter() - started_at) * 1000
            return audio

        # The pipeline is shared with every other runtime holding the same cache
        # key (e.g. a voice preview raised during a conversion). KPipeline keeps
        # mutable per-call state, so calls into it must not interleave.
        with model_cache.get_model_lock(self._cache_key()):
            retry_after_oom = False
            try:
                audio_chunks = self._run_pipeline(text, voice, speed)
            except Exception as exc:
                # The GPU is shared with other applications and with the local
                # cleaner, so an allocation can fail transiently. Return cached
                # blocks to the driver and retry the chunk once before failing
                # the whole chapter.
                if not self._is_cuda_oom(exc):
                    raise
                retry_after_oom = True
            if retry_after_oom:
                # Outside the except block, so the failed attempt's traceback
                # no longer pins its intermediate tensors.
                self._release_cuda_memory()
                logger.warning("CUDA out of memory during synthesis; retrying chunk once")
                try:
                    audio_chunks = self._run_pipeline(text, voice, speed)
                except Exception as exc:
                    if self._is_cuda_oom(exc):
                        raise TTSRuntimeMemoryError(
                            f"CUDA out of memory during synthesis: {exc}"
                        ) from exc
                    raise
        if not audio_chunks:
            raise SynthesisError("No audio generated")
        audio = np.concatenate(audio_chunks)

        if output_path is not None:
            self._write_output_audio(output_path, audio)

        self._chunks_processed += 1
        self._total_chunk_time_ms += (time.perf_counter() - started_at) * 1000
        return audio

    def _pipeline_for_voice(self, voice: str):  # noqa: ANN202
        # KPipeline phonemizes in one language, so a British voice needs the
        # British G2P. Sibling pipelines share the loaded model.
        language_code = kokoro_language_code(voice)
        if language_code == kokoro_language_code(self._config.default_voice):
            return self._pipeline
        pipeline = self._language_pipelines.get(language_code)
        if pipeline is None:
            from kokoro import KPipeline

            model = self._shared.model if self._shared is not None else self._pipeline.model
            pipeline = KPipeline(lang_code=language_code, model=model)
            self._language_pipelines[language_code] = pipeline
        return pipeline

    def _run_pipeline(self, text: str, voice: str, speed: float) -> list[np.ndarray]:
        audio_chunks: list[np.ndarray] = []
        for _graphemes, _phonemes, audio in self._pipeline_for_voice(voice)(
            text,
            voice=self._voice_arg(voice),
            speed=speed,
        ):
            if audio is None:
                continue
            if hasattr(audio, "detach"):
                audio = audio.detach().cpu().numpy()
            audio_chunks.append(np.asarray(audio, dtype=np.float32).reshape(-1))
        return audio_chunks

    def _is_cuda_oom(self, exc: BaseException) -> bool:
        if self.provider != "cuda":
            return False
        cuda = getattr(sys.modules.get("torch"), "cuda", None)
        oom_error = getattr(cuda, "OutOfMemoryError", None)
        return (
            isinstance(oom_error, type) and isinstance(exc, oom_error)
        ) or is_memory_pressure_error(str(exc))

    def _release_cuda_memory(self) -> None:
        """Return the CUDA caching allocator's free blocks to the driver."""
        gc.collect()
        cuda = getattr(sys.modules.get("torch"), "cuda", None)
        try:
            cuda.empty_cache()
        except Exception:
            logger.debug("torch.cuda.empty_cache failed after OOM", exc_info=True)

    def iter_synthesize_batch(
        self,
        items: list[SynthesisItem],
        progress_callback: SynthesisProgressCallback | None = None,
    ) -> Iterator[SynthesisResult]:
        total = len(items)
        for offset, item in enumerate(items, start=1):
            try:
                audio = self.synthesize(
                    item.text,
                    item.voice,
                    item.speed,
                    item.output_path,
                )
                yield SynthesisResult(
                    audio=audio,
                    index=item.index,
                    duration_ms=int(len(audio) / self.sample_rate * 1000),
                    audio_path=item.output_path,
                )
            except Exception as exc:
                yield SynthesisResult(
                    audio=None,
                    index=item.index,
                    error=str(exc),
                    duration_ms=0,
                    audio_path=item.output_path,
                )
            if progress_callback is not None:
                progress_callback(offset, total)

    def get_metrics(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "chunks_processed": self.chunks_processed,
            "average_chunk_time_ms": self.average_chunk_time_ms,
        }


def register_package_kokoro_runtimes() -> None:
    """Register upstream package Kokoro runtime implementations."""
    KokoroRuntimeFactory.register("cpu", "kokoro", PackageKokoroRuntime)
    KokoroRuntimeFactory.register("cuda", "kokoro", PackageKokoroRuntime)
