"""Canonical post-extraction artifact detectors shared by cleaner and benchmark scans."""

from __future__ import annotations

from dataclasses import dataclass
import re

PDF_HYPHENATION_LEAK_RE = re.compile(
    r"\b[A-Za-z]{2,}-\s+(?!(?:a|an|and|nor|or|the|to)\b)[a-z]{2,}\b"
)
PDF_INTERNAL_HYPHEN_FRAGMENT_RE = re.compile(
    r"(?i)\b(?:becom-ing|(?!(?:"
    r"anti|auto|card|co|cross|de|half|high|inter|low|micro|mid|multi|non|"
    r"over|post|pre|proto|quasi|re|self|semi|short|under|well"
    r")-)"
    r"[a-z]{2,6}-(?:"
    r"[a-z]{2,}(?:ence|ences|ance|ances|ment|ments|tion|tions|sion|sions)|"
    r"ence|nment|rience|sation|rounding|pendence"
    r"))\b"
)
PDF_PAGE_FOOTNOTE_LEAK_RE = re.compile(
    r"(?i)\b(?:"
    r"\d{1,4}[il]\s+[a-z]{3,}|"
    r"[a-z]{2,}\s+I\s+The problem\b|"
    r"This content downloaded from\b|"
    r"All use subject to\b|"
    r"Stable URL:|"
    r"JSTOR is a not-for-profit"
    r")"
)
PDF_JOINED_DASH_LEAK_RE = re.compile(r"(?i)\bthemselvestoward\b")
JOINED_FIRST_PERSON_LEAK_RE = re.compile(
    r"\bI(?:"
    r"am|always|began|begin|believe|can|could|feel|felt|found|had|have|"
    r"hear|heard|know|lay|looked|may|might|must|said|saw|see|shall|should|"
    r"suppose|think|want|wanted|was|went|were|will|wish|would"
    r")\b"
)
PROMPT_DELIMITER_RE = re.compile(
    r"(?im)(?:^\s*-{3,}\s*$|\.---|prompt delimiter|<\|/?(?:system|user|assistant)\|>)"
)
MODEL_COMMENTARY_RE = re.compile(
    r"(?i)\b("
    r"as an ai|"
    r"cleaned text:|"
    r"here is the cleaned|"
    r"i (?:have )?corrected\s+(?:the\s+)?(?:text|formatting|artifacts?|markdown|line breaks?)|"
    r"i removed\s+(?:the\s+)?(?:text|formatting|artifacts?|markdown|headers?|footers?|citations?|references?|line breaks?)|"
    r"note:|"
    r"the text has been"
    r")\b"
)
EPUB_SOCIAL_BOILERPLATE_RE = re.compile(
    r"(?i)\b(?:"
    r"follow\s+(?:the\s+)?penguin|"
    r"penguin(?:books|classics)|"
    r"youtube\.com/penguin|"
    r"facebook\.com/penguin|"
    r"soundcloud\.com/penguin|"
    r"@penguin(?:uk)?books"
    r")\b"
)
GARBLED_REPEATED_TOKEN_RE = re.compile(
    r"(?i)\b(?:"
    r"Firstop{3,}mry|"
    r"lltllll|"
    r"iVttLiiii|"
    r"Peeeeeeee|"
    r"bhedmeeeeta|"
    r"evidelllly|"
    r"i{10}"
    r")\b"
)


@dataclass(frozen=True)
class ExtractionArtifactIssue:
    issue_type: str
    excerpt: str


def has_pdf_hyphenation_leakage(text: str) -> bool:
    return (
        PDF_HYPHENATION_LEAK_RE.search(text) is not None
        or PDF_INTERNAL_HYPHEN_FRAGMENT_RE.search(text) is not None
    )


def has_pdf_page_footnote_leakage(text: str) -> bool:
    return PDF_PAGE_FOOTNOTE_LEAK_RE.search(text) is not None


def has_pdf_joined_dash_leakage(text: str) -> bool:
    return PDF_JOINED_DASH_LEAK_RE.search(text) is not None


def has_joined_first_person_leakage(text: str) -> bool:
    return JOINED_FIRST_PERSON_LEAK_RE.search(text) is not None


def scan_cleaner_handoff_artifacts(text: str) -> list[ExtractionArtifactIssue]:
    """Return leakage issues detected in cleaned or TTS handoff text."""
    issues: list[ExtractionArtifactIssue] = []
    checks: tuple[tuple[str, re.Pattern[str]], ...] = (
        ("prompt_delimiter_leakage", PROMPT_DELIMITER_RE),
        ("model_commentary_leakage", MODEL_COMMENTARY_RE),
        ("pdf_hyphenation_leakage", PDF_HYPHENATION_LEAK_RE),
        ("pdf_internal_hyphen_fragment", PDF_INTERNAL_HYPHEN_FRAGMENT_RE),
        ("pdf_page_footnote_leakage", PDF_PAGE_FOOTNOTE_LEAK_RE),
        ("pdf_joined_dash_leakage", PDF_JOINED_DASH_LEAK_RE),
        ("epub_social_boilerplate", EPUB_SOCIAL_BOILERPLATE_RE),
        ("joined_word_leakage", JOINED_FIRST_PERSON_LEAK_RE),
        ("garbled_repeated_token_leakage", GARBLED_REPEATED_TOKEN_RE),
    )
    for issue_type, pattern in checks:
        match = pattern.search(text)
        if match:
            issues.append(
                ExtractionArtifactIssue(
                    issue_type=issue_type,
                    excerpt=match.group(0)[:160],
                )
            )
    return issues
