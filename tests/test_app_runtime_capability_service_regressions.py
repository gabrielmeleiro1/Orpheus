"""Regression coverage for runtime capability service."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from modules.app.services import runtime_capability_service as capabilities


@pytest.mark.parametrize("failure_at", ["create", "start"])
def test_capability_refresh_retries_after_thread_start_failure(monkeypatch, failure_at):
    service = capabilities.RuntimeCapabilityService()
    thread = Mock()
    factory = Mock(return_value=thread)
    failure = RuntimeError("cannot start a new thread")
    if failure_at == "create":
        factory.side_effect = [failure, thread]
    else:
        thread.start.side_effect = [failure, None]
    monkeypatch.setattr(capabilities.threading, "Thread", factory)

    with pytest.raises(RuntimeError, match="cannot start a new thread"):
        service.get_system_runtime_snapshot()

    snapshot = service.get_system_runtime_snapshot()
    assert factory.call_count == 2
    assert thread.start.call_count == (1 if failure_at == "create" else 2)
    assert snapshot["refresh_in_progress"] is True
