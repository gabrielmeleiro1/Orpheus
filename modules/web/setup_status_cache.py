"""Single-flight cache for the expensive local setup-readiness probes."""

from __future__ import annotations

from concurrent.futures import Future, wait
import copy
import threading
import time
from typing import Any, Callable

from modules.app.services.runtime_capability_service import RuntimeCapabilityService
from modules.runtime_setup.receipts import ModelReceiptVerifier

StatusBuilder = Callable[[RuntimeCapabilityService], dict[str, Any]]


class RuntimeSetupStatusCache:
    """Small app-owned single-flight cache for expensive local readiness probes."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 4.0,
        clock: Callable[[], float] = time.monotonic,
        probe_service: RuntimeCapabilityService | None = None,
        receipt_verifier: ModelReceiptVerifier | None = None,
    ) -> None:
        self._ttl_seconds = max(0.1, min(float(ttl_seconds), 5.0))
        self._clock = clock
        self._probe_service = probe_service or RuntimeCapabilityService(
            cache_ttl_seconds=self._ttl_seconds,
        )
        self.receipt_verifier = receipt_verifier or ModelReceiptVerifier()
        self._condition = threading.Condition()
        self._refreshing = False
        self._key: tuple[Any, ...] | None = None
        self._expires_at = 0.0
        self._response: dict[str, Any] | None = None
        self._generation = 0
        self._flight: Future[dict[str, Any]] | None = None
        self._flight_key: tuple[Any, ...] | None = None
        self._flight_generation = 0

    @property
    def probe_service(self) -> RuntimeCapabilityService:
        """Expose the single-flight probe service for adopt re-validation."""
        return self._probe_service

    @staticmethod
    def _is_checking(value: Any) -> bool:
        # The status projection folds selected runtime checks into this state;
        # optional discovery checks must not prevent caching completed readiness.
        if isinstance(value, dict):
            return (
                bool(value.get("checking") or value.get("timed_out"))
                or value.get("state") in {"checking", "pending", "timeout"}
            )
        return False

    def _refresh(
        self,
        key: tuple[Any, ...],
        builder: StatusBuilder,
        generation: int,
        flight: Future[dict[str, Any]],
    ) -> None:
        try:
            response = builder(self._probe_service)
        except Exception as exc:
            with self._condition:
                self._refreshing = False
                flight.set_exception(exc)
                self._condition.notify_all()
            return
        with self._condition:
            if generation == self._generation and not self._is_checking(response):
                self._key = key
                self._response = copy.deepcopy(response)
                self._expires_at = self._clock() + self._ttl_seconds
            self._refreshing = False
            flight.set_result(response)
            self._condition.notify_all()

    def get(
        self,
        key: tuple[Any, ...],
        builder: StatusBuilder,
        *,
        pending_builder: Callable[[], dict[str, Any]] | None = None,
        wait_seconds: float = 2.0,
    ) -> dict[str, Any]:
        with self._condition:
            if (
                self._response is not None
                and self._key == key
                and self._clock() < self._expires_at
            ):
                return copy.deepcopy(self._response)
            start_refresh = not self._refreshing
            if start_refresh:
                self._refreshing = True
                self._flight = Future()
                self._flight_key = key
                self._flight_generation = self._generation
            flight = self._flight
            flight_key = self._flight_key
            generation = self._flight_generation
        assert flight is not None
        if start_refresh:
            if pending_builder is None:
                self._refresh(key, builder, generation, flight)
            else:
                threading.Thread(
                    target=self._refresh,
                    args=(key, builder, generation, flight),
                    name="setup-status-refresh",
                    daemon=True,
                ).start()
        if pending_builder is not None and not wait([flight], timeout=wait_seconds).done:
            return pending_builder()
        response = flight.result()
        if flight_key != key or generation != self._generation:
            return self.get(key, builder, pending_builder=pending_builder, wait_seconds=wait_seconds)
        return copy.deepcopy(response)

    def invalidate(self) -> None:
        """Allow the desktop setup runner to force an immediate next refresh."""
        with self._condition:
            self._generation += 1
            self._key = None
            self._response = None
            self._expires_at = 0.0
            self.receipt_verifier.invalidate()
            self._condition.notify_all()
