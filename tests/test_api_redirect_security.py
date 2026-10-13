import pytest
from urllib.request import Request

from modules.openai_compat import _AuthenticatedRedirectHandler
from tests._api_security_helpers import _call, _server

@pytest.mark.parametrize("kind", ["models", "voice", "cleaner-sync", "cleaner-async"])
def test_normal_success_authenticates_only_test_server(kind):
    with _server() as (url, observations):
        result = _call(kind, url)
    assert result
    assert len(observations) == 1 and observations[0]["authenticated"]


_REDIRECT_CASES = [("models", code) for code in (301, 302, 303, 307, 308)] + [
    (kind, code) for kind in ("voice", "cleaner-sync") for code in (301, 302, 303)]


@pytest.mark.parametrize("kind,status", _REDIRECT_CASES)
def test_same_origin_redirect_keeps_authorization(kind, status):
    with _server(status=status, redirect=lambda port: f"http://127.0.0.1:{port}/final") as (url, observations):
        assert _call(kind, url)
    assert len(observations) == 2
    assert all(row["authenticated"] for row in observations)


@pytest.mark.parametrize("kind,status", _REDIRECT_CASES)
def test_cross_origin_redirect_drops_authorization(kind, status):
    with _server() as (target, destination):
        with _server(status=status, redirect=target + "/final") as (url, origin):
            assert _call(kind, url)
    assert len(origin) == len(destination) == 1
    assert origin[0]["authenticated"]
    assert not destination[0]["authenticated"], "cross-origin target received authorization"


@pytest.mark.parametrize("kind", ["voice", "cleaner-sync"])
@pytest.mark.parametrize("status", [307, 308])
def test_unsupported_post_redirect_is_rejected_without_contacting_destination(kind, status):
    with _server() as (target, destination):
        with _server(status=status, redirect=target + "/final") as (url, origin):
            with pytest.raises(RuntimeError):
                _call(kind, url)
    assert len(origin) == 1
    assert destination == []


@pytest.mark.parametrize("source,target,authenticated", [
    ("https://provider.invalid/start", "https://PROVIDER.invalid:443/final", True),
    ("http://provider.invalid:80/start", "http://provider.invalid/final", True),
    ("https://provider.invalid/start", "http://provider.invalid/final", False),
    ("https://provider.invalid/start", "https://other.invalid/final", False),
    ("https://provider.invalid:443/start", "https://provider.invalid:444/final", False),
])
def test_redirect_origin_includes_scheme_host_and_effective_port(source, target, authenticated):
    request = Request(source, headers={"Authorization": "audit-placeholder", "Accept": "application/json"})
    redirected = _AuthenticatedRedirectHandler().redirect_request(request, None, 302, "Found", {}, target)
    assert bool(redirected.get_header("Authorization")) is authenticated
    assert redirected.get_header("Accept") == "application/json"
