"""
Test Text Cleaner Module
========================
Unit tests for the LLM-based text cleaner.
"""

import json
import os
import ssl
import sys
import threading
import asyncio
import time
import urllib.error as urlerror
from pathlib import Path
from types import SimpleNamespace

import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import modules.tts.cleaner as cleaner_module
from modules.tts.cleaner_transport import generate_api_response_async
from modules.tts.cleaner import TextCleaner
from modules.tts.cleaner_config import CleanerConfig
from modules.tts.cleaner_api import CleanerAPIStrategy
from modules.tts.cleaner_mlx import CleanerMLXStrategy
from modules.tts.cleaner_rules import extract_result
from modules.tts import cleaner_rules as cleaner_rules_backend
from tests.fixtures.fake_cleaner_backend import make_partial_text_cleaner


def test_text_cleaner_init_selects_backend_from_registry(monkeypatch):
    api_cleaner = TextCleaner(CleanerConfig(backend="api"))
    api_strategy = api_cleaner._backend_strategy
    assert isinstance(api_strategy, CleanerAPIStrategy)

    monkeypatch.setattr(
        "modules.tts.cleaner_mlx.ensure_mlx_lm_runtime",
        lambda: None,
    )
    mlx_cleaner = TextCleaner(CleanerConfig(backend="mlx"))
    mlx_strategy = mlx_cleaner._backend_strategy
    assert isinstance(mlx_strategy, CleanerMLXStrategy)

    with pytest.raises(ValueError, match="not supported"):
        TextCleaner(CleanerConfig(backend="bogus"))


def test_extract_result_strips_prompt_delimiter_echoes():
    response = "---\nClean audiobook text should remain.\n---"

    assert extract_result(response) == "Clean audiobook text should remain."


def test_extract_result_strips_attached_trailing_prompt_delimiter():
    response = "Final sentence should remain.---"

    assert extract_result(response) == "Final sentence should remain."


def test_rule_based_clean_preserves_em_dash_pronoun_spacing():
    assert (
        cleaner_rules_backend.rule_based_clean("Then perhaps”—I began again.")
        == "Then perhaps”—I began again."
    )
    assert (
        cleaner_rules_backend.rule_based_clean("So there—I would call it by name.")
        == "So there—I would call it by name."
    )
    assert (
        cleaner_rules_backend.rule_based_clean("It was decided—A man should go first.")
        == "It was decided—A man should go first."
    )


def test_rule_based_clean_repairs_reviewed_spaced_hyphen_compounds():
    from modules.ingestion.extraction_artifact_signals import (
        scan_cleaner_handoff_artifacts,
    )

    cleaned = cleaner_rules_backend.rule_based_clean(
        "He was a notorious card- sharper who fleeced them."
    )
    assert "card-sharper" in cleaned
    assert "card- sharper" not in cleaned
    assert not any(
        issue.issue_type == "pdf_hyphenation_leakage"
        for issue in scan_cleaner_handoff_artifacts(cleaned)
    )

    # A second reviewed compound proves this is not tied to one benchmark phrase.
    assert (
        "well-being"
        in cleaner_rules_backend.rule_based_clean("Their well- being mattered most.")
    )


def test_rule_based_clean_preserves_unknown_spaced_hyphen_text_for_gate():
    cleaned = cleaner_rules_backend.rule_based_clean(
        "The passage left experi- ence and gover- nment artifacts."
    )

    assert "experi- ence" in cleaned
    assert "gover- nment" in cleaned
    assert cleaner_rules_backend.needs_llm_cleaning(cleaned) is True


