"""Regression checks that property contracts reject corrupted outputs."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests import test_text_cleaner_hypothesis as cleaner_properties
from tests import test_tts_chunker_hypothesis as chunker_properties


@pytest.mark.parametrize(
    "bad_chunks",
    [
        [],
        [""],
        ["Z" * 40],
        ["First sentence keeps all original words." * 2],
        ["words. original all keeps sentence First"],
        ["First sentence keeps all original word."],
    ],
    ids=["empty", "empty-chunk", "replacement", "duplication", "reordering", "loss"],
)
def test_chunk_preservation_property_rejects_corruption(monkeypatch, bad_chunks):
    monkeypatch.setattr(
        chunker_properties.TextChunker, "chunk", lambda self, text: bad_chunks,
    )
    contract = chunker_properties.TestTextChunkerProperties()
    with pytest.raises(AssertionError):
        contract.test_chunked_text_joins_to_original.hypothesis.inner_test(
            contract, "First sentence keeps all original words.", 100,
        )


@pytest.mark.parametrize(
    "text,max_tokens",
    [
        ("  First\t sentence.\nSecond\u2003sentence!  ", 100),
        ("ab\u00e9\u4e2d" * 150, 10),
    ],
    ids=["normalized-whitespace", "hard-split-token"],
)
def test_chunk_preservation_property_accepts_real_output(text, max_tokens):
    contract = chunker_properties.TestTextChunkerProperties()
    contract.test_chunked_text_joins_to_original.hypothesis.inner_test(
        contract, text, max_tokens,
    )


@pytest.mark.parametrize(
    "bad_text",
    ["Smith returned home.", "Dr. Jones returned home.", "Doctor Smith returned home."],
    ids=["deletion", "rewriting", "expansion"],
)
def test_abbreviation_property_rejects_corruption(monkeypatch, bad_text):
    monkeypatch.setattr(
        cleaner_properties.TextCleaner, "_rule_based_clean",
        lambda self, text: text.replace("Dr. Smith returned home.", bad_text),
    )
    contract = cleaner_properties.TestTextCleanerRuleBased()
    with pytest.raises(AssertionError):
        contract.test_abbreviations_preserved.hypothesis.inner_test(
            contract, "Dr. Smith returned home.", "Dr. Smith returned home.", "the traveler read",
        )


@pytest.mark.parametrize("mutation", ["empty", "rewritten", "bypassed"])
def test_fallback_property_rejects_corruption(monkeypatch, mutation):
    original_clean = cleaner_properties.TextCleaner.clean

    def corrupt_clean(cleaner, text):
        if mutation == "bypassed":
            return cleaner._rule_based_clean(text)
        original_clean(cleaner, text)
        return "" if mutation == "empty" else "An unrelated replacement sentence."

    monkeypatch.setattr(cleaner_properties.TextCleaner, "clean", corrupt_clean)
    contract = cleaner_properties.TestTextCleanerErrorHandling()
    with pytest.raises(AssertionError):
        contract.test_fallback_on_error.hypothesis.inner_test(
            contract, "Preserve this meaningful sentence after the model error.",
        )


@pytest.mark.parametrize(
    "script",
    [
        "test_tts_chunker_hypothesis.py",
        "test_text_cleaner_hypothesis.py",
        "test_file_parsing_hypothesis.py",
    ],
)
@pytest.mark.parametrize("pytest_exit", [0, 1, 2, 5])
def test_property_script_propagates_pytest_exit(script, pytest_exit):
    root = Path(__file__).resolve().parents[1]
    code = (
        "import pytest, runpy; "
        f"pytest.main=lambda *args, **kwargs: {pytest_exit}; "
        f"runpy.run_path({str(Path('tests') / script)!r}, run_name='__main__')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=root,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == pytest_exit, result.stderr


@pytest.mark.parametrize(
    "script",
    [
        "test_tts_chunker_hypothesis.py",
        "test_text_cleaner_hypothesis.py",
        "test_file_parsing_hypothesis.py",
    ],
)
def test_property_script_collects_when_run_by_file_path(script):
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    # Run the real interpreter's file entrypoint and pytest imports. Collection
    # verifies the import boundary without rerunning every generated example.
    env["PYTEST_ADDOPTS"] = "--collect-only"
    result = subprocess.run(
        [sys.executable, str(root / "tests" / script)], cwd=root, env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "collected" in result.stdout, result.stdout
