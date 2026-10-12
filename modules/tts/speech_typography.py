"""Recover narrative casing for speech without changing the stored source."""

from __future__ import annotations

from collections import Counter, defaultdict
from functools import lru_cache
import re

_WORD = re.compile(r"(?<![\w])[A-Za-z]+(?:['’][A-Za-z]+)?(?![\w])")
_SEPARATOR = re.compile(r"[ \t.,:;!?—–-]*")
# These have ordinary-word lookalikes (US/us, RAM/ram, etc.) or are common
# technical initialisms. Never infer their pronunciation from prose casing.
_ACRONYMS = frozenset({
    "AI", "API", "ASCII", "AT&T", "BBC", "CIA", "CPU", "CSS", "DNA", "DOI",
    "EU", "FBI", "GPU", "HTML", "HTTP", "HTTPS", "ID", "ISBN", "IT", "JSON",
    "NASA", "NATO", "NHS", "PDF", "RAM", "RNA", "ROM", "SQL", "TTS", "TV",
    "UK", "UN", "UNESCO", "UNICEF", "URL", "US", "USA", "USB", "UTF", "XML",
})


def _protected(word: str) -> bool:
    stem = re.sub(r"['’]S$", "", word)
    return stem in _ACRONYMS or bool(re.fullmatch(r"[IVXLCDM]+", stem))


def _identifier_word(text: str, match: re.Match[str]) -> bool:
    """Do not recase pieces of AT&T, part IDs, paths, or dotted identifiers."""
    before = text[max(0, match.start() - 2):match.start()]
    after = text[match.end():match.end() + 2]
    if (before and before[-1] in "&/@_=+\\-") or (after and after[0] in "&/@_=+\\-"):
        return True
    return bool(re.search(r"\w[.:]$", before) or re.match(r"[.:]\w", after))


def narrative_casing(source: str) -> dict[str, str]:
    """Learn spellings from this chapter, not from an invented name dictionary."""
    forms: dict[str, Counter[str]] = defaultdict(Counter)
    for match in _WORD.finditer(source):
        word = match.group()
        if not word.isupper() and not _identifier_word(source, match):
            forms[word.upper()][word] += 1
    hints = {key: counts.most_common(1)[0][0] for key, counts in forms.items()}

    # A final, isolated signature can introduce a first name only once. Require
    # a surname already established in the narrative; short/known initialisms
    # and identifiers remain untouched. This covers "HASTIE LANYON." safely
    # without title-casing arbitrary capitalized prose or single-word labels.
    signature = re.search(
        r"(?:^|\n|[.!?][ \t]+)([A-Z]{5,})[ \t]+([A-Z]{3,})[.]?\s*$", source,
    )
    if signature:
        first, surname = signature.groups()
        known = hints.get(surname, "")
        if known.istitle() and not _protected(first) and not _protected(surname):
            hints.setdefault(first, first.title())
    return {key: value for key, value in hints.items() if not _protected(key)}


@lru_cache(maxsize=4)
def _chapter_casing_hints(source: str) -> dict[str, str]:
    # Every chunk of a chapter is normalized against the same chapter source;
    # rescanning it per chunk made speech preparation quadratic in chapter size.
    # str caches its hash, so a repeat lookup does not rescan the source.
    return narrative_casing(source)


def normalize_speech_typography(
    text: str, source: str, *, heading: bool = False,
) -> str:
    """Normalize evidenced all-caps typography only in the synthesized copy.

    Body words must belong to an all-caps run of at least two words. An isolated
    acronym/emphasis in ordinary prose therefore retains its original casing.
    Headings may be single words, but still require narrative evidence.
    """
    hints = _chapter_casing_hints(source)
    if not hints:
        return text
    matches = list(_WORD.finditer(text))
    changes: dict[int, str] = {}
    run: list[re.Match[str]] = []

    def finish() -> None:
        if heading or len(run) >= 2:
            for match in run:
                word = match.group()
                replacement = hints.get(word)
                if replacement is None and re.search(r"['’]S$", word):
                    if stem := hints.get(word[:-2]):
                        replacement = stem + word[-2] + "s"
                if replacement:
                    changes[match.start()] = replacement
        run.clear()

    for match in matches:
        if not match.group().isupper() or len(match.group()) < 2 or _identifier_word(text, match):
            finish()
            continue
        if run and not _SEPARATOR.fullmatch(text[run[-1].end():match.start()]):
            finish()
        run.append(match)
    finish()
    return _WORD.sub(lambda match: changes.get(match.start(), match.group()), text)
