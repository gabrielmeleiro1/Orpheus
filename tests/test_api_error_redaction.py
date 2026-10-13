import asyncio
import json
import logging
import time
import traceback
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from modules.tts import cleaner_api
from modules.tts.cleaner_trace import _exception_fallback_trace, TraceDecisionRequest
from tests._api_security_helpers import _call, _contains_no_credential, _CREDENTIAL, _server, UpstreamError


def _assert_safe_error(error):
    safe_message = _contains_no_credential(error)
    assert safe_message, "transport exposed synthetic credential"
    safe_traceback = _contains_no_credential("".join(traceback.format_exception(error)))
    assert safe_traceback, "exception chain exposed synthetic credential"
    if hasattr(error, "detail"):
        safe_detail = _contains_no_credential(error.detail)
        assert safe_detail, "error detail exposed synthetic credential"


@pytest.mark.parametrize("kind", ["models", "voice", "cleaner-sync", "cleaner-async"])
def test_provider_error_echo_is_redacted_before_leaving_transport(kind):
    with _server(status=401) as (url, observations):
        with pytest.raises(RuntimeError) as error:
            _call(kind, url)
    assert len(observations) == 1 and observations[0]["authenticated"]
    _assert_safe_error(error.value)
    assert "401" in str(error.value)


@pytest.mark.parametrize("kind", ["models", "voice", "cleaner-sync", "cleaner-async"])
def test_provider_normalized_padded_credential_echo_is_redacted(kind):
    with _server(status=401) as (url, _observations):
        with pytest.raises(RuntimeError) as error:
            _call(kind, url, api_key=" " + _CREDENTIAL + " ")
    _assert_safe_error(error.value)


@pytest.mark.parametrize("kind", ["models", "voice", "cleaner-sync"])
def test_connection_error_echo_is_redacted(monkeypatch, kind):
    def fail(*_args, **_kwargs):
        raise URLError("connection failed " + _CREDENTIAL)

    monkeypatch.setattr(cleaner_api.urlrequest, "urlopen", fail)
    with pytest.raises(RuntimeError) as error:
        _call(kind, "http://127.0.0.1:1")
    _assert_safe_error(error.value)
    assert "connection" in str(error.value)


def test_retry_error_echo_is_redacted(monkeypatch):
    calls = []

    async def respond(**kwargs):
        calls.append(kwargs["payload"])
        if len(calls) == 1:
            raise UpstreamError(400, "max_tokens unsupported; use max_completion_tokens " + _CREDENTIAL)
        raise UpstreamError(401, "credential rejected " + _CREDENTIAL)

    monkeypatch.setattr(cleaner_api, "post_api_chat_completions_async", respond)
    with pytest.raises(RuntimeError) as error:
        asyncio.run(cleaner_api.post_api_chat_completions_with_retry_async(
            config=SimpleNamespace(api_timeout_seconds=3), base_url="http://127.0.0.1:1",
            api_key=_CREDENTIAL, payload={"max_tokens": 20}, ssl_context=None,
            http_error_cls=UpstreamError, max_tokens=20))
    assert calls == [{"max_tokens": 20}, {"max_completion_tokens": 20}]
    _assert_safe_error(error.value)


def test_provider_error_stays_redacted_in_cleanup_log_and_trace(caplog):
    with _server(status=401) as (url, _observations):
        with pytest.raises(RuntimeError) as error:
            _call("cleaner-sync", url)
    cleaner = SimpleNamespace(_llm_fallback_count=0, _total_chunks_cleaned=0, _total_time_ms=0)
    request = TraceDecisionRequest("sample", "sample", time.time(), {"score": 1}, "sample")
    trace = _exception_fallback_trace(
        cleaner, SimpleNamespace, request, error.value,
        warning_logger=logging.getLogger("audit.cleanup.redaction"))
    assert trace.mode == "llm_fallback"
    assert trace.cleaned_text == "sample"
    safe_log = _contains_no_credential(caplog.text)
    safe_trace = _contains_no_credential(json.dumps(vars(trace)))
    assert safe_log, "cleanup logger exposed synthetic credential"
    assert safe_trace, "serialized cleanup trace exposed synthetic credential"
