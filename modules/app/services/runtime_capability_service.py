"""Bounded runtime capability probes for optional MLX dependencies."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import threading
import time
from typing import Any


ProbeResult = dict[str, Any]


class RuntimeCapabilityService:
    """Runs short-lived subprocess probes and caches their results."""

    _TTS_PROBE = """
import json
try:
    from modules.tts.mlx_compat import patch_frozen_espeakng_loader_paths
    patch_frozen_espeakng_loader_paths()
    import mlx.core  # noqa: F401
    import mlx.nn  # noqa: F401
    from mlx_audio.tts.generate import generate_audio  # noqa: F401
    from mlx_audio.tts.models.base import GenerationResult  # noqa: F401
    import mlx_audio.tts.models.kokoro  # noqa: F401
    print(json.dumps({"available": True}))
except Exception as exc:
    print(json.dumps({"available": False, "error": str(exc)}))
"""

    _CLEANER_PROBE = """
import json
try:
    import mlx.core  # noqa: F401
    import mlx_lm  # noqa: F401
    print(json.dumps({"available": True}))
except Exception as exc:
    print(json.dumps({"available": False, "error": str(exc)}))
"""

    def __init__(
        self,
        *,
        cache_ttl_seconds: float = 60.0,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._cache_ttl_seconds = cache_ttl_seconds
        self._timeout_seconds = timeout_seconds
        self._cache: dict[str, tuple[float, ProbeResult]] = {}
        self._lock = threading.Lock()
        self._refresh_in_progress = False

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
                time.monotonic() + self._cache_ttl_seconds,
                stored,
            )
        return dict(stored)

    def _store_snapshot_results(
        self,
        *,
        tts: ProbeResult,
        cleaner: ProbeResult,
    ) -> None:
        expires_at = time.monotonic() + self._cache_ttl_seconds
        stored_tts = dict(tts)
        stored_tts["cache_hit"] = False
        stored_cleaner = dict(cleaner)
        stored_cleaner["cache_hit"] = False
        with self._lock:
            self._cache["tts"] = (expires_at, stored_tts)
            self._cache["cleaner"] = (expires_at, stored_cleaner)

    def _get_snapshot_cache_state(self) -> tuple[dict[str, ProbeResult | None], str, bool]:
        now = time.monotonic()
        with self._lock:
            refresh_in_progress = self._refresh_in_progress
            results: dict[str, ProbeResult | None] = {}
            states: list[str] = []
            for probe_kind in ("tts", "cleaner"):
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

    def _refresh_runtime_snapshot(self) -> None:
        try:
            tts = self._run_probe(self._TTS_PROBE, self._timeout_seconds)
            cleaner = self._run_probe(self._CLEANER_PROBE, self._timeout_seconds)
            self._store_snapshot_results(tts=tts, cleaner=cleaner)
        finally:
            with self._lock:
                self._refresh_in_progress = False

    def _ensure_background_refresh(self) -> bool:
        with self._lock:
            if self._refresh_in_progress:
                return False
            self._refresh_in_progress = True

        thread = threading.Thread(
            target=self._refresh_runtime_snapshot,
            name="runtime-capability-refresh",
            daemon=True,
        )
        thread.start()
        return True

    def _run_probe(self, script: str, timeout_seconds: float) -> ProbeResult:
        started_at = time.perf_counter()
        if getattr(sys, "frozen", False):
            try:
                payload = self._run_in_process_probe(script)
                return {
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
                }

        try:
            completed = subprocess.run(
                [sys.executable, "-c", script],
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
            "available": bool(payload.get("available")),
            "latency_ms": latency_ms,
            "error": payload.get("error"),
            "cache_hit": False,
        }

    def _run_in_process_probe(self, script: str) -> ProbeResult:
        stdout = io.StringIO()
        stderr = io.StringIO()
        namespace: dict[str, Any] = {"__name__": "__runtime_capability_probe__"}
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exec(script, namespace)  # noqa: S102

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

    def probe_mlx_cleaner_runtime(self) -> ProbeResult:
        cached = self._cached_result("cleaner")
        if cached is not None:
            return cached
        return self._store_result(
            "cleaner",
            self._run_probe(self._CLEANER_PROBE, self._timeout_seconds),
        )

    def get_system_runtime_snapshot(self) -> dict[str, Any]:
        results, state, refresh_in_progress = self._get_snapshot_cache_state()
        if state != "fresh":
            refresh_in_progress = refresh_in_progress or self._ensure_background_refresh()
        return {
            "mode": "import",
            "cache_ttl_seconds": self._cache_ttl_seconds,
            "cache_hit": state == "fresh",
            "state": state,
            "refresh_in_progress": refresh_in_progress,
            "tts": self._probe_payload(results.get("tts")),
            "cleaner": self._probe_payload(results.get("cleaner")),
        }