def test_rule_based_clean_preserves_valid_prose_against_hyphen_repair():
    # Hanging/suspended hyphens (followed by a conjunction/article) are valid and
    # must be left untouched.
    for valid in (
        "pre- and post-war economics shifted.",
        "two- or three-year contracts were common.",
        "neither in- nor outside the room.",
    ):
        assert cleaner_rules_backend.rule_based_clean(valid) == valid

    # Already-correct hyphenated compounds and em dashes stay intact.
    assert (
        cleaner_rules_backend.rule_based_clean("a self-reliant, well-known idea")
        == "a self-reliant, well-known idea"
    )
    assert (
        cleaner_rules_backend.rule_based_clean("He paused — perhaps too long.")
        == "He paused — perhaps too long."
    )
    assert cleaner_rules_backend.rule_based_clean("No- wait, he said.") == (
        "No- wait, he said."
    )
    assert cleaner_rules_backend.rule_based_clean("Yes- perhaps we should.") == (
        "Yes- perhaps we should."
    )
    assert cleaner_rules_backend.rule_based_clean("Well- maybe tomorrow.") == (
        "Well- maybe tomorrow."
    )
    # A hyphen flanked by spaces (punctuation dash) is not a line-break artifact.
    assert (
        cleaner_rules_backend.rule_based_clean("the plan - which failed - was bold")
        == "the plan - which failed - was bold"
    )


def test_rule_based_clean_still_repairs_spaced_caps_em_dash_attributions():
    assert (
        cleaner_rules_backend.rule_based_clean("An attribution follows—C ORMAC M C C ARTHY")
        == "An attribution follows—Cormac McCarthy"
    )


def test_cleaner_skip_llm_routes_joined_first_person_words_to_llm(monkeypatch):
    cleaner = _make_loaded_mlx_cleaner(
        CleanerConfig(skip_llm_when_not_needed=True)
    )
    calls = []

    def fake_generate(_model, _tokenizer, **kwargs):
        calls.append(kwargs)
        return "I am tired. I would rest."

    monkeypatch.setattr(cleaner_module.cleaner_mlx_backend, "make_sampler", lambda temp=0.0: ("sampler", temp))
    monkeypatch.setattr(cleaner_module.cleaner_mlx_backend, "generate", fake_generate)

    trace = cleaner.clean_with_trace("Iam tired. Iwould rest.")

    assert trace.mode == "llm_success"
    assert trace.cleaned_text == "I am tired. I would rest."
    assert len(calls) == 1


def test_cleaner_prompt_scopes_literary_preservation():
    from modules.tts.cleaner_config import build_cleaner_prompt

    prompt = build_cleaner_prompt(
        "Long - long - long - many days.",
        reason="uppercase token that may be OCR/casing artifact",
    )

    assert "Likely cleanup reason: uppercase token that may be OCR/casing artifact" in prompt
    assert "If you are not certain" in prompt
    assert "preserve literary repetition" in prompt
    assert "epigraphs, poems, titles, foreign-language text" in prompt


def _make_loaded_mlx_cleaner(config: CleanerConfig | None = None) -> TextCleaner:
    return make_partial_text_cleaner(config or CleanerConfig(), loaded=True)


def test_cleaner_skip_llm_option_uses_rule_only_for_long_clean_text(monkeypatch):
    cleaner = _make_loaded_mlx_cleaner(
        CleanerConfig(skip_llm_when_not_needed=True)
    )
    text = (
        "This long paragraph already reads naturally for narration. "
        "It has ordinary sentence structure and no extraction artifacts. "
    ) * 12

    def fail_generate(*_args, **_kwargs):
        raise AssertionError("LLM generation should not be called")

    monkeypatch.setattr(cleaner_module.cleaner_mlx_backend, "generate", fail_generate)

    trace = cleaner.clean_with_trace(text)

    assert trace.mode == "rule_only"
    assert trace.cleaned_text == cleaner._rule_based_clean(text)
    stats = cleaner.stats_snapshot()
    assert stats["rule_only"] == 1
    assert stats["llm_success"] == 0


def test_cleaner_batch_uses_rule_only_after_hallucination_fallback(monkeypatch):
    cleaner = _make_loaded_mlx_cleaner(
        CleanerConfig(skip_llm_when_not_needed=True)
    )
    calls = []
    original_clean_with_trace = cleaner.clean_with_trace

    def fake_clean_with_trace(text: str):
        calls.append(text)
        if len(calls) == 1:
            return cleaner_module._CleanTrace(
                original_text=text,
                rule_based_text=text,
                cleaned_text=text,
                llm_output="changed text",
                llm_error="LLM preservation guard rejected token replace",
                mode="hallucination_fallback",
            )
        return original_clean_with_trace(text)

    monkeypatch.setattr(cleaner, "clean_with_trace", fake_clean_with_trace)

    traces = cleaner.clean_chunks_with_trace(["chunk one", "chunk two", "chunk tre"])

    assert traces[0].mode == "hallucination_fallback"
    assert traces[1].mode == "rule_only"
    assert traces[2].mode == "rule_only"
    assert len(calls) == 1


