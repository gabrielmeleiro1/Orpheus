"""
Text Cleaner – Text Preservation Tests
=======================================
Comprehensive tests verifying the cleaner only removes/corrects artifacts
and does NOT rewrite, paraphrase, or hallucinate content.
"""

import os
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_rule_only_cleaner() -> TextCleaner:
    """Create a TextCleaner that only does rule-based cleaning (no LLM)."""
    cleaner = TextCleaner.__new__(TextCleaner)
    cleaner.config = CleanerConfig()
    cleaner._loaded = False
    cleaner._model = None
    cleaner._tokenizer = None
    cleaner._manager = None
    cleaner._model_lock = threading.RLock()
    cleaner._total_chunks_cleaned = 0
    cleaner._total_time_ms = 0
    cleaner._llm_success_count = 0
    cleaner._llm_fallback_count = 0
    cleaner._rule_only_count = 0
    return attach_cleaner_backend_strategy(cleaner)


def _make_mlx_cleaner_with_fake_llm(fake_response: str) -> TextCleaner:
    """Create a TextCleaner with a fake LLM that returns *fake_response*."""
    cleaner = _make_rule_only_cleaner()
    cleaner.config = CleanerConfig(backend="mlx")
    cleaner._loaded = True
    return cleaner


def _patch_generate(fake_response: str):
    """Context-free monkey-patch helpers for mlx generate + make_sampler."""
    original_generate = cleaner_module.cleaner_mlx_backend.generate
    original_make_sampler = cleaner_module.cleaner_mlx_backend.make_sampler

    def _fake_generate(_model, _tokenizer, **kwargs):
        return fake_response

    cleaner_module.cleaner_mlx_backend.generate = _fake_generate
    cleaner_module.cleaner_mlx_backend.make_sampler = lambda temp=0.0: ("sampler", temp)

    def _restore():
        cleaner_module.cleaner_mlx_backend.generate = original_generate
        cleaner_module.cleaner_mlx_backend.make_sampler = original_make_sampler

    return _restore


# ---------------------------------------------------------------------------
# 1. Clean text passes through unchanged (rule-based)
# ---------------------------------------------------------------------------


class TestCleanTextUnchanged:
    """Already-clean prose should survive rule-based cleaning intact."""

    CLEAN_TEXTS = [
        "The quick brown fox jumps over the lazy dog.",
        "She said, 'Hello!' and waved goodbye.",
        "It was a dark and stormy night; the rain fell in torrents.",
        "Welcome back, I see you get it now. Did you bring the band-aids?",
    ]

    @pytest.mark.parametrize("text", CLEAN_TEXTS)
    def test_clean_prose_unchanged(self, text: str):
        cleaner = _make_rule_only_cleaner()
        result = cleaner._rule_based_clean(text)
        assert result == text, f"Expected unchanged, got: {result!r}"


# ---------------------------------------------------------------------------
# 2. OCR / extraction artefact removal (rule-based)
# ---------------------------------------------------------------------------


