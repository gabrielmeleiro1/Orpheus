"""Tests for cross-process cleaner API global limiter."""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import time

import pytest

from modules.tts.api_rate_limit import GlobalAPILimiter, acquire_global_limiter_async


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
