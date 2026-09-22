"""Single-flight cache for the expensive local setup-readiness probes."""

from __future__ import annotations

import copy
import threading
import time
from typing import Any, Callable

from modules.app.services.runtime_capability_service import RuntimeCapabilityService
from scripts.download_models import ModelReceiptVerifier

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
            timeout_seconds=15.0,
        )
        self.receipt_verifier = receipt_verifier or ModelReceiptVerifier()
        self._condition = threading.Condition()
        self._refreshing = False
        self._key: tuple[Any, ...] | None = None
        self._expires_at = 0.0
        self._response: dict[str, Any] | None = None
        self._generation = 0

    @property
    def probe_service(self) -> RuntimeCapabilityService:
        """Expose the single-flight probe service for adopt re-validation."""
        return self._probe_service

    def get(
        self,
        key: tuple[Any, ...],
        builder: StatusBuilder,
    ) -> dict[str, Any]:
        while True:
            with self._condition:
                now = self._clock()
                if (
                    self._response is not None
                    and self._key == key
                    and now < self._expires_at
                ):
                    return copy.deepcopy(self._response)
                if not self._refreshing:
                    self._refreshing = True
                    generation = self._generation
                    break
                self._condition.wait()
        try:
            response = builder(self._probe_service)
        except Exception:
            with self._condition:
                self._refreshing = False
                self._condition.notify_all()
            raise
        with self._condition:
            invalidated = generation != self._generation
            if not invalidated:
                self._key = key
                self._response = copy.deepcopy(response)
                self._expires_at = self._clock() + self._ttl_seconds
            self._refreshing = False
            self._condition.notify_all()
        if invalidated:
            return self.get(key, builder)
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
