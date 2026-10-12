"""
Property-Based Tests for Text Cleaner/Normalizer
=================================================
Uses Hypothesis for fuzz testing text normalization edge cases.
Tests rule-based cleaning and text transformations.
"""

import pytest
import re
from hypothesis import HealthCheck, given, strategies as st, settings, assume
from unittest.mock import patch, MagicMock

# Import the cleaner module components
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.tts.cleaner import (
    TextCleaner,
)
from modules.tts.cleaner_config import CleanerConfig
from modules.tts.cleaner_mlx import (
    clear_cleaner_cache as _clear_cleaner_cache,
    get_cached_cleaner as _get_cached_cleaner,
    set_cached_cleaner as _set_cached_cleaner,
)

pytestmark = pytest.mark.property


# Strategy for generating text with markdown
markdown_elements = st.one_of(
    # Headers
    st.builds(lambda t: f"# {t}", st.text(min_size=1, max_size=50)),
    st.builds(lambda t: f"## {t}", st.text(min_size=1, max_size=50)),
    # Bold/Italic
    st.builds(lambda t: f"**{t}**", st.text(min_size=1, max_size=30)),
    st.builds(lambda t: f"_{t}_", st.text(min_size=1, max_size=30)),
    # Links
    st.builds(lambda t, u: f"[{t}]({u})", 
              st.text(min_size=1, max_size=20),
              st.text(min_size=5, max_size=50)),
    # Plain text
    st.text(min_size=1, max_size=100),
)


# Strategy for text with abbreviations
abbreviation_text = st.one_of(
    st.just("Dr. Smith went to the hospital."),
    st.just("Mr. and Mrs. Johnson live at 123 St. James St."),
    st.just("Prof. Anderson teaches math, e.g. algebra and calculus."),
    st.just("The meeting is at 3 p.m., i.e. after lunch."),
    st.just("Team A vs. Team B"),
    st.just("John Doe, Jr. and his father, Sr."),
    st.builds(lambda: st.text(min_size=10, max_size=200)),
)


class TestTextCleanerRuleBased:
    """Property-based tests for rule-based text cleaning."""

    @staticmethod
    def _rule_only_cleaner() -> TextCleaner:
        return TextCleaner(CleanerConfig(backend="api"))
    
    @pytest.mark.property
    @given(st.text(min_size=0, max_size=1000))
    @settings(max_examples=200, deadline=5000)
    def test_rule_cleaning_never_crashes(self, text):
        """
        Rule-based cleaning should never crash on any input.
        """
        # Create cleaner with mocked load to avoid actual model loading
        with patch.object(TextCleaner, 'load'):
            cleaner = self._rule_only_cleaner()
            # Access the _rule_based_clean method directly
            try:
                result = cleaner._rule_based_clean(text)
                assert isinstance(result, str)
            except Exception as e:
                pytest.fail(f"Rule-based cleaning crashed on: {repr(text[:100])}... Error: {e}")
    
    @pytest.mark.property
    @given(markdown_elements)
    @settings(max_examples=150, deadline=5000)
    def test_markdown_formatting_removed(self, text):
        """
        Markdown formatting should be removed or simplified.
        
        Headers (#), bold (**), italics (_), links should be cleaned.
        """
        with patch.object(TextCleaner, 'load'):
            cleaner = self._rule_only_cleaner()
            result = cleaner._rule_based_clean(text)
            
            # Basic sanity checks
            assert isinstance(result, str)
            
            # Markdown headers at start of lines should be removed
            assert not re.match(r'^#{1,6}\s', result, re.MULTILINE)
            
            # Should not have standalone doubled asterisks around text
            # (single asterisks might remain for other purposes)
            assert '**' not in result or result.count('**') == 0

    def test_nested_markdown_asterisks_do_not_leave_bold_delimiters(self):
        with patch.object(TextCleaner, 'load'):
            cleaner = self._rule_only_cleaner()

            assert cleaner._rule_based_clean("**0*0**") == "00"
    
    @pytest.mark.property
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Dr. Smith returned home.", "Dr. Smith returned home."),
            ("Mr. Jones arrived.", "Mr. Jones arrived."),
            ("Mrs. Jones arrived.", "Mrs. Jones arrived."),
            ("Ms. Lee spoke.", "Ms. Lee spoke."),
            ("Prof. Lee taught.", "Prof. Lee taught."),
            ("St. Louis is nice.", "St. Louis is nice."),
            ("Bring books, pens, etc.", "Bring books, pens, etc."),
            ("Choose fruit, e.g. apples.", "Choose fruit, e.g. apples."),
            ("He left, i.e. went home.", "He left, i.e. went home."),
            ("Team Alpha vs. Team Beta", "Team Alpha vs. Team Beta"),
        ],
    )
    @given(
        suffix=st.lists(
            st.sampled_from(["the", "traveler", "read", "another", "quiet", "chapter"]),
            min_size=1, max_size=20,
        ).map(" ".join),
    )
    @settings(max_examples=100, deadline=5000)
    def test_abbreviations_preserved(self, text, expected, suffix):
        """Keep source abbreviations and surrounding prose unchanged."""
        with patch.object(TextCleaner, 'load'):
            cleaner = self._rule_only_cleaner()
            result = cleaner._rule_based_clean(f"{text} {suffix}")

            assert isinstance(result, str)
            assert result == f"{expected} {suffix}"
    
    @pytest.mark.property
    @given(st.text(alphabet=" \t\n\r\x0b\x0c\u00a0\u2003", max_size=500))
    @settings(max_examples=100, deadline=3000)
    def test_empty_and_whitespace_handled(self, text):
        """
        Empty or whitespace-only input should return empty string.
        """
        with patch.object(TextCleaner, 'load'):
            cleaner = self._rule_only_cleaner()

            assert cleaner.clean(text) == ""

    @pytest.mark.property
    @given(st.text(min_size=1, max_size=500))
    @settings(
        max_examples=100,
        deadline=3000,
        suppress_health_check=[HealthCheck.filter_too_much],
    )
    def test_content_not_lost(self, text):
        """
        Substantial content should not be lost during cleaning.
        
        While formatting may change, the core text should remain.
        """
        # Filter out mostly formatting characters
        core_content = re.sub(r'[#*_\[\]\(\)!`]', '', text)
        assume(len(core_content) > 10)  # Need some actual content
        
        with patch.object(TextCleaner, 'load'):
            cleaner = self._rule_only_cleaner()
            result = cleaner._rule_based_clean(text)
            
            # Result should have some content
            assert len(result) > 0
            
            # Core alphabetic content should be mostly preserved
            result_letters = set(re.sub(r'[^a-zA-Z]', '', result).lower())
            original_letters = set(re.sub(r'[^a-zA-Z]', '', core_content).lower())
            
            # Most letters from original should be in result
            if original_letters:
                preserved_ratio = len(result_letters & original_letters) / len(original_letters)
                assert preserved_ratio >= 0.5, f"Lost too much content: {preserved_ratio:.2%}"