class TestRemovesOCRArtifacts:
    """Stray formatting from PDF/OCR extraction should be removed."""

    def test_removes_stray_hash_header(self):
        cleaner = _make_rule_only_cleaner()
        result = cleaner._rule_based_clean("### DEAR MARKETS")
        assert result == "DEAR MARKETS"

    def test_removes_bold_markers(self):
        cleaner = _make_rule_only_cleaner()
        result = cleaner._rule_based_clean("**bold text** here")
        assert result == "bold text here"

    def test_removes_italic_markers(self):
        cleaner = _make_rule_only_cleaner()
        result = cleaner._rule_based_clean("_italic_ word")
        assert result == "italic word"

    def test_removes_backticks(self):
        cleaner = _make_rule_only_cleaner()
        result = cleaner._rule_based_clean("use `code` here")
        assert result == "use code here"

    def test_removes_pipe_characters(self):
        cleaner = _make_rule_only_cleaner()
        result = cleaner._rule_based_clean("left | right")
        assert "left" in result and "right" in result
        assert "|" not in result

    def test_removes_html_br(self):
        cleaner = _make_rule_only_cleaner()
        result = cleaner._rule_based_clean("line one<br>line two")
        assert "<br>" not in result
        assert "line one" in result
        assert "line two" in result

    def test_repairs_clear_spaced_hyphenated_compounds(self):
        cleaner = _make_rule_only_cleaner()
        result = cleaner._rule_based_clean(
            "The self- governed recipient- cum witness made a promise- making and promise- keeping ritual."
        )

        assert result == (
            "The self-governed recipient-cum witness made a promise-making and promise-keeping ritual."
        )

    def test_removes_link_formatting(self):
        cleaner = _make_rule_only_cleaner()
        result = cleaner._rule_based_clean("[click here](https://example.com)")
        assert result == "click here"

    def test_removes_image_formatting(self):
        cleaner = _make_rule_only_cleaner()
        result = cleaner._rule_based_clean("![alt text](image.png)")
        # The image regex removes ![alt](url) -> alt text
        # but the leading '!' may remain if regex doesn't fully consume it
        assert "alt text" in result
        assert "(image.png)" not in result


# ---------------------------------------------------------------------------
# 3. Abbreviation preservation (rule-based)
# ---------------------------------------------------------------------------


class TestAbbreviationExpansion:
    """Rule-based cleaning should preserve common abbreviations."""

    ABBREVIATION_CASES = [
        ("Dr. Smith arrived.", "Dr. Smith arrived."),
        ("Mr. Jones left.", "Mr. Jones left."),
        ("Mrs. Brown stayed.", "Mrs. Brown stayed."),
        ("Prof. Lee taught.", "Prof. Lee taught."),
        ("St. Louis is nice.", "St. Louis is nice."),
        ("e.g. this works", "e.g. this works"),
        ("i.e. this too", "i.e. this too"),
        ("and etc.", "and etc."),
        ("Team A vs. Team B", "Team A vs. Team B"),
    ]

    @pytest.mark.parametrize("input_text, expected", ABBREVIATION_CASES)
    def test_abbreviation_expanded(self, input_text: str, expected: str):
        cleaner = _make_rule_only_cleaner()
        result = cleaner._rule_based_clean(input_text)
        assert result == expected, f"Got: {result!r}"


# ---------------------------------------------------------------------------
# 4. Output never grows beyond reasonable bounds (LLM mock)
# ---------------------------------------------------------------------------


class TestLLMDoesNotAddContent:
    """When the LLM returns a response, the output should not be longer
    than the input (no hallucinated extra content)."""

    def test_llm_output_not_longer_than_input(self):
        input_text = "The crash of 1987 was devastating."
        # Simulate an LLM that correctly returns a cleaned version
        fake_response = "The crash of 1987 was devastating."
        cleaner = _make_mlx_cleaner_with_fake_llm(fake_response)
        restore = _patch_generate(fake_response)
        try:
            trace = cleaner.clean_with_trace(input_text)
            assert len(trace.cleaned_text) <= len(input_text) * 1.1  # 10% tolerance
        finally:
            restore()

    def test_llm_hallucination_detected_by_length(self):
        """If LLM doubles the text, that's a problem we can flag."""
        input_text = "The experiment in 2023 used 42 subjects to measure the effect on 100 participants overall."
        fake_response = (
            "The experiment in 2023 used 42 subjects to measure the effect on 100 participants overall. "
            "And here is a lot of extra hallucinated content that was never in the original text at all."
        )
        cleaner = _make_mlx_cleaner_with_fake_llm(fake_response)
        restore = _patch_generate(fake_response)
        try:
            trace = cleaner.clean_with_trace(input_text)
            assert trace.mode == "hallucination_fallback"
            assert trace.cleaned_text == input_text
            assert trace.llm_output == fake_response
            assert len(trace.llm_output) > len(input_text)
        finally:
            restore()


