"""Canonical post-extraction artifact detectors shared by cleaner and benchmark scans."""

from __future__ import annotations

import re

PDF_HYPHENATION_LEAK_RE = re.compile(
    r"\b[A-Za-z]{2,}-\s+(?!(?:a|an|and|nor|or|the|to)\b)[a-z]{2,}\b"
)
# The post-hyphen fragment must start lowercase: line-break hyphenation
# artifacts rejoin lowercase word halves ("experi-ence"), while legitimate
# hyphenated compounds capitalize the second word ("Time-Dimension").
PDF_INTERNAL_HYPHEN_FRAGMENT_RE = re.compile(
    r"\b(?:[Bb]ecom-ing|(?!(?i:"
    r"anti|auto|card|co|cross|de|far|fore|full|half|high|ill|inter|long|low|"
    r"micro|mid|multi|non|off|out|over|post|pre|proto|quasi|re|self|semi|"
    r"short|under|well"
    r")-)"
    r"[A-Za-z][a-z]{1,5}-(?:"
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
