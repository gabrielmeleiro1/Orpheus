"""Tests for cross-process cleaner API global limiter."""

from __future__ import annotations

import asyncio
from concurrent.futures import ProcessPoolExecutor
import multiprocessing as mp
import time

import pytest

from modules.tts.api_rate_limit import GlobalAPILimiter, acquire_global_limiter_async


_SPAWN_PROBE_SEMAPHORE = None


def _init_spawn_probe(semaphore) -> None:
    global _SPAWN_PROBE_SEMAPHORE
    _SPAWN_PROBE_SEMAPHORE = semaphore


def _worker_probe_initialized_spawn_semaphore() -> bool:
    semaphore = _SPAWN_PROBE_SEMAPHORE
    if semaphore is None:
        return False
    acquired = semaphore.acquire(False)
    if acquired:
        semaphore.release()
    return bool(acquired)


def _worker_hold_semaphore(
    semaphore,
    active_value,
    peak_value,
    lock,
    hold_seconds: float,
) -> None:
    semaphore.acquire()
    try:
        with lock:
            active = int(active_value.get()) + 1
            active_value.set(active)
            peak_value.set(max(int(peak_value.get()), active))
        time.sleep(hold_seconds)
    finally:
        with lock:
            active_value.set(max(0, int(active_value.get()) - 1))
        semaphore.release()


def _worker_hold_raw_value_semaphore(
    semaphore,
    active_value,
    peak_value,
    lock,
    hold_seconds: float,
) -> None:
    semaphore.acquire()
    try:
        with lock:
            active = int(active_value.value) + 1
            active_value.value = active
            peak_value.value = max(int(peak_value.value), active)
        time.sleep(hold_seconds)
    finally:
        with lock:
            active_value.value = max(0, int(active_value.value) - 1)
        semaphore.release()


@pytest.mark.parallel
def test_global_api_limiter_enforces_cross_process_cap() -> None:
    limiter = GlobalAPILimiter(max_concurrency=2)
    try:
        manager = mp.Manager()
    except (EOFError, OSError) as exc:
        limiter.shutdown()
        pytest.skip(f"multiprocessing manager unavailable: {exc}")
    active = manager.Value("i", 0)
    peak = manager.Value("i", 0)
    lock = manager.Lock()

    processes = [
        mp.Process(
            target=_worker_hold_semaphore,
            args=(limiter.semaphore, active, peak, lock, 0.12),
        )
        for _ in range(6)
    ]

    try:
        for proc in processes:
            proc.start()
        for proc in processes:
            proc.join(timeout=15)
            assert proc.exitcode == 0

        assert int(peak.get()) <= 2
    finally:
        for proc in processes:
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=1)
        limiter.shutdown()
        manager.shutdown()


@pytest.mark.asyncio
async def test_async_limiter_context_noop_when_none() -> None:
    async with acquire_global_limiter_async(None):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_async_limiter_context_acquires_and_releases() -> None:
    limiter = GlobalAPILimiter(max_concurrency=1)
    semaphore = limiter.semaphore

    acquired_while_held = {"value": False}

    async with acquire_global_limiter_async(semaphore):
        acquired_while_held["value"] = bool(await asyncio.to_thread(semaphore.acquire, False))
        if acquired_while_held["value"]:
            semaphore.release()

    assert acquired_while_held["value"] is False
    assert bool(await asyncio.to_thread(semaphore.acquire, False)) is True
    semaphore.release()
    limiter.shutdown()


@pytest.mark.parallel
def test_global_api_limiter_uses_process_semaphore_without_manager(monkeypatch) -> None:
    import modules.tts.api_rate_limit as api_rate_limit

    manager_called = {"value": False}

    def _manager_unexpected():
        manager_called["value"] = True
        raise AssertionError("multiprocessing.Manager should not be used in frozen runtime")

    monkeypatch.setattr(api_rate_limit.mp, "Manager", _manager_unexpected)

    limiter = api_rate_limit.GlobalAPILimiter(max_concurrency=3)

    assert manager_called["value"] is False
    assert limiter.semaphore is not None
    assert limiter.snapshot()["max_concurrency"] == 3

    active = mp.Value("i", 0)
    peak = mp.Value("i", 0)
    lock = mp.Lock()
    processes = [
        mp.Process(
            target=_worker_hold_raw_value_semaphore,
            args=(limiter.semaphore, active, peak, lock, 0.12),
        )
        for _ in range(8)
    ]

    try:
        for proc in processes:
            proc.start()
        for proc in processes:
            proc.join(timeout=15)
            assert proc.exitcode == 0

        assert int(peak.value) <= 3
    finally:
        for proc in processes:
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=1)
        limiter.shutdown()