def test_cleaner_blank_text_counts_as_rule_only_chunk():
    cleaner = _make_loaded_mlx_cleaner()

    trace = cleaner.clean_with_trace("   ")

    assert trace.mode == "rule_only"
    assert trace.cleaned_text == ""
    assert cleaner.stats_snapshot()["rule_only"] == 1
    assert cleaner.stats_snapshot()["total_chunks"] == 1


def test_cleaner_api_runtime_prefers_direct_api_key(monkeypatch):
    """Direct pasted API keys should be usable without env setup."""
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    config = CleanerConfig(
        backend="api",
        api_key="  pasted-key  ",
        api_key_env="OPENAI_API_KEY",
        api_model="gpt-5-nano",
    )

    base_url, api_key, model_name = cleaner_module.cleaner_api_backend.resolve_api_runtime(config)

    assert base_url == "https://api.openai.com/v1"
    assert api_key == "pasted-key"
    assert model_name == "gpt-5-nano"


def test_rule_based_cleaning():
    """Test rule-based cleaning without loading LLM."""
    print("\n" + "="*50)
    print("TEXT CLEANER TEST SUITE")
    print("="*50 + "\n")
    
    print("[1] Testing rule-based text cleaning...")
    
    # Create cleaner without loading LLM
    cleaner = make_partial_text_cleaner(CleanerConfig(), loaded=False)
    
    test_cases = [
        ("Dr. Smith went to St. Louis.", "Dr. Smith went to St. Louis."),
        ("e.g. this is an example", "e.g. this is an example"),
        ("Too   many    spaces", "Too many spaces"),
        ("Mr. and Mrs. Jones left", "Mr. and Mrs. Jones left"),
        ("Prof. Brown said etc.", "Prof. Brown said etc."),
    ]
    
    passed = 0
    for input_text, expected in test_cases:
        result = cleaner._rule_based_clean(input_text)
        if result == expected:
            print(f"  ✓ '{input_text}'")
            print(f"    → '{result}'")
            passed += 1
        else:
            print(f"  ✗ '{input_text}'")
            print(f"    Got:      '{result}'")
            print(f"    Expected: '{expected}'")
    
    print(f"\n  Passed {passed}/{len(test_cases)} tests")

    print("\n[1b] Testing markdown table normalization...")
    table_input = (
        "|Action|Conscious Reason|Subconscious Reason|\n"
        "|---|---|---|\n"
        "|I am taking profits|You can't go broke taking a profit|Avoid pain|"
    )
    table_cleaned = cleaner._rule_based_clean(table_input)
    assert "Action: I am taking profits" in table_cleaned
    assert "Conscious Reason: You can't go broke taking a profit" in table_cleaned
    assert "Subconscious Reason: Avoid pain" in table_cleaned
    assert "|" not in table_cleaned
    print("  ✓ markdown table converted to speech-friendly text")

    compressed_table_input = (
        "|Action|Conscious Reason|Subconscious Reason| |---|---|---| "
        "|1. I am letting my loss run|I am hoping|Avoid pain| "
        "|2. I am letting my loss run|Indicator/Fib/etc., says<br>so|Avoid pain|"
    )
    compressed_cleaned = cleaner._rule_based_clean(compressed_table_input)
    assert "Action: 1. I am letting my loss run" in compressed_cleaned
    assert "Indicator/Fib/etc., says so" in compressed_cleaned
    assert "|" not in compressed_cleaned
    assert "<br>" not in compressed_cleaned
    print("  ✓ compressed markdown table and <br> are normalized")
    
    # Test 2: LLM detection
    print("\n[2] Testing LLM need detection...")
    
    need_tests = [
        ("Hello world", False),
        ("There are 42 apples", False),
        ("NASA launched today", False),
        ("Iam tired today", True),
        ("Simple text here", False),
    ]
    
    for text, expected in need_tests:
        result = cleaner._needs_llm_cleaning(text)
        status = "✓" if result == expected else "✗"
        print(f"  {status} '{text}' → needs_llm={result} (expected {expected})")
    
    # Test 3: Result extraction
    print("\n[3] Testing result extraction...")
    
    extract_tests = [
        ("  cleaned text  ", "cleaned text"),
        ("First paragraph.\n\nSecond paragraph.", "First paragraph.\n\nSecond paragraph."),
    ]
    
    for input_text, expected in extract_tests:
        result = cleaner._extract_result(input_text)
        status = "✓" if result == expected else "✗"
        print(f"  {status} Extraction works correctly")

    # Test 4: mlx-lm sampler compatibility
    print("\n[4] Testing mlx-lm sampler compatibility...")
    compat_cleaner = make_partial_text_cleaner(CleanerConfig(), loaded=True)

    calls = []

    def fake_generate(_model, _tokenizer, **kwargs):
        calls.append(kwargs)
        assert "sampler" in kwargs
        return "I am tired today."

    original_generate = cleaner_module.cleaner_mlx_backend.generate
    original_make_sampler = cleaner_module.cleaner_mlx_backend.make_sampler
    cleaner_module.cleaner_mlx_backend.make_sampler = lambda temp=0.0: ("sampler", temp)
    cleaner_module.cleaner_mlx_backend.generate = fake_generate
    try:
        cleaned = compat_cleaner.clean("Iam tired today.")
    finally:
        cleaner_module.cleaner_mlx_backend.generate = original_generate
        cleaner_module.cleaner_mlx_backend.make_sampler = original_make_sampler

    assert cleaned == "I am tired today."
    assert len(calls) == 1
    assert calls[0]["sampler"] == ("sampler", compat_cleaner.config.temperature)
    stats = compat_cleaner.stats_snapshot()
    assert stats["llm_success"] == 1
    assert stats["llm_fallback"] == 0
    print("  ✓ cleaner uses sampler-based generation for mlx-lm compatibility")

    # Test 5: before/after cleaner trace
    print("\n[5] Testing cleaner before/after trace...")
    cleaner_module.cleaner_mlx_backend.make_sampler = lambda temp=0.0: ("sampler", temp)
    cleaner_module.cleaner_mlx_backend.generate = fake_generate
    try:
        trace = compat_cleaner.clean_with_trace("Iam tired today.")
    finally:
        cleaner_module.cleaner_mlx_backend.generate = original_generate
        cleaner_module.cleaner_mlx_backend.make_sampler = original_make_sampler
    assert trace.original_text == "Iam tired today."
    assert "Iam" in trace.rule_based_text
    assert trace.cleaned_text == "I am tired today."
    assert trace.mode == "llm_success"
    print("  ✓ cleaner trace captures original, rule-based, and cleaned text")

    # Test 6: API backend path (OpenAI-compatible / Kimi)
    print("\n[6] Testing API cleaner backend request flow...")
    api_cleaner = make_partial_text_cleaner(
        CleanerConfig(
            model_name="moonshot-v1-8k",
            backend="api",
            api_base_url="https://api.moonshot.cn/v1",
            api_key_env="KIMI_API_KEY",
            api_model=None,
        ),
        loaded=True,
    )

    captured_request = {"url": None, "model": None, "context": None}

    class _FakeResponse:
        def __init__(self, payload: dict):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            _ = (exc_type, exc_val, exc_tb)
            return False

        def read(self) -> bytes:
            return json.dumps(self._payload).encode("utf-8")

    def fake_urlopen(request_obj, timeout=0, context=None):
        _ = timeout
        captured_request["context"] = context
        captured_request["url"] = request_obj.full_url
        body = json.loads(request_obj.data.decode("utf-8"))
        captured_request["model"] = body["model"]
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": "I am tired today."
                        }
                    }
                ]
            }
        )

    original_urlopen = cleaner_module.cleaner_api_backend.urlrequest.urlopen
    original_key = os.environ.get("KIMI_API_KEY")
    os.environ["KIMI_API_KEY"] = "test-key"
    cleaner_module.cleaner_api_backend.urlrequest.urlopen = fake_urlopen
    try:
        cleaned_api = api_cleaner.clean("Iam tired today.")
    finally:
        cleaner_module.cleaner_api_backend.urlrequest.urlopen = original_urlopen
        if original_key is None:
            os.environ.pop("KIMI_API_KEY", None)
        else:
            os.environ["KIMI_API_KEY"] = original_key

    assert cleaned_api == "I am tired today."
    assert captured_request["url"] == "https://api.moonshot.cn/v1/chat/completions"
    assert captured_request["model"] == "moonshot-v1-8k"
    assert captured_request["context"] is not None
    assert api_cleaner.stats_snapshot()["llm_success"] == 1
    print("  ✓ API backend sends request and consumes cleaned response")

    # Test 7: API backend TLS context with custom CA bundle
    print("\n[7] Testing API cleaner backend TLS custom CA bundle...")
    api_ca_cleaner = make_partial_text_cleaner(
        CleanerConfig(
            model_name="moonshot-v1-8k",
            backend="api",
            api_base_url="https://api.moonshot.cn/v1",
            api_key_env="KIMI_API_KEY",
            api_model=None,
        ),
        loaded=True,
    )
    class _FakeSSLContext:
        def __init__(self):
            self.loaded_cafile = None

        def load_verify_locations(self, cafile=None, capath=None, cadata=None):
            _ = (capath, cadata)
            self.loaded_cafile = cafile

    ssl_capture = {"created": 0, "context": None, "urlopen_context": None}

    def fake_create_default_context(*args, **kwargs):
        _ = (args, kwargs)
        ssl_capture["created"] += 1
        ctx = _FakeSSLContext()
        ssl_capture["context"] = ctx
        return ctx

    def fake_urlopen_with_context(request_obj, timeout=0, context=None):
        _ = timeout
        ssl_capture["urlopen_context"] = context
        captured_request["url"] = request_obj.full_url
        body = json.loads(request_obj.data.decode("utf-8"))
        captured_request["model"] = body["model"]
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": "I am tired today."
                        }
                    }
                ]
            }
        )

    original_create_default_context = ssl.create_default_context
    original_urlopen = cleaner_module.cleaner_api_backend.urlrequest.urlopen
    original_key = os.environ.get("KIMI_API_KEY")
    original_ca_bundle = os.environ.get("CLEANER_API_CA_BUNDLE")
    os.environ["KIMI_API_KEY"] = "test-key"
    os.environ["CLEANER_API_CA_BUNDLE"] = "/tmp/custom-ca.pem"
    ssl.create_default_context = fake_create_default_context
    cleaner_module.cleaner_api_backend.urlrequest.urlopen = fake_urlopen_with_context
    try:
        cleaned_api_ca = api_ca_cleaner.clean("Iam tired today.")
    finally:
        ssl.create_default_context = original_create_default_context
        cleaner_module.cleaner_api_backend.urlrequest.urlopen = original_urlopen
        if original_key is None:
            os.environ.pop("KIMI_API_KEY", None)
        else:
            os.environ["KIMI_API_KEY"] = original_key
        if original_ca_bundle is None:
            os.environ.pop("CLEANER_API_CA_BUNDLE", None)
        else:
            os.environ["CLEANER_API_CA_BUNDLE"] = original_ca_bundle

    assert cleaned_api_ca == "I am tired today."
    assert ssl_capture["created"] == 1
    assert ssl_capture["context"] is not None
    assert ssl_capture["context"].loaded_cafile == "/tmp/custom-ca.pem"
    assert ssl_capture["urlopen_context"] is ssl_capture["context"]
    print("  ✓ API backend applies custom CA bundle to SSL context")

    # Test 8: API backend parser fallback for choices[].text payloads
    print("\n[8] Testing API cleaner backend parser fallback...")
    api_text_cleaner = make_partial_text_cleaner(
        CleanerConfig(
            model_name="gpt-5-nano",
            backend="api",
            api_base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            api_model="gpt-5-nano",
        ),
        loaded=True,
    )

    parser_capture = {"url": None, "model": None}

    def fake_urlopen_text_choice(request_obj, timeout=0, context=None):
        _ = timeout
        _ = context
        parser_capture["url"] = request_obj.full_url
        body = json.loads(request_obj.data.decode("utf-8"))
        parser_capture["model"] = body["model"]
        return _FakeResponse({"choices": [{"text": "I am tired today."}]})

    original_urlopen = cleaner_module.cleaner_api_backend.urlrequest.urlopen
    original_key = os.environ.get("OPENAI_API_KEY")
    os.environ["OPENAI_API_KEY"] = "test-key"
    cleaner_module.cleaner_api_backend.urlrequest.urlopen = fake_urlopen_text_choice
    try:
        cleaned_api_text = api_text_cleaner.clean("Iam tired today.")
    finally:
        cleaner_module.cleaner_api_backend.urlrequest.urlopen = original_urlopen
        if original_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = original_key

    assert cleaned_api_text == "I am tired today."
    assert parser_capture["url"] == "https://api.openai.com/v1/chat/completions"
    assert parser_capture["model"] == "gpt-5-nano"
    print("  ✓ API backend parser handles choices[].text fallback")

    # Test 9: API backend retries token field when provider rejects max_tokens
    print("\n[9] Testing API token parameter fallback (max_tokens -> max_completion_tokens)...")
    api_token_cleaner = make_partial_text_cleaner(
        CleanerConfig(
            model_name="gpt-4o-mini",
            backend="api",
            api_base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            api_model="gpt-4o-mini",
        ),
        loaded=True,
    )

    token_payloads = []

    class _Fake400Body:
        def read(self) -> bytes:
            return (
                b'{"error":{"message":"Unsupported parameter: \'max_tokens\' is not supported with this model. '
                b'Use \'max_completion_tokens\' instead.","type":"invalid_request_error","param":"max_tokens",'
                b'"code":"unsupported_parameter"}}'
            )

        def close(self) -> None:
            return

    def fake_urlopen_token_retry(request_obj, timeout=0, context=None):
        _ = timeout
        _ = context
        body = json.loads(request_obj.data.decode("utf-8"))
        token_payloads.append(body)

        if len(token_payloads) == 1:
            assert "max_tokens" in body
            assert "max_completion_tokens" not in body
            exc = urlerror.HTTPError(
                url=request_obj.full_url,
                code=400,
                msg="Bad Request",
                hdrs={},
                fp=_Fake400Body(),
            )
            exc.fp = _Fake400Body()
            raise exc

        assert "max_completion_tokens" in body
        assert "max_tokens" not in body
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": "I am tired today."
                        }
                    }
                ]
            }
        )

    original_urlopen = cleaner_module.cleaner_api_backend.urlrequest.urlopen
    original_key = os.environ.get("OPENAI_API_KEY")
    os.environ["OPENAI_API_KEY"] = "test-key"
    cleaner_module.cleaner_api_backend.urlrequest.urlopen = fake_urlopen_token_retry
    try:
        cleaned_api_token = api_token_cleaner.clean("Iam tired today.")
    finally:
        cleaner_module.cleaner_api_backend.urlrequest.urlopen = original_urlopen
        if original_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = original_key

    assert cleaned_api_token == "I am tired today."
    assert len(token_payloads) == 2
    print("  ✓ API backend retries with max_completion_tokens when max_tokens is rejected")

    # Test 10: GPT-5 models use max_completion_tokens without retry
    print("\n[10] Testing GPT-5 token parameter selection...")
    api_gpt5_cleaner = make_partial_text_cleaner(
        CleanerConfig(
            model_name="gpt-5-nano",
            backend="api",
            api_base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            api_model="gpt-5-nano",
        ),
        loaded=True,
    )

    gpt5_payloads = []

    def fake_urlopen_gpt5(request_obj, timeout=0, context=None):
        _ = timeout
        _ = context
        body = json.loads(request_obj.data.decode("utf-8"))
        gpt5_payloads.append(body)
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": "I am tired today."
                        }
                    }
                ]
            }
        )

    original_urlopen = cleaner_module.cleaner_api_backend.urlrequest.urlopen
    original_key = os.environ.get("OPENAI_API_KEY")
    os.environ["OPENAI_API_KEY"] = "test-key"
    cleaner_module.cleaner_api_backend.urlrequest.urlopen = fake_urlopen_gpt5
    try:
        cleaned_api_gpt5 = api_gpt5_cleaner.clean("Iam tired today.")
    finally:
        cleaner_module.cleaner_api_backend.urlrequest.urlopen = original_urlopen
        if original_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = original_key

    assert cleaned_api_gpt5 == "I am tired today."
    assert len(gpt5_payloads) == 1
    assert "max_completion_tokens" in gpt5_payloads[0]
    assert "max_tokens" not in gpt5_payloads[0]
    assert "temperature" not in gpt5_payloads[0]
    print("  ✓ GPT-5 requests use max_completion_tokens")

    # Test 11: o-family models should omit temperature override
    print("\n[11] Testing o-family temperature compatibility...")
    api_o3_cleaner = make_partial_text_cleaner(
        CleanerConfig(
            model_name="o3-mini",
            backend="api",
            api_base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            api_model="o3-mini",
        ),
        loaded=True,
    )

    o3_payloads = []

    def fake_urlopen_o3(request_obj, timeout=0, context=None):
        _ = timeout
        _ = context
        body = json.loads(request_obj.data.decode("utf-8"))
        o3_payloads.append(body)
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": "I am tired today."
                        }
                    }
                ]
            }
        )

    original_urlopen = cleaner_module.cleaner_api_backend.urlrequest.urlopen
    original_key = os.environ.get("OPENAI_API_KEY")
    os.environ["OPENAI_API_KEY"] = "test-key"
    cleaner_module.cleaner_api_backend.urlrequest.urlopen = fake_urlopen_o3
    try:
        cleaned_api_o3 = api_o3_cleaner.clean("Iam tired today.")
    finally:
        cleaner_module.cleaner_api_backend.urlrequest.urlopen = original_urlopen
        if original_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = original_key

    assert cleaned_api_o3 == "I am tired today."
    assert len(o3_payloads) == 1
    assert "max_completion_tokens" in o3_payloads[0]
    assert "temperature" not in o3_payloads[0]
    print("  ✓ o-family requests omit temperature override")

    print("\n" + "="*50)
    print("RULE-BASED TESTS PASSED ✓")
    print("="*50 + "\n")
    print("Note: Full LLM-based cleaning requires model download (~2GB)")
    print("Run manually with: python -c \"from modules.tts import TextCleaner; c=TextCleaner(); print(c.clean('Test 123'))\"")
    
    return True


