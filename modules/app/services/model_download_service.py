"""Single-flight background downloader for managed model groups.

Packaged builds cannot run the source-checkout installer, so the desktop
frontend provisions model assets through this service instead: the runtime is
already baked into the bundle and only the Hugging Face snapshots (plus their
download receipts) are missing.
"""

from __future__ import annotations

import errno
import multiprocessing
import re
import threading
from typing import Any, Callable, Iterable

import requests
from huggingface_hub.errors import (
    GatedRepoError,
    HfHubHTTPError,
    LocalEntryNotFoundError,
    RepositoryNotFoundError,
)

from scripts import setup_runtime
from scripts.download_models import download_model_snapshot, resolve_model_groups

# Fail fast when the installer, adopt, or delete already holds the setup lock;
# the caller surfaces this as a 409 rather than queueing behind a long run.
_LOCK_ACQUIRE_TIMEOUT_SECONDS = 0.5


class ModelDownloadBusyError(RuntimeError):
    """A download is already running in this backend."""


def _download_worker(model_id: str, result: Any) -> None:
    try:
        download_model_snapshot(model_id)
        result.send(None)
    except BaseException as exc:
        result.send(_user_facing_error(exc, model_id))
    finally:
        result.close()


def _sanitized_error(exc: BaseException) -> str:
    message = re.sub(r"[\x00-\x1f\x7f]", " ", str(exc)).strip()
    return message[:200] or exc.__class__.__name__


def _user_facing_error(exc: BaseException, model_id: str | None) -> str:
    """Map raw download failures onto messages a user can act on."""
    subject = model_id or "a required model"
    if isinstance(exc, (GatedRepoError, RepositoryNotFoundError)):
        return (
            f"The model {subject} is not available on Hugging Face. "
            "Update Orpheus or try again later."
        )
    if isinstance(
        exc,
        (requests.ConnectionError, requests.Timeout, LocalEntryNotFoundError),
    ):
        return (
            "Could not reach huggingface.co. "
            "Check your internet connection and try again."
        )
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return f"Not enough disk space to download {subject}."
    if isinstance(exc, HfHubHTTPError):
        return f"Hugging Face rejected the download of {subject}: {_sanitized_error(exc)}"
    return f"Downloading {subject} failed: {_sanitized_error(exc)}"


def _idle_state() -> dict[str, Any]:
    return {
        "lifecycle": "idle",
        "profile": None,
        "model_groups": [],
        "total_models": 0,
        "completed_models": 0,
        "current_model_group": None,
        "current_model": None,
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
        downloader: Callable[[str], Any] | None = None,
        group_resolver: Callable[[Iterable[str]], tuple[str, ...]] = resolve_model_groups,
        lock_factory: Callable[[], Any] | None = None,
        on_terminal: Callable[[], None] | None = None,
    ) -> None:
        self._downloader = downloader or self._download_interruptibly
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
        self._cancel_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def cancel_requested(self) -> bool:
        """Allow cooperative download adapters to stop promptly."""
        return self._cancel_event.is_set()

    def status(self) -> dict[str, Any]:
        with self._mutex:
            return dict(self._state, model_groups=list(self._state["model_groups"]))

    def start(self, *, profile_id: str, model_groups: Iterable[str], configure_runtime: bool = True, cleaner_cuda_quantization: str = "4bit") -> dict[str, Any]:
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
            if self._state["lifecycle"] in {"running", "stopping"}:
                raise ModelDownloadBusyError("A model download is already running.")
            lock = self._lock_factory()
            lock.__enter__()
            self._cancel_event.clear()
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
                "cleaner_cuda_quantization": cleaner_cuda_quantization,
                "total_models": sum(len(models) for _, models in plan),
                "completed_models": 0,
                "current_model_group": groups[0] if groups else None,
                "current_model": None,
                "error": None,
            }
            self._thread = thread
            if not configure_runtime:
                self._state["configure_runtime"] = False
            try:
                thread.start()
            except Exception:
                self._thread = None
                self._state = _idle_state()
                lock.__exit__(None, None, None)
                raise
        return self.status()

    def shutdown(self, *, timeout_seconds: float = 2.0) -> bool:
        """Cancel the transfer and join; retain the lock until its worker exits."""
        self.request_cancel()
        with self._mutex:
            thread = self._thread
            running = self._state["lifecycle"] in {"running", "stopping"}
            if thread is None or not running:
                return True

        thread.join(timeout=max(0.0, float(timeout_seconds)))
        if thread.is_alive():
            return False
        return True

    def request_cancel(self) -> dict[str, Any]:
        """Request cancellation without blocking an API response on downloads."""
        with self._mutex:
            if self._state["lifecycle"] == "running":
                self._cancel_event.set()
                self._state.update(
                    lifecycle="stopping",
                    error="Cancellation requested; waiting for the downloader to stop.",
                )
            return dict(self._state, model_groups=list(self._state["model_groups"]))

    def _download_interruptibly(self, model_id: str) -> None:
        context = multiprocessing.get_context("spawn")
        reader, writer = context.Pipe(duplex=False)
        process = context.Process(target=_download_worker, args=(model_id, writer), daemon=True)
        started = False
        try:
            process.start()
            started = True
            writer.close()
            while process.is_alive():
                if self._cancel_event.wait(0.1):
                    process.terminate()
                    break
            process.join(timeout=1.0)
            if process.is_alive():
                process.kill()
                process.join()
            if self.cancel_requested:
                return
            if not reader.poll():
                raise RuntimeError("The download worker stopped unexpectedly. Try again.")
            message = reader.recv()
            if message:
                raise RuntimeError(message)
            if process.exitcode != 0:
                raise RuntimeError("The download worker stopped unexpectedly. Try again.")
        finally:
            if started and process.is_alive():
                process.terminate()
                process.join()
            reader.close()
            writer.close()
            if started:
                process.close()

    def _run(
        self,
        plan: list[tuple[str, tuple[str, ...]]],
        lock: Any,
    ) -> None:
        current_model: str | None = None
        failure: BaseException | None = None
        try:
            for group_id, models in plan:
                if self.cancel_requested:
                    break
                with self._mutex:
                    self._state["current_model_group"] = group_id
                for model_id in models:
                    if self.cancel_requested:
                        break
                    current_model = model_id
                    with self._mutex:
                        self._state["current_model"] = model_id
                    self._downloader(model_id)
                    if self.cancel_requested:
                        break
                    with self._mutex:
                        self._state["completed_models"] += 1
        except BaseException as exc:
            failure = exc
        try:
            lock.__exit__(None, None, None)
        except Exception as exc:
            failure = failure or exc

        current_thread = threading.current_thread()
        owns_terminal_state = False
        with self._mutex:
            if self._thread is current_thread:
                cancelled = self._cancel_event.is_set()
                self._state.update(
                    lifecycle=(
                        "cancelled"
                        if cancelled
                        else "failed"
                        if failure is not None
                        else "complete"
                    ),
                    error=(
                        None
                        if cancelled or failure is None
                        else _user_facing_error(failure, current_model)
                    ),
                    current_model_group=None,
                    current_model=None,
                )
                self._thread = None
                owns_terminal_state = True

        if owns_terminal_state:
            callback = self._on_terminal
            if callback is not None:
                try:
                    callback()
                except Exception:
                    pass