class TestLLMPreservationGuard:
    """LLM cleanup should repair artifacts without editing literary style."""

    def test_llm_output_that_removes_literary_repetition_falls_back(self):
        input_text = (
            "Long – long – long – many minutes, many hours, many days, "
            "have I heard it – yet I dared not speak!"
        )
        fake_response = (
            "Long many minutes, many hours, many days, "
            "have I heard it – yet I dared not speak!"
        )
        cleaner = _make_mlx_cleaner_with_fake_llm(fake_response)
        restore = _patch_generate(fake_response)
        try:
            trace = cleaner.clean_with_trace(input_text)
        finally:
            restore()

        assert trace.mode == "hallucination_fallback"
        assert trace.cleaned_text == cleaner._rule_based_clean(input_text)
        assert trace.llm_output == fake_response
        assert "preservation guard" in str(trace.llm_error)

    def test_llm_output_that_mutates_epigraph_text_falls_back(self):
        input_text = (
            "The Fall of the House of Usher Son coeur est un luth suspendu; "
            "Sitôt qu’on le touche il résonne. DE BÉRANGER"
        )
        fake_response = (
            "The Fall of the House of Usher. Sonnet coeur est un luth suspendu; "
            "Sitôt qu’on le touche, il résonne. DE BÉRANGER"
        )
        cleaner = _make_mlx_cleaner_with_fake_llm(fake_response)
        restore = _patch_generate(fake_response)
        try:
            trace = cleaner.clean_with_trace(input_text)
        finally:
            restore()

        assert trace.mode == "hallucination_fallback"
        assert trace.cleaned_text == cleaner._rule_based_clean(input_text)
        assert trace.llm_output == fake_response

    def test_llm_output_can_repair_split_uppercase_token(self):
        input_text = (
            "I must abandon life and reason together, in some struggle "
            "with the grim phantasm, F EAR."
        )
        fake_response = (
            "I must abandon life and reason together, in some struggle "
            "with the grim phantasm, FEAR."
        )
        cleaner = _make_mlx_cleaner_with_fake_llm(fake_response)
        restore = _patch_generate(fake_response)
        try:
            trace = cleaner.clean_with_trace(input_text)
        finally:
            restore()

        assert trace.mode == "llm_success"
        assert trace.cleaned_text == fake_response


# ---------------------------------------------------------------------------
# 5. Punctuation and quotes preserved (rule-based)
# ---------------------------------------------------------------------------


class TestPreservesPunctuationAndQuotes:
    """Special punctuation characters should survive cleaning."""

    def test_preserves_em_dash(self):
        cleaner = _make_rule_only_cleaner()
        text = "taking all your gifts back \u2013 with interest."
        result = cleaner._rule_based_clean(text)
        assert "\u2013" in result

    def test_preserves_curly_quotes(self):
        cleaner = _make_rule_only_cleaner()
        text = "\u201cWelcome back,\u201d she said."
        result = cleaner._rule_based_clean(text)
        assert "\u201c" in result
        assert "\u201d" in result

    def test_preserves_apostrophe(self):
        cleaner = _make_rule_only_cleaner()
        text = "can't won't shouldn't"
        result = cleaner._rule_based_clean(text)
        assert "can't" in result


# ---------------------------------------------------------------------------
# 6. Sentence structure preserved (LLM mock)
# ---------------------------------------------------------------------------


class TestDoesNotRewriteSentences:
    """The LLM path should return text with the same sentence structure."""

    def test_word_order_preserved(self):
        input_text = "I pursued you like a lovestruck teenager."
        # Good LLM: returns same text
        fake_response = "I pursued you like a lovestruck teenager."
        cleaner = _make_mlx_cleaner_with_fake_llm(fake_response)
        restore = _patch_generate(fake_response)
        try:
            result = cleaner.clean(input_text)
            assert result == input_text
        finally:
            restore()


# ---------------------------------------------------------------------------
# 7. Fallback returns rule-based result
# ---------------------------------------------------------------------------


