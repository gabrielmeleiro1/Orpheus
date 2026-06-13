"""Multi-chapter API cleaner trace tests for malformed extraction artifacts."""

import asyncio

import json
import sys
import threading
from pathlib import Path

import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import modules.tts.cleaner as cleaner_module
from modules.tts.cleaner import TextCleaner
from modules.tts.cleaner_config import CleanerConfig
from tests.fixtures.fake_cleaner_backend import attach_cleaner_backend_strategy


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "cleaner_api_trace_multichapter_samples.json"


def _load_samples() -> list[dict[str, object]]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _make_api_cleaner() -> TextCleaner:
    cleaner = TextCleaner.__new__(TextCleaner)
    cleaner.config = CleanerConfig(
        backend="api",
        api_base_url="https://api.openai.com/v1",
        api_key_env="TEST_OPENAI_API_KEY",
        api_model="gpt-4o-mini",
    )
    cleaner._loaded = True
    cleaner._model = None
    cleaner._tokenizer = None
    cleaner._manager = None
    cleaner._model_lock = threading.RLock()
    cleaner._total_chunks_cleaned = 0
    cleaner._total_time_ms = 0
    cleaner._llm_success_count = 0
    cleaner._llm_fallback_count = 0
    cleaner._rule_only_count = 0
    cleaner._needs_llm_cleaning = lambda _text: True
    return attach_cleaner_backend_strategy(cleaner)


class _FakeResponse:
    def __init__(self, content_text: str):
        self._content_text = content_text

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        _ = (exc_type, exc, tb)
        return False

    def read(self) -> bytes:
        payload = {"choices": [{"message": {"content": self._content_text}}]}
        return json.dumps(payload).encode("utf-8")


SAMPLES = _load_samples()