def test_cleaner_transport_generate_api_response_async_uses_existing_cleaner_hooks():
    observed = {}

    async def _post_api_chat_completions_with_retry_async(**kwargs):
        observed["post_kwargs"] = kwargs
        return '{"choices":[{"message":{"content":"cleaned text"}}]}'

    cleaner = SimpleNamespace(
        config=SimpleNamespace(
            api_global_limiter=None,
            max_tokens=99,
            temperature=0.2,
        ),
        _build_api_ssl_context=lambda: "ssl-context",
        _post_api_chat_completions_with_retry_async=_post_api_chat_completions_with_retry_async,
        _parse_api_response_text=lambda response_body: f"parsed::{response_body}",
    )

    original_resolve_api_runtime = cleaner_module.cleaner_api_backend.resolve_api_runtime
    original_build_api_payload = cleaner_module.cleaner_api_backend.build_api_payload
    cleaner_module.cleaner_api_backend.resolve_api_runtime = lambda config: (  # noqa: ARG005
        "https://api.example.test/v1",
        "key-123",
        "gpt-5-nano",
    )
    cleaner_module.cleaner_api_backend.build_api_payload = lambda **kwargs: {  # noqa: ARG005
        "payload": "ok",
    }
    try:
        result = asyncio.run(
            generate_api_response_async(
                cleaner,
                "prompt text",
                http_error_cls=RuntimeError,
                aiohttp_module="fake-aiohttp",
            )
        )
    finally:
        cleaner_module.cleaner_api_backend.resolve_api_runtime = original_resolve_api_runtime
        cleaner_module.cleaner_api_backend.build_api_payload = original_build_api_payload

    assert result == 'parsed::{"choices":[{"message":{"content":"cleaned text"}}]}'
    assert observed["post_kwargs"]["base_url"] == "https://api.example.test/v1"
    assert observed["post_kwargs"]["api_key"] == "key-123"
    assert observed["post_kwargs"]["payload"] == {"payload": "ok"}
    assert observed["post_kwargs"]["ssl_context"] == "ssl-context"
    assert observed["post_kwargs"]["aiohttp_module"] == "fake-aiohttp"