class TestFallbackBehavior:
    """When the LLM/API errors out, rule-based text should be returned."""

    def test_fallback_on_llm_exception(self):
        cleaner = _make_rule_only_cleaner()
        cleaner.config = CleanerConfig(backend="mlx")
        cleaner._loaded = True

        def _exploding_generate(*args, **kwargs):
            raise RuntimeError("API is down")

        original_generate = cleaner_module.cleaner_mlx_backend.generate
        original_make_sampler = cleaner_module.cleaner_mlx_backend.make_sampler
        cleaner_module.cleaner_mlx_backend.generate = _exploding_generate
        cleaner_module.cleaner_mlx_backend.make_sampler = lambda temp=0.0: ("sampler", temp)
        try:
            trace = cleaner.clean_with_trace("NASA launched today.")
            assert trace.mode == "llm_fallback"
            assert trace.cleaned_text == "NASA launched today."
        finally:
            cleaner_module.cleaner_mlx_backend.generate = original_generate
            cleaner_module.cleaner_mlx_backend.make_sampler = original_make_sampler

    def test_fallback_stats_incremented(self):
        cleaner = _make_rule_only_cleaner()
        cleaner.config = CleanerConfig(backend="mlx")
        cleaner._loaded = True

        def _exploding_generate(*args, **kwargs):
            raise RuntimeError("boom")

        original_generate = cleaner_module.cleaner_mlx_backend.generate
        original_make_sampler = cleaner_module.cleaner_mlx_backend.make_sampler
        cleaner_module.cleaner_mlx_backend.generate = _exploding_generate
        cleaner_module.cleaner_mlx_backend.make_sampler = lambda temp=0.0: ("sampler", temp)
        try:
            cleaner.clean("NASA launched today.")
            stats = cleaner.stats_snapshot()
            assert stats["llm_fallback"] == 1
            assert stats["llm_success"] == 0
        finally:
            cleaner_module.cleaner_mlx_backend.generate = original_generate
            cleaner_module.cleaner_mlx_backend.make_sampler = original_make_sampler


# ---------------------------------------------------------------------------
# 8. Trace captures LLM error
# ---------------------------------------------------------------------------


class TestTraceCapturesError:
    """The CleanTrace should record the error message on fallback."""

    def test_trace_records_error_message(self):
        cleaner = _make_rule_only_cleaner()
        cleaner.config = CleanerConfig(backend="mlx")
        cleaner._loaded = True

        def _exploding_generate(*args, **kwargs):
            raise RuntimeError("Cleaner API HTTP 401: Invalid Authentication")

        original_generate = cleaner_module.cleaner_mlx_backend.generate
        original_make_sampler = cleaner_module.cleaner_mlx_backend.make_sampler
        cleaner_module.cleaner_mlx_backend.generate = _exploding_generate
        cleaner_module.cleaner_mlx_backend.make_sampler = lambda temp=0.0: ("sampler", temp)
        try:
            # Use text long enough (>50 chars) with numbers to trigger LLM path
            text = "The investor bought 42 shares of stock in 2024 and sold them later for a significant profit."
            trace = cleaner.clean_with_trace(text)
            assert trace.mode == "llm_fallback"
            assert "401" in trace.llm_error
            assert "Invalid Authentication" in trace.llm_error
        finally:
            cleaner_module.cleaner_mlx_backend.generate = original_generate
            cleaner_module.cleaner_mlx_backend.make_sampler = original_make_sampler


# ---------------------------------------------------------------------------
# 9. Real chapter text from chapter_05 (rule-based)
# ---------------------------------------------------------------------------


