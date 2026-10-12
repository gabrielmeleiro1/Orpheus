"""Regression coverage for setup status cache."""

import pytest

from tests._factories_group2_setup import _RuntimeProbe


@pytest.mark.parametrize("response,cached", [
    ({"state": "checking"}, False), ({"state": "pending"}, False),
    ({"state": "timeout"}, False), ({"checking": True}, False), ({"timed_out": True}, False),
    ({"state": "ready", "installed_runtime": {"checking": True}}, True),
])
def test_status_cache_retries_selected_checks_but_caches_optional_discovery(setup_context, response, cached):
    from modules.web.setup_status_cache import RuntimeSetupStatusCache

    cache = RuntimeSetupStatusCache(clock=lambda: 0.0, probe_service=_RuntimeProbe())
    calls = []

    def build(service):
        calls.append(service)
        return response

    first = cache.get(("selection",), build)
    first["client-edit"] = True
    assert cache.get(("selection",), build) == response
    assert len(calls) == (1 if cached else 2)
