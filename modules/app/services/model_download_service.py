"""Single-flight background downloader for managed model groups.

Packaged builds cannot run the source-checkout installer, so the desktop
frontend provisions model assets through this service instead: the runtime is
already baked into the bundle and only the Hugging Face snapshots (plus their
download receipts) are missing.
"""

from __future__ import annotations

import re
import threading
from typing import Any, Callable, Iterable

from scripts import setup_runtime
from scripts.download_models import download_model_snapshot, resolve_model_groups

# Fail fast when the installer, adopt, or delete already holds the setup lock;
# the caller surfaces this as a 409 rather than queueing behind a long run.
_LOCK_ACQUIRE_TIMEOUT_SECONDS = 0.5


class ModelDownloadBusyError(RuntimeError):
    """A download is already running in this backend."""


def _sanitized_error(exc: BaseException) -> str:
    message = re.sub(r"[\x00-\x1f\x7f]", " ", str(exc)).strip()
    return message[:200] or exc.__class__.__name__


def _idle_state() -> dict[str, Any]:
    return {
        "lifecycle": "idle",
        "profile": None,
        "model_groups": [],
        "total_models": 0,
        "completed_models": 0,
        "current_model_group": None,
        "error": None,
    }


class ModelDownloadService:
    """Download model groups on a worker thread while holding the setup lock.

    Holding ``SetupLock`` for the whole download makes adopt, delete, and the
    source-checkout installer conflict with it symmetrically: they fail with
    "setup in progress" while a download runs, and starting a download fails
    the same way while any of them holds the lock.
    """

    def __init__(
        self,
        *,
        downloader: Callable[[str], Any] = download_model_snapshot,
        group_resolver: Callable[[Iterable[str]], tuple[str, ...]] = resolve_model_groups,
        lock_factory: Callable[[], Any] | None = None,
        on_terminal: Callable[[], None] | None = None,
    ) -> None:
        self._downloader = downloader
        self._group_resolver = group_resolver
        self._lock_factory = lock_factory or (
            lambda: setup_runtime.SetupLock(
                setup_runtime.default_state_path(),
                timeout_seconds=_LOCK_ACQUIRE_TIMEOUT_SECONDS,
            )
        )
        self._on_terminal = on_terminal
        self._mutex = threading.Lock()
        self._state = _idle_state()

    def status(self) -> dict[str, Any]:
        with self._mutex:
            return dict(self._state, model_groups=list(self._state["model_groups"]))

    def start(self, *, profile_id: str, model_groups: Iterable[str]) -> dict[str, Any]:
        """Begin a download; raises when busy or when the setup lock is held.

        The lock is acquired synchronously so callers get an immediate
        conflict error instead of a download that fails moments later.
        """
        plan = [
            (group_id, self._group_resolver((group_id,)))
            for group_id in model_groups
        ]
        groups = [group_id for group_id, _ in plan]

        with self._mutex:
            if self._state["lifecycle"] == "running":
                raise ModelDownloadBusyError("A model download is already running.")
            lock = self._lock_factory()
            lock.__enter__()
            try:
                thread = threading.Thread(
                    target=self._run,
                    args=(plan, lock),
                    name="setup-model-download",
                    daemon=True,
                )
            except Exception:
                lock.__exit__(None, None, None)
                raise
            self._state = {
                "lifecycle": "running",
                "profile": profile_id,
                "model_groups": groups,
                "total_models": sum(len(models) for _, models in plan),
                "completed_models": 0,
                "current_model_group": groups[0] if groups else None,
                "error": None,
            }
        thread.start()
        return self.status()

    def _run(
        self,
        plan: list[tuple[str, tuple[str, ...]]],
        lock: Any,
    ) -> None:
        try:
            try:
                for group_id, models in plan:
                    with self._mutex:
                        self._state["current_model_group"] = group_id
                    for model_id in models:
                        self._downloader(model_id)
                        with self._mutex:
                            self._state["completed_models"] += 1
            finally:
                lock.__exit__(None, None, None)
            with self._mutex:
                self._state.update(lifecycle="complete", current_model_group=None)
        except Exception as exc:
            with self._mutex:
                self._state.update(
                    lifecycle="failed",
                    error=_sanitized_error(exc),
                    current_model_group=None,
                )
        finally:
            callback = self._on_terminal
            if callback is not None:
                try:
                    callback()
                except Exception:
                    pass