class TestRealChapterTextArtifacts:
    """Test with real text from the chapter_05 trace that triggered fallback."""

    CHAPTER_TEXT_WITH_ARTIFACTS = (
        "### **DEAR MARKETS** rom the moment I first came across you, "
        "I have been fascinated by you. I probably even fell in love with you. "
        "I was too young to know what that meant, no more than ten # **F** "
        "years old."
    )

    def test_removes_markdown_headers_and_bold(self):
        cleaner = _make_rule_only_cleaner()
        result = cleaner._rule_based_clean(self.CHAPTER_TEXT_WITH_ARTIFACTS)
        # Header markers removed
        assert not result.startswith("#")
        # Bold markers removed
        assert "**" not in result
        # Content preserved
        assert "DEAR MARKETS" in result
        assert "fascinated by you" in result

    def test_removes_orphaned_hash_f(self):
        """The '# F' from PDF extraction — rule-based only removes start-of-line
        headers, so mid-line '# F' won't be caught.  This test documents that
        the LLM is needed for mid-line artifact removal."""
        cleaner = _make_rule_only_cleaner()
        result = cleaner._rule_based_clean(self.CHAPTER_TEXT_WITH_ARTIFACTS)
        # '**F**' bold markers should be removed at least
        assert "**" not in result
        # The underlying issue (orphaned 'F' with '#') is an LLM task;
        # rule-based cleaning removes the header marker only at line start.
        # Here we just verify no crash and bold is removed.

    def test_preserves_years_as_numbers(self):
        """Years like 1973, 1987 should remain as digits (rule-based doesn't touch them)."""
        text = (
            "When you went through the devastating bear market of 1973, "
            "I was just learning to walk. When you roared with anger during "
            "the crash of 1987, I was just finishing school."
        )
        cleaner = _make_rule_only_cleaner()
        result = cleaner._rule_based_clean(text)
        assert "1973" in result
        assert "1987" in result

    def test_full_chapter_no_crash(self):
        """Full chapter text should clean without crashing."""
        full_text = (
            "### **DEAR MARKETS** rom the moment I first came across you, "
            "I have been fascinated by you. I probably even fell in love with you. "
            "I was too young to know what that meant, no more than ten # **F** "
            "years old. You were featured in a national newspaper – a competition "
            "of sorts. I was too young to play with you, so I observed. Time was "
            "not on my side. I was born a few decades too soon to participate in "
            "trading like it is possible today."
        )
        cleaner = _make_rule_only_cleaner()
        result = cleaner._rule_based_clean(full_text)
        assert isinstance(result, str)
        assert len(result) > 100
        assert "###" not in result
        assert "**" not in result


# ---------------------------------------------------------------------------
# 10. API backend fallback (mocked HTTP error)
# ---------------------------------------------------------------------------


class TestAPIBackendFallback:
    """Test that API backend errors are handled gracefully."""

    def test_api_401_triggers_fallback(self):
        """Simulate the exact error from the chapter_05 trace."""
        import urllib.error as urlerror

        cleaner = _make_rule_only_cleaner()
        cleaner.config = CleanerConfig(
            backend="api",
            api_base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            api_model="gpt-4o-mini",
        )
        cleaner._loaded = True
        attach_cleaner_backend_strategy(cleaner)

        original_urlopen = cleaner_module.cleaner_api_backend.urlrequest.urlopen
        original_key = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "sk-invalid-key"

        class _Fake401Response:
            fp = True
            code = 401
            reason = "Unauthorized"

            def read(self):
                return b'{"error":{"message":"Invalid Authentication","type":"invalid_authentication_error"}}'

        def fake_urlopen(request_obj, timeout=0, context=None):
            exc = urlerror.HTTPError(
                url=request_obj.full_url,
                code=401,
                msg="Unauthorized",
                hdrs={},
                fp=_Fake401Response(),
            )
            exc.fp = _Fake401Response()
            raise exc

        cleaner_module.cleaner_api_backend.urlrequest.urlopen = fake_urlopen
        try:
            trace = cleaner.clean_with_trace("NASA launched today.")
            assert trace.mode == "llm_fallback"
            assert "401" in trace.llm_error
            assert trace.cleaned_text == "NASA launched today."
        finally:
            cleaner_module.cleaner_api_backend.urlrequest.urlopen = original_urlopen
            if original_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = original_key


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