class TestTextCleanerLLMSemantics:
    """Tests for LLM-routing semantics."""

    @pytest.mark.property
    @given(st.text(min_size=1, max_size=100))
    @settings(max_examples=50, deadline=3000)
    def test_short_text_uses_rule_based_only(self, text):
        """
        Short text without numbers or OCR-fragment markers should use rule-based cleaning only.
        """
        assume(len(text) < 50)
        assume(not re.search(r'\d', text))  # No numbers
        assume(not re.search(r"\b(?!I\b|A\b)[B-HJ-Z]\b", text))
        
        with patch.object(TextCleaner, 'load'):
            cleaner = TextCleaner(CleanerConfig(backend="api"))
            
            # Should not need LLM
            assert not cleaner._needs_llm_cleaning(text)


class TestTextCleanerErrorHandling:
    """Tests for error handling and edge cases."""

    @staticmethod
    def _mocked_mlx_cleaner() -> TextCleaner:
        with patch("modules.tts.cleaner_mlx.ensure_mlx_lm_runtime"):
            return TextCleaner()
    
    @pytest.mark.property
    @given(st.text(min_size=0, max_size=1000))
    @settings(max_examples=100, deadline=3000)
    def test_fallback_on_error(self, text):
        """
        If LLM cleaning fails, should fallback to rule-based result.
        """
        with patch.object(TextCleaner, 'load'):
            cleaner = self._mocked_mlx_cleaner()
            cleaner._loaded = True
            cleaner._model = MagicMock()
            cleaner._tokenizer = MagicMock()

            # Empty input exits before routing. Keep arbitrary generated suffixes
            # while ensuring this property actually exercises the failure branch.
            text = f"Preserve this text. {text}"
            expected = cleaner._rule_based_clean(text)
            with (
                patch.object(cleaner, '_needs_llm_cleaning', return_value=True),
                patch(
                    'modules.tts.cleaner_mlx.generate', side_effect=RuntimeError("LLM Error"),
                ) as generate,
            ):
                result = cleaner.clean(text)
                generate.assert_called_once()
                assert isinstance(result, str)
                assert result == expected


class TestTextCleanerCaching:
    """Tests for model caching behavior."""
    
    @pytest.mark.property
    @given(model_name=st.text(min_size=1, max_size=100))
    @settings(max_examples=50, deadline=3000)
    def test_cache_operations(self, model_name):
        """
        Cache get/set operations should work correctly.
        """
        # Clear cache first
        _clear_cleaner_cache()
        
        # Initially should be None
        assert _get_cached_cleaner(model_name) is None
        
        # Set a mock cache entry
        mock_model = MagicMock()
        mock_tokenizer = MagicMock()
        _set_cached_cleaner(model_name, mock_model, mock_tokenizer)
        
        # Should be retrievable
        cached = _get_cached_cleaner(model_name)
        assert cached is not None
        assert cached == (mock_model, mock_tokenizer)
        
        # Clear and verify
        _clear_cleaner_cache()
        assert _get_cached_cleaner(model_name) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-m", "property", "-o", "addopts="]))