@pytest.mark.parallel
def test_global_api_limiter_semaphore_can_be_sent_to_spawn_pool(monkeypatch) -> None:
    import modules.tts.api_rate_limit as api_rate_limit

    # Simulate Linux's fork default even when this test suite runs on macOS.
    def _fork_default_semaphore(value):
        semaphore = mp.get_context("spawn").BoundedSemaphore(value)
        semaphore._is_fork_ctx = True
        return semaphore

    monkeypatch.setattr(
        api_rate_limit.mp,
        "BoundedSemaphore",
        _fork_default_semaphore,
    )
    limiter = GlobalAPILimiter(max_concurrency=1)
    try:
        # A semaphore made by the default (fork) context cannot be pickled for
        # a spawn pool on Linux. The limiter must explicitly use spawn.
        context = mp.get_context("spawn")
        with ProcessPoolExecutor(
            max_workers=1,
            mp_context=context,
            initializer=_init_spawn_probe,
            initargs=(limiter.semaphore,),
        ) as pool:
            assert pool.submit(_worker_probe_initialized_spawn_semaphore).result(timeout=15) is True
    finally:
        limiter.shutdown()


@pytest.mark.asyncio
async def test_async_limiter_cancellation_releases_late_acquire_once() -> None:
    limiter = GlobalAPILimiter(max_concurrency=1)
    semaphore = limiter.semaphore
    assert semaphore.acquire(False) is True

    async def _wait_for_limiter() -> None:
        async with acquire_global_limiter_async(semaphore):
            pass

    task = asyncio.create_task(_wait_for_limiter())
    await asyncio.sleep(0.05)
    task.cancel()
    semaphore.release()

    with pytest.raises(asyncio.CancelledError):
        await task

    # The worker thread acquires after cancellation, then must release exactly
    # once before cancellation propagates to the caller.
    assert semaphore.acquire(False) is True
    semaphore.release()
    limiter.shutdown()


@pytest.mark.asyncio
async def test_async_limiter_repeated_cancellation_releases_late_acquire() -> None:
    limiter = GlobalAPILimiter(max_concurrency=1)
    semaphore = limiter.semaphore
    assert semaphore.acquire(False) is True

    async def _wait_for_limiter() -> None:
        async with acquire_global_limiter_async(semaphore):
            pass

    task = asyncio.create_task(_wait_for_limiter())
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.sleep(0.05)
    task.cancel()
    semaphore.release()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert semaphore.acquire(False) is True
    semaphore.release()
    limiter.shutdown()


@pytest.mark.asyncio
async def test_async_limiter_false_acquire_raises_without_release() -> None:
    class _FalseAcquire:
        def __init__(self) -> None:
            self.release_count = 0

        def acquire(self, block=True, timeout=None) -> bool:
            return False

        def release(self) -> None:
            self.release_count += 1

    semaphore = _FalseAcquire()
    with pytest.raises(TimeoutError):
        async with acquire_global_limiter_async(semaphore, timeout=0.01):
            pass
    assert semaphore.release_count == 0


@pytest.mark.asyncio
async def test_async_limiter_does_not_retry_internal_type_error() -> None:
    class _InternalTypeErrorAcquire:
        def __init__(self) -> None:
            self.acquire_calls = 0
            self.release_calls = 0

        def acquire(self, block=True, timeout=None) -> bool:
            self.acquire_calls += 1
            raise TypeError("internal acquire failure")

        def release(self) -> None:
            self.release_calls += 1

    semaphore = _InternalTypeErrorAcquire()
    with pytest.raises(TypeError, match="internal acquire failure"):
        async with acquire_global_limiter_async(semaphore, timeout=0.01):
            pass
    assert semaphore.acquire_calls == 1
    assert semaphore.release_calls == 0


@pytest.mark.asyncio
async def test_async_limiter_supports_no_timeout_acquire_returning_none() -> None:
    class _NoTimeoutAcquire:
        def __init__(self) -> None:
            self.acquire_calls = 0
            self.release_calls = 0

        def acquire(self):
            self.acquire_calls += 1
            return None

        def release(self) -> None:
            self.release_calls += 1

    semaphore = _NoTimeoutAcquire()
    async with acquire_global_limiter_async(semaphore):
        pass
    assert semaphore.acquire_calls == 1
    assert semaphore.release_calls == 1


@pytest.mark.asyncio
async def test_limiter_snapshot_counts_only_requests_that_waited() -> None:
    limiter = GlobalAPILimiter(max_concurrency=1)
    semaphore = limiter.semaphore
    async with acquire_global_limiter_async(semaphore):
        pass
    assert limiter.snapshot()["wait_count"] == 0

    assert semaphore.acquire(False) is True

    async def _release_later() -> None:
        await asyncio.sleep(0.05)
        semaphore.release()

    releaser = asyncio.create_task(_release_later())
    async with acquire_global_limiter_async(semaphore):
        pass
    await releaser

    snapshot = limiter.snapshot()
    assert snapshot["wait_count"] == 1
    assert snapshot["wait_ms_total"] >= 40