def test_mlx_clean_batch_with_trace_async_serializes_local_model_calls():
    """MLX batch trace cleaning must not fan out concurrent local generations."""

    class _FakeCleaner:
        def __init__(self):
            self._lock = threading.Lock()
            self.active_calls = 0
            self.peak_active_calls = 0

        def clean_with_trace(self, text: str):
            with self._lock:
                self.active_calls += 1
                self.peak_active_calls = max(self.peak_active_calls, self.active_calls)
            try:
                time.sleep(0.02)
                return SimpleNamespace(
                    original_text=text,
                    rule_based_text=text,
                    cleaned_text=f"cleaned {text}",
                    mode="llm_success",
                    llm_output=None,
                    llm_error=None,
                )
            finally:
                with self._lock:
                    self.active_calls -= 1

    cleaner = _FakeCleaner()
    strategy = CleanerMLXStrategy()
    texts = [f"chunk {idx}" for idx in range(6)]

    traces = asyncio.run(strategy.clean_batch_with_trace_async(cleaner, texts))

    assert [trace.cleaned_text for trace in traces] == [f"cleaned {text}" for text in texts]
    assert cleaner.peak_active_calls == 1


def test_trace_decision_scores_before_rule_only_gate_when_llm_not_required():
    from modules.tts.cleaner_trace import _start_trace_decision_flow
    from modules.tts.cleaner import _CleanTrace

    score_calls: list[str] = []
    cleaner = SimpleNamespace(
        _rule_based_clean=lambda text: f"{text} cleaned",
        _needs_llm_cleaning=lambda text: False,
        _artifact_score_for=lambda text: (
            score_calls.append(text) or {"total": 0.0, "cleaning_path": "rules_only"}
        ),
        _should_use_rule_only_cleaning=lambda rule_based_text, artifact_score: True,
        _loaded=True,
        _rule_only_count=0,
        _total_chunks_cleaned=0,
        _total_time_ms=0.0,
    )

    result = _start_trace_decision_flow(cleaner, _CleanTrace, "clean prose")

    assert score_calls == ["clean prose cleaned"]
    assert result.mode == "rule_only"


def test_trace_decision_defers_artifact_scoring_when_llm_heuristics_trigger():
    from modules.tts.cleaner_trace import TraceDecisionRequest, _start_trace_decision_flow

    score_calls: list[str] = []
    cleaner = SimpleNamespace(
        _rule_based_clean=lambda text: text,
        _needs_llm_cleaning=lambda text: True,
        _artifact_score_for=lambda text: (
            score_calls.append(text) or {"total": 0.5, "cleaning_path": "light_llm"}
        ),
        _should_use_rule_only_cleaning=lambda rule_based_text, artifact_score: False,
        _loaded=True,
        load=lambda: None,
    )

    result = _start_trace_decision_flow(cleaner, TraceDecisionRequest, "bad- hy phen")

    assert score_calls == []
    assert isinstance(result, TraceDecisionRequest)
    assert result.artifact_score is None


if __name__ == "__main__":
    success = test_rule_based_cleaning()
    sys.exit(0 if success else 1)