@pytest.mark.parametrize(
    "sample",
    SAMPLES,
    ids=[f"{s['chapter']}-{s['description']}" for s in SAMPLES],
)
def test_api_trace_before_after_for_malformed_chapter_snippet(
    sample: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleaner = _make_api_cleaner()
    captured: dict[str, object] = {}

    def fake_urlopen(request_obj, timeout=0, context=None):
        _ = (timeout, context)
        body = json.loads(request_obj.data.decode("utf-8"))
        captured["url"] = request_obj.full_url
        captured["model"] = body.get("model")
        captured["user_prompt"] = body.get("messages", [{}, {}])[1].get("content", "")
        captured["payload"] = body
        return _FakeResponse(str(sample["expected_clean"]))

    monkeypatch.setenv("TEST_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cleaner_module.cleaner_api_backend.urlrequest, "urlopen", fake_urlopen)

    trace = cleaner.clean_with_trace(str(sample["dirty"]))

    assert trace.original_text == sample["dirty"]
    assert trace.rule_based_text == sample["expected_rule_based"]
    assert trace.cleaned_text == sample["expected_clean"]
    assert trace.mode == "llm_success"
    assert trace.llm_error is None
    assert trace.rule_based_text != trace.cleaned_text
    assert trace.rule_based_text in str(captured["user_prompt"])

    for token in sample["must_not_contain_after"]:
        assert str(token) not in trace.cleaned_text

    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["model"] == cleaner.config.api_model
    assert "max_tokens" in dict(captured["payload"])
    assert "max_completion_tokens" not in dict(captured["payload"])


def test_api_trace_multichapter_batch_updates_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    cleaner = _make_api_cleaner()
    queue = list(SAMPLES)

    def fake_urlopen(_request_obj, timeout=0, context=None):
        _ = (timeout, context)
        sample = queue.pop(0)
        return _FakeResponse(str(sample["expected_clean"]))

    monkeypatch.setenv("TEST_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cleaner_module.cleaner_api_backend.urlrequest, "urlopen", fake_urlopen)

    traces = [cleaner.clean_with_trace(str(sample["dirty"])) for sample in SAMPLES]

    assert len(queue) == 0
    assert all(t.mode == "llm_success" for t in traces)
    assert all(t.original_text != t.cleaned_text for t in traces)

    stats = cleaner.stats_snapshot()
    assert stats["llm_success"] == len(SAMPLES)
    assert stats["llm_fallback"] == 0
    assert stats["rule_only"] == 0
    assert stats["total_chunks"] == len(SAMPLES)


def test_api_trace_repairs_he_time_fragment_after_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    cleaner = _make_api_cleaner()
    dirty = (
        "### **BEST LOSER WINS** |## he time has come to get more specific. "
        "We can decide to get down to the business of creating a # **T** finely tuned trading mind."
    )
    llm_output = (
        "BEST LOSER WINS he time has come to get more specific. "
        "We can decide to get down to the business of creating a finely tuned trading mind."
    )

    def fake_urlopen(_request_obj, timeout=0, context=None):
        _ = (timeout, context)
        return _FakeResponse(llm_output)

    monkeypatch.setenv("TEST_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cleaner_module.cleaner_api_backend.urlrequest, "urlopen", fake_urlopen)

    trace = cleaner.clean_with_trace(dirty)
    assert trace.mode == "llm_success"
    assert "WINS he time has come" not in trace.cleaned_text
    assert "WINS the time has come" in trace.cleaned_text


def test_api_trace_repairs_spaced_caps_signature_after_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleaner = _make_api_cleaner()
    dirty = (
        "To Katie and Maisie The desert wind would salt their ruins and there would be nothing, "
        "no ghost or scribe, to tell any pilgrim in his passing how it was that people had lived "
        "in this place and in this place had died. —C ORMAC M C C ARTHY"
    )

    def fake_urlopen(_request_obj, timeout=0, context=None):
        _ = (timeout, context)
        # Simulate a model miss: returns the noisy signature unchanged.
        return _FakeResponse(dirty)

    monkeypatch.setenv("TEST_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cleaner_module.cleaner_api_backend.urlrequest, "urlopen", fake_urlopen)

    trace = cleaner.clean_with_trace(dirty)
    assert trace.mode == "llm_success"
    assert "—C ORMAC M C C ARTHY" not in trace.cleaned_text
    assert "—Cormac McCarthy" in trace.cleaned_text


def test_api_transport_forces_identity_encoding_to_avoid_br_decode_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleaner = _make_api_cleaner()
    captured_headers: list[dict[str, str]] = []

    class _FakeClientError(Exception):
        pass

    class _FakeAioHTTPResponse:
        def __init__(self, payload: dict[str, object]):
            self._payload = payload
            self.status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            _ = (exc_type, exc, tb)
            return False

        async def text(self) -> str:
            return json.dumps(self._payload)

    class _FakeClientSession:
        def __init__(self, *args, **kwargs):
            _ = (args, kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            _ = (exc_type, exc, tb)
            return False

        def post(self, url, *, headers, json, ssl):  # noqa: A002
            _ = (url, json, ssl)
            if headers.get("Accept-Encoding") != "identity":
                raise _FakeClientError("400, message:\n  Can not decode content-encoding: br")
            captured_headers.append(dict(headers))
            return _FakeAioHTTPResponse({"choices": [{"message": {"content": "Dr. Smith bought 42 apples."}}]})

    monkeypatch.setenv("TEST_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        cleaner_module,
        "aiohttp",
        type(
            "_FakeAioHttpModule",
            (),
            {
                "ClientSession": _FakeClientSession,
                "ClientTimeout": lambda **_kwargs: object(),
                "ClientError": _FakeClientError,
            },
        ),
        raising=False,
    )

    trace = cleaner.clean_with_trace("Dr. Smith bought 42 apples.")
    assert trace.mode == "llm_success"
    assert trace.cleaned_text == "Dr. Smith bought 42 apples."
    assert captured_headers
    assert captured_headers[0].get("Accept-Encoding") == "identity"


def test_openai_compat_parser_handles_nested_structured_choice_content() -> None:
    from modules.openai_compat import parse_chat_response_text

    response_body = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "content": [
                            {"text": {"value": "Dr. Smith"}},
                            {"content": [{"text": " bought "}, {"text": {"value": "42 apples."}}]},
                        ]
                    }
                }
            ]
        }
    )

    parsed = parse_chat_response_text(
        response_body,
        empty_choices_message="Cleaner API returned no choices",
        empty_content_message="Cleaner API returned empty content",
        parse_error_prefix="Cleaner API response parse failed",
    )

    assert parsed == "Dr. Smith bought 42 apples."


def test_api_trace_strips_numeric_reference_markers_even_when_llm_echoes_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleaner = _make_api_cleaner()
    dirty = (
        "known to history as the Salt Creek Massacre. [5] Seven men rode out. "
        "he wrote, \"the whole country seems in a fair way of becoming totally depopulated.\" [3] "
        "This phenomenon was not unknown. [10] For this reason they had avoided earlier plagues."
    )

    def fake_urlopen(_request_obj, timeout=0, context=None):
        _ = (timeout, context)
        # Simulate model pass-through with no correction.
        return _FakeResponse(dirty)

    monkeypatch.setenv("TEST_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cleaner_module.cleaner_api_backend.urlrequest, "urlopen", fake_urlopen)

    trace = cleaner.clean_with_trace(dirty)
    assert trace.mode == "llm_success"
    assert "[5]" not in trace.cleaned_text
    assert "[3]" not in trace.cleaned_text
    assert "[10]" not in trace.cleaned_text
    assert "Salt Creek Massacre. Seven men rode out." in trace.cleaned_text
    assert "depopulated.\" This phenomenon" in trace.cleaned_text


def test_api_trace_repairs_split_uppercase_word_for_tts_natural_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleaner = _make_api_cleaner()
    dirty = (
        "One A NEW KIND OF WAR C AVALRYMEN REMEMBER SUCH moments: "
        "dust swirling behind the pack mules."
    )

    def fake_urlopen(_request_obj, timeout=0, context=None):
        _ = (timeout, context)
        # Simulate model pass-through with no correction.
        return _FakeResponse(dirty)

    monkeypatch.setenv("TEST_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(cleaner_module.cleaner_api_backend.urlrequest, "urlopen", fake_urlopen)

    trace = cleaner.clean_with_trace(dirty)
    assert trace.mode == "llm_success"
    assert "C AVALRYMEN" not in trace.cleaned_text
    assert "CAVALRYMEN REMEMBER SUCH moments" in trace.cleaned_text
    assert "One A NEW KIND OF WAR" in trace.cleaned_text
    assert "One ANEW KIND OF WAR" not in trace.cleaned_text


def test_api_clean_batch_async_returns_results_for_all_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleaner = _make_api_cleaner()
    inputs = [
        "### **ONE** |## rom",
        "### **TWO** |## alking",
    ]

    requests: list[dict[str, object]] = []

    class _FakeAioHTTPResponse:
        def __init__(self, payload: dict[str, object]):
            self._payload = payload
            self.status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            _ = (exc_type, exc, tb)
            return False

        async def text(self) -> str:
            return json.dumps(self._payload)

        def raise_for_status(self) -> None:
            return None

    class _FakeClientSession:
        def __init__(self, *args, **kwargs):
            _ = (args, kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            _ = (exc_type, exc, tb)
            return False

        def post(self, url, *, headers, json, ssl):  # noqa: A002
            _ = (headers, ssl)
            requests.append({"url": url, "payload": json})
            chunk_text = str(json.get("messages", [{}, {}])[1].get("content", ""))
            if "ONE" in chunk_text:
                cleaned = "ONE From"
            else:
                cleaned = "TWO Walking"
            return _FakeAioHTTPResponse({"choices": [{"message": {"content": cleaned}}]})

    monkeypatch.setenv("TEST_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        cleaner_module,
        "aiohttp",
        type(
            "_FakeAioHttpModule",
            (),
            {
                "ClientSession": _FakeClientSession,
                "ClientTimeout": lambda **_kwargs: object(),
                "ClientError": Exception,
            },
        ),
        raising=False,
    )

    outputs = asyncio.run(cleaner.clean_batch_async(inputs))
    assert outputs == ["ONE From", "TWO Walking"]
    assert len(requests) == len(inputs)


def test_api_clean_batch_with_trace_async_returns_trace_rows_in_input_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleaner = _make_api_cleaner()
    cleaner.config.api_batch_concurrency = 4
    inputs = [
        "### **ONE** |## rom",
        "### **TWO** |## alking",
    ]

    class _FakeAioHTTPResponse:
        def __init__(self, payload: dict[str, object]):
            self._payload = payload
            self.status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            _ = (exc_type, exc, tb)
            return False

        async def text(self) -> str:
            return json.dumps(self._payload)

    class _FakeClientSession:
        def __init__(self, *args, **kwargs):
            _ = (args, kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            _ = (exc_type, exc, tb)
            return False

        def post(self, url, *, headers, json, ssl):  # noqa: A002
            _ = (url, headers, ssl)
            chunk_text = str(json.get("messages", [{}, {}])[1].get("content", ""))
            if "ONE" in chunk_text:
                cleaned = "ONE From"
            else:
                cleaned = "TWO Walking"
            return _FakeAioHTTPResponse({"choices": [{"message": {"content": cleaned}}]})

    monkeypatch.setenv("TEST_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        cleaner_module,
        "aiohttp",
        type(
            "_FakeAioHttpModule",
            (),
            {
                "ClientSession": _FakeClientSession,
                "ClientTimeout": lambda **_kwargs: object(),
                "ClientError": Exception,
            },
        ),
        raising=False,
    )

    traces = asyncio.run(cleaner.clean_batch_with_trace_async(inputs))
    assert [trace.cleaned_text for trace in traces] == ["ONE From", "TWO Walking"]
    assert [trace.mode for trace in traces] == ["llm_success", "llm_success"]
    assert all(trace.original_text == inputs[idx] for idx, trace in enumerate(traces))
    assert traces[0].rule_based_text != traces[0].original_text


def test_api_clean_batch_with_trace_async_reuses_one_aiohttp_session_per_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleaner = _make_api_cleaner()
    cleaner.config.api_batch_concurrency = 4
    inputs = [f"Chunk {idx} content with # artifact" for idx in range(10)]
    session_creations = 0

    class _FakeAioHTTPResponse:
        def __init__(self, cleaned_text: str):
            self._cleaned_text = cleaned_text
            self.status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            _ = (exc_type, exc, tb)
            return False

        async def text(self) -> str:
            return json.dumps({"choices": [{"message": {"content": self._cleaned_text}}]})

    class _FakeClientSession:
        def __init__(self, *args, **kwargs):
            nonlocal session_creations
            _ = (args, kwargs)
            session_creations += 1

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            _ = (exc_type, exc, tb)
            return False

        def post(self, url, *, headers, json, ssl):  # noqa: A002
            _ = (url, headers, ssl)
            prompt_text = str(json.get("messages", [{}, {}])[1].get("content", ""))
            cleaned_text = prompt_text.split("Text to correct:\n---\n", 1)[1].split("\n---", 1)[0]
            return _FakeAioHTTPResponse(cleaned_text)

    monkeypatch.setenv("TEST_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        cleaner_module,
        "aiohttp",
        type(
            "_FakeAioHttpModule",
            (),
            {
                "ClientSession": _FakeClientSession,
                "ClientTimeout": lambda **_kwargs: object(),
                "ClientError": Exception,
            },
        ),
        raising=False,
    )

    traces = asyncio.run(cleaner.clean_batch_with_trace_async(inputs))

    assert len(traces) == len(inputs)
    assert all(trace.mode == "llm_success" for trace in traces)
    assert session_creations == 1


def test_api_clean_batch_with_trace_async_honors_bounded_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleaner = _make_api_cleaner()
    cleaner.config.api_batch_concurrency = 2
    inputs = [f"Chunk {idx} content" for idx in range(6)]
    active_requests = 0
    peak_concurrency = 0

    class _FakeAioHTTPResponse:
        def __init__(self, cleaned_text: str):
            self._cleaned_text = cleaned_text
            self.status = 200

        async def __aenter__(self):
            nonlocal active_requests, peak_concurrency
            active_requests += 1
            peak_concurrency = max(peak_concurrency, active_requests)
            await asyncio.sleep(0.01)
            return self

        async def __aexit__(self, exc_type, exc, tb):
            nonlocal active_requests
            _ = (exc_type, exc, tb)
            active_requests -= 1
            return False

        async def text(self) -> str:
            return json.dumps({"choices": [{"message": {"content": self._cleaned_text}}]})

    class _FakeClientSession:
        def __init__(self, *args, **kwargs):
            _ = (args, kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            _ = (exc_type, exc, tb)
            return False

        def post(self, url, *, headers, json, ssl):  # noqa: A002
            _ = (url, headers, ssl)
            prompt_text = str(json.get("messages", [{}, {}])[1].get("content", ""))
            return _FakeAioHTTPResponse(prompt_text)

    monkeypatch.setenv("TEST_OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        cleaner_module,
        "aiohttp",
        type(
            "_FakeAioHttpModule",
            (),
            {
                "ClientSession": _FakeClientSession,
                "ClientTimeout": lambda **_kwargs: object(),
                "ClientError": Exception,
            },
        ),
        raising=False,
    )

    traces = asyncio.run(cleaner.clean_batch_with_trace_async(inputs))
    assert len(traces) == len(inputs)
    assert peak_concurrency <= 2


def test_api_trace_async_logs_exception_fallback(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cleaner = _make_api_cleaner()

    async def fail_generate(_prompt: str, *, api_session=None) -> str:
        _ = api_session
        raise RuntimeError("api boom")

    monkeypatch.setattr(cleaner, "_generate_api_response_async", fail_generate)
    caplog.set_level("WARNING", logger="modules.tts.cleaner")

    trace = asyncio.run(
        cleaner._clean_with_trace_api_async(  # noqa: SLF001
            "Chunk content with # extraction artifact.",
            asyncio.Semaphore(1),
        )
    )

    assert trace.mode == "llm_fallback"
    assert trace.llm_error == "api boom"
    assert "LLM cleaning failed: api boom, using rule-based fallback" in caplog.text


def test_trace_decision_flow_sync_async_success_parity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "Chunk content with # extraction artifact."
    cleaned_text = "Chunk content with extraction artifact."
    sync_cleaner = _make_api_cleaner()
    async_cleaner = _make_api_cleaner()

    monkeypatch.setattr(sync_cleaner, "_generate_llm_response", lambda _prompt: cleaned_text)

    async def async_generate(_prompt: str, *, api_session=None) -> str:
        _ = api_session
        return cleaned_text

    monkeypatch.setattr(async_cleaner, "_generate_api_response_async", async_generate)

    sync_trace = sync_cleaner.clean_with_trace(text)
    async_trace = asyncio.run(
        async_cleaner._clean_with_trace_api_async(  # noqa: SLF001
            text,
            asyncio.Semaphore(1),
        )
    )

    assert sync_trace.mode == async_trace.mode == "llm_success"
    assert sync_trace.original_text == async_trace.original_text == text
    assert sync_trace.rule_based_text == async_trace.rule_based_text
    assert sync_trace.cleaned_text == async_trace.cleaned_text == cleaned_text
    assert sync_trace.llm_output == async_trace.llm_output == cleaned_text
    assert sync_trace.llm_error == async_trace.llm_error is None
    assert sync_trace.artifact_score == async_trace.artifact_score
    assert sync_cleaner.stats_snapshot() == async_cleaner.stats_snapshot()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-o", "addopts="]))
