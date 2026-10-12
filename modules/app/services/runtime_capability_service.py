"""Bounded runtime capability probes for optional MLX and CUDA dependencies."""

from __future__ import annotations

import io
import json
import multiprocessing
from multiprocessing.connection import Connection
import os
import subprocess
import sys
import threading
import time
from typing import Any

from modules.subprocess_utils import background_process_kwargs

from modules.tts.cleaner_backends.registry import (
    available_cleaner_backends,
    get_cleaner_backend,
    is_local_cleaner_backend,
    validate_cleaner_backend,
)


ProbeResult = dict[str, Any]

# Limit concurrent native runtime startup across frozen service instances.
_FROZEN_PROBE_LOCK = threading.RLock()


def _frozen_probe_worker(script: str, connection: Connection) -> None:
    try:
        connection.send(RuntimeCapabilityService()._run_in_process_probe(script))
    finally:
        connection.close()


class RuntimeCapabilityService:
    """Runs short-lived subprocess probes and caches their results."""

    _TTS_PROBE = """
import json
try:
    from modules.tts.mlx_compat import patch_espeakng_loader_paths
    patch_espeakng_loader_paths()
    import mlx.core  # noqa: F401
    import mlx.nn  # noqa: F401
    from mlx_audio.tts.generate import generate_audio  # noqa: F401
    from mlx_audio.tts.models.base import GenerationResult  # noqa: F401
    import mlx_audio.tts.models.kokoro  # noqa: F401
    print(json.dumps({"available": True}))
except Exception as exc:
    print(json.dumps({"available": False, "error": str(exc)}))
"""

    _CLEANER_MLX_PROBE = """
import json
try:
    import mlx.core  # noqa: F401
    import mlx_lm  # noqa: F401
    print(json.dumps({"available": True}))
except Exception as exc:
    print(json.dumps({"available": False, "error": str(exc)}))
"""

    _TTS_PACKAGE_PROBE = """
import json
try:
    import torch  # noqa: F401
    from kokoro import KModel, KPipeline  # noqa: F401
    from modules.tts.runtime_spec import TTSRuntimeSpec
    device = "cuda" if TTSRuntimeSpec.cuda_available_on_current_platform() else "cpu"
    print(json.dumps({"available": True, "device": device}))
except Exception as exc:
    print(json.dumps({"available": False, "error": str(exc)}))
"""

    _CLEANER_CUDA_PROBE = """
import json
import os
try:
    import torch
    from modules.runtime_profiles import cuda_hardware_error, CUDA_CLEANER_UNAVAILABLE_REASON
    if not torch.cuda.is_available() or cuda_hardware_error() is not None:
        print(json.dumps({
            "available": False,
            "error": CUDA_CLEANER_UNAVAILABLE_REASON,
        }))
    else:
        import transformers  # noqa: F401
        quant = globals().get("CLEANER_CUDA_QUANTIZATION") or os.getenv("CLEANER_CUDA_QUANTIZATION", "4bit").strip().lower() or "4bit"
        if quant in {"4bit", "8bit"}:
            import bitsandbytes  # noqa: F401
        print(json.dumps({"available": True}))
except Exception as exc:
    print(json.dumps({"available": False, "error": str(exc)}))
"""

    _CLEANER_PROBE_SCRIPTS: dict[str, str] = {
        "mlx": _CLEANER_MLX_PROBE,
        "cuda": _CLEANER_CUDA_PROBE,
    }

    def __init__(
        self,
        *,
        cache_ttl_seconds: float = 60.0,
        timeout_seconds: float | None = None,
        lock_timeout_seconds: float = 120.0,
    ) -> None:
        self._cache_ttl_seconds = cache_ttl_seconds
        self._timeout_seconds = (
            (120.0 if getattr(sys, "frozen", False) else 15.0)
            if timeout_seconds is None else timeout_seconds
        )
        self._lock_timeout_seconds = lock_timeout_seconds
        self._cache: dict[str, tuple[float, ProbeResult]] = {}
        self._lock = threading.Lock()
        self._refresh_in_progress = False

    def _cleaner_cache_key(self, backend: str, cleaner_cuda_quantization: str | None = None) -> str:
        normalized = validate_cleaner_backend(backend)
        suffix = f":{self._cuda_quantization(cleaner_cuda_quantization)}" if normalized == "cuda" else ""
        return f"cleaner:{normalized}{suffix}"

    def _cuda_quantization(self, requested: str | None = None) -> str:
        if requested is not None:
            if requested not in {"4bit", "8bit", "fp16"}:
                raise ValueError("Unsupported cleaner quantization.")
            return requested
        return os.getenv("CLEANER_CUDA_QUANTIZATION", "4bit").strip().lower() or "4bit"

    def _cached_result(self, probe_kind: str) -> ProbeResult | None:
        with self._lock:
            cached = self._cache.get(probe_kind)
            if cached is None:
                return None

            expires_at, result = cached
            if time.monotonic() >= expires_at:
                return None

            cached_result = dict(result)
            cached_result["cache_hit"] = True
            return cached_result

    def _store_result(self, probe_kind: str, result: ProbeResult) -> ProbeResult:
        stored = dict(result)
        stored["cache_hit"] = False
        with self._lock:
            self._cache[probe_kind] = (
                time.monotonic() + (0.0 if stored.get("timed_out") else self._cache_ttl_seconds),
                stored,
            )
        return dict(stored)

    def _snapshot_probe_kinds(self, primary_runtime: str = "mlx") -> tuple[str, ...]:
        # MLX reports its own TTS and cleaner checks; every other runtime uses
        # the PyTorch package TTS check and the CUDA cleaner check below.
        kinds = ["tts", "cleaner"] if primary_runtime == "mlx" else ["tts-package"]
        for backend_name in available_cleaner_backends():
            if is_local_cleaner_backend(backend_name):
                kinds.append(self._cleaner_cache_key(backend_name))
        return tuple(kinds)

    def _store_cleaner_backend_result(
        self,
        backend: str,
        result: ProbeResult,
        cleaner_cuda_quantization: str | None = None,
    ) -> ProbeResult:
        normalized = validate_cleaner_backend(backend)
        cache_key = self._cleaner_cache_key(normalized, cleaner_cuda_quantization)
        stored = self._store_result(cache_key, result)
        if normalized == "mlx":
            legacy = dict(stored)
            with self._lock:
                self._cache["cleaner"] = (self._cache[cache_key][0], legacy)
        return stored

    def _get_snapshot_cache_state(
        self, primary_runtime: str = "mlx",
    ) -> tuple[dict[str, ProbeResult | None], str, bool]:
        now = time.monotonic()
        with self._lock:
            refresh_in_progress = self._refresh_in_progress
            results: dict[str, ProbeResult | None] = {}
            states: list[str] = []
            for probe_kind in self._snapshot_probe_kinds(primary_runtime):
                cached = self._cache.get(probe_kind)
                if cached is None:
                    results[probe_kind] = None
                    states.append("missing")
                    continue

                expires_at, result = cached
                results[probe_kind] = dict(result)
                if now < expires_at:
                    states.append("fresh")
                else:
                    states.append("stale")

        if all(state == "fresh" for state in states):
            overall_state = "fresh"
        elif any(state in {"fresh", "stale"} for state in states):
            overall_state = "stale"
        else:
            overall_state = "pending"
        return results, overall_state, refresh_in_progress

    def _read_cached_cleaner_backend_result(self, backend: str) -> ProbeResult | None:
        cache_key = self._cleaner_cache_key(validate_cleaner_backend(backend))
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached is None:
                return None
            return dict(cached[1])

    def get_cached_cleaner_backend_availability(self) -> dict[str, bool]:
        """Return cached local cleaner availability without running probes."""
        availability: dict[str, bool] = {}
        for backend_name in available_cleaner_backends():
            if not is_local_cleaner_backend(backend_name):
                continue
            if not get_cleaner_backend(backend_name).requires_model:
                availability[backend_name] = True
                continue
            cached = self._read_cached_cleaner_backend_result(backend_name)
            availability[backend_name] = (
                bool(cached.get("available", False)) if cached is not None else False
            )
        return availability

    def _probe_payload(self, result: ProbeResult | None) -> dict[str, Any]:
        if result is None:
            return {
                "available": False,
                "latency_ms": None,
                "error": None,
            }
        return {
            "available": bool(result.get("available", False)),
            "latency_ms": result.get("latency_ms"),
            "error": result.get("error"),
        }

    def _refresh_runtime_snapshot(self, primary_runtime: str = "mlx") -> None:
        try:
            if primary_runtime == "mlx":
                self.probe_mlx_tts_runtime()
            else:
                self.probe_package_tts_runtime()
            for backend_name in available_cleaner_backends():
                if not is_local_cleaner_backend(backend_name):
                    continue
                self.probe_cleaner_backend(backend_name)
        finally:
            with self._lock:
                self._refresh_in_progress = False

    def _ensure_background_refresh(self, primary_runtime: str = "mlx") -> bool:
        with self._lock:
            if self._refresh_in_progress:
                return False
            self._refresh_in_progress = True

        try:
            thread = threading.Thread(
                target=self._refresh_runtime_snapshot,
                args=(primary_runtime,),
                name="runtime-capability-refresh",
                daemon=True,
            )
            thread.start()
        except BaseException:
            with self._lock:
                self._refresh_in_progress = False
            raise
        return True

    def _run_probe(self, script: str, timeout_seconds: float) -> ProbeResult:
        started_at = time.perf_counter()
        if getattr(sys, "frozen", False):
            try:
                payload = self._run_frozen_probe(script, timeout_seconds)
                return {
                    **({"results": payload["results"]} if isinstance(payload.get("results"), dict) else {}),
                    **({"device": payload["device"]} if payload.get("device") in {"cpu", "cuda"} else {}),
                    "available": bool(payload.get("available")),
                    "latency_ms": round((time.perf_counter() - started_at) * 1000, 2),
                    "error": payload.get("error"),
                    "cache_hit": False,
                }
            except Exception as exc:
                return {
                    "available": False,
                    "latency_ms": round((time.perf_counter() - started_at) * 1000, 2),
                    "error": str(exc),
                    "cache_hit": False,
                    **({"timed_out": True} if isinstance(exc, TimeoutError) else {}),
                }

        try:
            completed = subprocess.run(
                [sys.executable, "-c", script],
                **background_process_kwargs(),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except (subprocess.TimeoutExpired, TimeoutError) as exc:
            return {
                "available": False,
                "latency_ms": round((time.perf_counter() - started_at) * 1000, 2),
                "error": str(exc),
                "cache_hit": False,
                "timed_out": True,
            }
        except Exception as exc:
            return {
                "available": False,
                "latency_ms": round((time.perf_counter() - started_at) * 1000, 2),
                "error": str(exc),
                "cache_hit": False,
            }

        latency_ms = round((time.perf_counter() - started_at) * 1000, 2)
        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()

        if not stdout:
            return {
                "available": False,
                "latency_ms": latency_ms,
                "error": stderr or f"probe exited with code {completed.returncode}",
                "cache_hit": False,
            }

        try:
            payload = json.loads(stdout.splitlines()[-1])
        except json.JSONDecodeError:
            return {
                "available": False,
                "latency_ms": latency_ms,
                "error": stdout or stderr or "invalid probe output",
                "cache_hit": False,
            }

        return {
            **({"results": payload["results"]} if isinstance(payload.get("results"), dict) else {}),
            **({"device": payload["device"]} if payload.get("device") in {"cpu", "cuda"} else {}),
            "available": bool(payload.get("available")),
            "latency_ms": latency_ms,
            "error": payload.get("error"),
            "cache_hit": False,
        }

    def _run_frozen_probe(self, script: str, timeout_seconds: float) -> ProbeResult:
        if not _FROZEN_PROBE_LOCK.acquire(timeout=max(0.0, self._lock_timeout_seconds)):
            raise TimeoutError(f"Runtime probe lock wait timed out after {self._lock_timeout_seconds} seconds")
        deadline = time.monotonic() + timeout_seconds
        receiver = sender = worker = None
        try:
            context = multiprocessing.get_context("spawn")
            receiver, sender = context.Pipe(duplex=False)
            worker = context.Process(target=_frozen_probe_worker, args=(script, sender))
            # Native startup must not hold a closing desktop backend alive.
            worker.daemon = True
            worker.start()
            sender.close()
            if not receiver.poll(max(0.0, deadline - time.monotonic())):
                raise TimeoutError(f"Runtime probe timed out after {timeout_seconds} seconds")
            try:
                return receiver.recv()
            except EOFError as exc:
                raise RuntimeError("Runtime probe exited without a result") from exc
        finally:
            try:
                if worker is not None and worker.pid is not None:
                    worker.join(timeout=max(0.0, deadline - time.monotonic()))
                    if worker.is_alive():
                        worker.terminate()
                        worker.join(timeout=0.5)
                    if worker.is_alive():
                        worker.kill()
                        worker.join(timeout=0.5)
                if receiver is not None:
                    receiver.close()
                if sender is not None:
                    sender.close()
            finally:
                _FROZEN_PROBE_LOCK.release()

    def _run_in_process_probe(self, script: str) -> ProbeResult:
        """Execute a probe script in-process without process-global stdout.

        Packaged windowed backends often start with ``sys.stdout is None``.
        Setup status and ``/api/system/capabilities`` also probe concurrently.
        ``contextlib.redirect_stdout`` is process-global and not thread-safe:
        overlapping redirects produce empty capture and the exact error
        ``probe produced no output`` even when the import path succeeds.

        Capture via a namespace-local ``print`` so each concurrent probe has
        its own buffer and scripts remain portable with the subprocess path.
        """
        stdout = io.StringIO()
        stderr = io.StringIO()

        def _capture_print(*args: Any, **kwargs: Any) -> None:
            file_obj = kwargs.get("file", stdout)
            if file_obj in (None, sys.stdout):
                file_obj = stdout
            elif file_obj is sys.stderr:
                file_obj = stderr
            sep = str(kwargs.get("sep", " "))
            end = str(kwargs.get("end", "\n"))
            file_obj.write(sep.join(str(arg) for arg in args) + end)

        namespace: dict[str, Any] = {
            "__name__": "__runtime_capability_probe__",
            "print": _capture_print,
        }
        try:
            exec(script, namespace)  # noqa: S102
        except Exception as exc:
            return {
                "available": False,
                "error": str(exc) or "probe raised before producing output",
            }

        output = stdout.getvalue().strip()
        if not output:
            return {
                "available": False,
                "error": stderr.getvalue().strip() or "probe produced no output",
            }

        try:
            payload = json.loads(output.splitlines()[-1])
        except json.JSONDecodeError:
            return {
                "available": False,
                "error": output or stderr.getvalue().strip() or "invalid probe output",
            }

        return {
            **({"results": payload["results"]} if isinstance(payload.get("results"), dict) else {}),
            **({"device": payload["device"]} if payload.get("device") in {"cpu", "cuda"} else {}),
            "available": bool(payload.get("available")),
            "error": payload.get("error"),
        }

    def probe_mlx_tts_runtime(self) -> ProbeResult:
        cached = self._cached_result("tts")
        if cached is not None:
            return cached
        return self._store_result(
            "tts",
            self._run_probe(self._TTS_PROBE, self._timeout_seconds),
        )

    def probe_package_tts_runtime(self) -> ProbeResult:
        """Probe the PyTorch-backed Kokoro package runtime without loading a model."""
        cached = self._cached_result("tts-package")
        if cached is not None:
            return cached
        return self._store_result(
            "tts-package",
            self._run_probe(self._TTS_PACKAGE_PROBE, self._timeout_seconds),
        )

    def probe_package_and_cuda_cleaner_runtime(
        self, *, cleaner_cuda_quantization: str | None = None,
    ) -> tuple[ProbeResult, ProbeResult]:
        """Share one torch import across the two configured CUDA checks."""
        precision = self._cuda_quantization(cleaner_cuda_quantization)
        cleaner_key = self._cleaner_cache_key("cuda", precision)
        tts = self._cached_result("tts-package")
        cleaner = self._cached_result(cleaner_key)
        if tts is not None or cleaner is not None:
            return (
                tts if tts is not None else self.probe_package_tts_runtime(),
                cleaner if cleaner is not None else self.probe_cleaner_backend(
                    "cuda", cleaner_cuda_quantization=precision,
                ),
            )
        # Each original script catches its own import/device errors. Capture its
        # JSON independently so a failed optional cleaner never masks TTS.
        script = (
            "import json\nresults = {}\n"
            f"scripts = {dict(tts=self._TTS_PACKAGE_PROBE, cleaner=self._CLEANER_CUDA_PROBE)!r}\n"
            "for name, source in scripts.items():\n"
            "    output = []\n"
            f"    namespace = {{'CLEANER_CUDA_QUANTIZATION': {precision!r}, 'print': output.append}}\n"
            "    exec(source, namespace)\n"
            "    results[name] = json.loads(output[-1])\n"
            "print(json.dumps({'available': all(r.get('available') for r in results.values()), 'results': results}))\n"
        )
        result = self._run_probe(script, 2 * self._timeout_seconds)
        results = result.get("results")
        payloads = [
            results.get(name) if isinstance(results, dict) else None
            for name in ("tts", "cleaner")
        ]
        if not all(isinstance(payload, dict) and isinstance(payload.get("available"), bool) for payload in payloads):
            # A crash or timeout in the shared process (for example a failing GPU
            # driver in the cleaner check) must not mark CPU narration unavailable.
            return (
                self.probe_package_tts_runtime(),
                self.probe_cleaner_backend("cuda", cleaner_cuda_quantization=precision),
            )
        checks = []
        for payload in payloads:
            checks.append({**result, **payload})
            checks[-1].pop("results", None)
        return (
            self._store_result("tts-package", checks[0]),
            self._store_cleaner_backend_result("cuda", checks[1], precision),
        )

    def probe_cleaner_backend(self, backend: str, *, cleaner_cuda_quantization: str | None = None) -> ProbeResult:
        """Probe one registered cleaner backend's local runtime availability."""
        normalized = validate_cleaner_backend(backend)
        precision = self._cuda_quantization(cleaner_cuda_quantization) if normalized == "cuda" else None
        cache_key = self._cleaner_cache_key(normalized, precision)
        cached = self._cached_result(cache_key)
        if cached is not None:
            return cached

        strategy = get_cleaner_backend(normalized)
        if strategy.requires_api_key or not strategy.requires_model:
            return self._store_result(
                cache_key,
                {
                    "available": True,
                    "latency_ms": 0.0,
                    "error": None,
                    "cache_hit": False,
                },
            )

        script = self._CLEANER_PROBE_SCRIPTS.get(normalized)
        if script is None:
            return self._store_result(
                cache_key,
                {
                    "available": False,
                    "latency_ms": 0.0,
                    "error": f"No runtime probe configured for cleaner backend '{normalized}'.",
                    "cache_hit": False,
                },
            )

        if normalized == "cuda":
            script = f"CLEANER_CUDA_QUANTIZATION = {precision!r}\n" + script
        return self._store_cleaner_backend_result(
            normalized,
            self._run_probe(script, self._timeout_seconds),
            precision,
        )

    def get_system_runtime_snapshot(self, primary_runtime: str = "mlx") -> dict[str, Any]:
        """Report the TTS and local-cleaner checks for the selected runtime.

        On CUDA and CPU runtimes the MLX packages are absent by design, so their
        import failure is not a reason to show; ``tts`` and ``cleaner`` report
        the PyTorch package and CUDA cleaner checks instead.
        """
        mlx = primary_runtime == "mlx"
        results, state, refresh_in_progress = self._get_snapshot_cache_state(primary_runtime)
        if state != "fresh":
            refresh_in_progress = refresh_in_progress or self._ensure_background_refresh(primary_runtime)
        if mlx:
            cleaner_key = "cleaner"
        elif "cuda" in available_cleaner_backends():
            cleaner_key = self._cleaner_cache_key("cuda")
        else:
            cleaner_key = None
        return {
            "mode": "import",
            "cache_ttl_seconds": self._cache_ttl_seconds,
            "cache_hit": state == "fresh",
            "state": state,
            "refresh_in_progress": refresh_in_progress,
            "tts": self._probe_payload(results.get("tts" if mlx else "tts-package")),
            "cleaner": self._probe_payload(results.get(cleaner_key) if cleaner_key else None),
        }