_SPAWN_HOLD_READY = None


def _init_pool_limiter_probe(_memory, _workers, config, _startup_barrier=None):
    global _SPAWN_PROBE_SEMAPHORE, _SPAWN_HOLD_READY
    _SPAWN_PROBE_SEMAPHORE = config["cleaner_api_global_limiter"]
    _SPAWN_HOLD_READY = config["ready"]


def _worker_hold_until_terminated():
    _SPAWN_PROBE_SEMAPHORE.acquire()
    _SPAWN_HOLD_READY.set()
    time.sleep(60)


@pytest.mark.parallel
def test_terminated_pool_recovers_capacity_without_releasing_parent_permit(monkeypatch):
    import modules.pipeline.pool_manager as pool_module

    limiter = GlobalAPILimiter(2)
    proxy = limiter.semaphore
    ready = mp.get_context("spawn").Event()
    manager = pool_module.SharedWorkerPoolManager(max_workers=1)
    monkeypatch.setattr(pool_module, "_init_worker", _init_pool_limiter_probe)
    monkeypatch.setattr(manager, "_run_startup_probes_locked", lambda: None)
    assert proxy.acquire(False)
    try:
        manager.ensure_started("first", worker_init_config={
            "cleaner_api_global_limiter": proxy, "ready": ready,
        })
        manager._state.executor.submit(_worker_hold_until_terminated)
        assert ready.wait(10)
        assert not proxy.acquire(False)
        manager.shutdown(timeout_seconds=0.0)
        assert manager.shutdown(timeout_seconds=5.0)
        assert proxy.acquire(False), "terminated worker's permit must be recovered"
        assert not proxy.acquire(False), "parent request still owns its permit"
        proxy.release()
        proxy.release()
        assert proxy.acquire(False)
        assert proxy.acquire(False)
        assert not proxy.acquire(False)
        proxy.release()
        proxy.release()
        manager.ensure_started("second", worker_init_config={
            "cleaner_api_global_limiter": proxy, "ready": ready,
        })
        assert manager._state.executor.submit(_worker_probe_initialized_spawn_semaphore).result(10)
    finally:
        manager.shutdown(timeout_seconds=0.0)
        manager.shutdown(timeout_seconds=5.0)
        limiter.shutdown()


def test_pool_shutdown_does_not_recover_permits_while_worker_is_alive(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event
    from unittest.mock import Mock

    import modules.pipeline.pool_manager as pool_module

    limiter = GlobalAPILimiter(1)
    proxy = limiter.semaphore
    process = Mock(is_alive=Mock(return_value=True))
    executor = Mock(_processes={1: process})
    callbacks = []
    callback_finished = Event()
    worker_limiters = []

    def start_executor(**kwargs):
        worker_limiter = kwargs["initargs"][2]["cleaner_api_global_limiter"]
        assert worker_limiter.acquire(False)
        worker_limiters.append(worker_limiter)
        if len(worker_limiters) > 1:
            worker_limiter.release()
        return executor

    def shutdown(_executor, *, on_quiesced, **kwargs):
        callbacks.append(on_quiesced)
        on_quiesced()
        callback_finished.set()
        return True

    monkeypatch.setattr(pool_module, "ProcessPoolExecutor", start_executor)
    monkeypatch.setattr(pool_module, "_shutdown_executor_bounded", shutdown)
    manager = pool_module.SharedWorkerPoolManager(max_workers=1)
    monkeypatch.setattr(manager, "_run_startup_probes_locked", lambda: None)
    manager.ensure_started("first", worker_init_config={"cleaner_api_global_limiter": proxy})
    successor = ThreadPoolExecutor(max_workers=1)
    try:
        finished = manager.shutdown(timeout_seconds=0.0)
        assert callback_finished.wait(5.0)
        assert not proxy.acquire(False), "a live worker still owns the only permit"
        assert not finished
        assert not manager.shutdown(timeout_seconds=0.0)
        pending = successor.submit(
            manager.ensure_started, "second",
            worker_init_config={"cleaner_api_global_limiter": proxy},
        )
        with pytest.raises(TimeoutError):
            pending.result(timeout=0.05)
        process.is_alive.return_value = False
        callbacks[-1]()
        pending.result(timeout=5.0)
        assert manager.shutdown(timeout_seconds=5.0)
        assert proxy.acquire(False)
        proxy.release()
    finally:
        process.is_alive.return_value = False
        if callbacks:
            callbacks[-1]()
        successor.shutdown(wait=True)
        manager.shutdown(timeout_seconds=0.0)
        limiter.shutdown()
