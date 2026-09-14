"""PDF text-layer preflight helpers.

The checks in this module deliberately do not perform OCR. They only inspect
the text and image metadata that PyMuPDF can read from the PDF itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
import re

PDFTextLayerClassification = Literal["native_text", "low_text_layer", "ocr_required", "unknown"]

DEFAULT_MAX_SAMPLE_PAGES = 10
NATIVE_TEXT_MIN_CHARS = 1000
NATIVE_TEXT_MIN_WORDS = 120
NATIVE_TEXT_MIN_NON_EMPTY_RATIO = 0.5
# A page with a real prose fragment should not be treated as an image-only
# scan merely because it also contains a raster illustration. Keep the OCR
# gate for genuinely scant text (roughly a label, caption, or a few residues).
LOW_TEXT_MAX_CHARS = 120
LOW_TEXT_MAX_WORDS = 20
SHORT_NATIVE_MIN_CHARS = 100
SHORT_NATIVE_MIN_WORDS = 20
SHORT_NATIVE_MIN_NON_EMPTY_RATIO = 0.8
SHORT_NATIVE_MAX_PAGES = 2

_WORD_RE = re.compile(r"\b\w+\b")
_MARKDOWN_RULE_RE = re.compile(r"(?:[-*_]\s*){3,}")


@dataclass(frozen=True)
class PDFTextLayerAnalysis:
    """Confidence summary for whether a PDF has an extractable text layer."""

    classification: PDFTextLayerClassification
    confidence: float
    total_pages: int
    sampled_pages: int
    non_empty_pages: int
    text_chars: int
    word_count: int
    image_pages: int = 0
    image_count: int = 0

    @property
    def requires_ocr(self) -> bool:
        return self.classification == "ocr_required"

    @property
    def warning_message(self) -> str | None:
        if self.classification == "ocr_required":
            return (
                "OCR required: this PDF appears to be scanned/image-only or has too little "
                "extractable text. It is not text-extractable without OCR, and this app "
                "does not run OCR yet."
            )
        if self.classification == "low_text_layer":
            return (
                "This PDF has a weak text layer; conversion may miss content unless the "
                "source is re-exported with embedded text."
            )
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "confidence": self.confidence,
            "requires_ocr": self.requires_ocr,
            "total_pages": self.total_pages,
            "sampled_pages": self.sampled_pages,
            "non_empty_pages": self.non_empty_pages,
            "text_chars": self.text_chars,
            "word_count": self.word_count,
            "image_pages": self.image_pages,
            "image_count": self.image_count,
        }


def _sample_page_indexes(total_pages: int, max_sample_pages: int) -> list[int]:
    if total_pages <= 0 or max_sample_pages <= 0:
        return []
    if total_pages <= max_sample_pages:
        return list(range(total_pages))
    if max_sample_pages == 1:
        return [0]
    step = (total_pages - 1) / (max_sample_pages - 1)
    return sorted({round(index * step) for index in range(max_sample_pages)})


def _normalized_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    meaningful_lines = []
    for line in value.splitlines():
        normalized_line = re.sub(r"\s+", " ", line).strip()
        if not normalized_line:
            continue
        if _MARKDOWN_RULE_RE.fullmatch(normalized_line):
            continue
        meaningful_lines.append(normalized_line)
    return re.sub(r"\s+", " ", " ".join(meaningful_lines)).strip()


def _count_words(text: str) -> int:
    return len(_WORD_RE.findall(text))


def _classify(
    *,
    total_pages: int,
    sampled_pages: int,
    non_empty_pages: int,
    text_chars: int,
    word_count: int,
    image_pages: int,
    image_count: int,
    image_aware: bool,
    min_page_chars: int = 0,
    min_page_words: int = 0,
) -> PDFTextLayerAnalysis:
    if sampled_pages <= 0:
        return PDFTextLayerAnalysis(
            classification="unknown",
            confidence=0.0,
            total_pages=total_pages,
            sampled_pages=sampled_pages,
            non_empty_pages=non_empty_pages,
            text_chars=text_chars,
            word_count=word_count,
            image_pages=image_pages,
            image_count=image_count,
        )

    non_empty_ratio = non_empty_pages / sampled_pages
    image_ratio = image_pages / sampled_pages

    native_text = (
        text_chars >= NATIVE_TEXT_MIN_CHARS
        and word_count >= NATIVE_TEXT_MIN_WORDS
        and non_empty_ratio >= NATIVE_TEXT_MIN_NON_EMPTY_RATIO
    )
    if native_text:
        confidence = min(
            0.99,
            0.75
            + min(0.15, text_chars / 20000)
            + min(0.10, non_empty_ratio * 0.10),
        )
        return PDFTextLayerAnalysis(
            classification="native_text",
            confidence=round(confidence, 2),
            total_pages=total_pages,
            sampled_pages=sampled_pages,
            non_empty_pages=non_empty_pages,
            text_chars=text_chars,
            word_count=word_count,
            image_pages=image_pages,
            image_count=image_count,
        )

    # Short, image-free documents (for example a one-page note) can have a
    # complete native text layer without meeting the long-document density
    # thresholds above. Require strong page coverage so this does not hide a
    # partially extracted multi-page document; image-aware scans remain in the
    # low/ OCR paths below.
    short_native_text = (
        text_chars >= SHORT_NATIVE_MIN_CHARS
        and word_count >= SHORT_NATIVE_MIN_WORDS
        and non_empty_ratio >= SHORT_NATIVE_MIN_NON_EMPTY_RATIO
        and total_pages <= SHORT_NATIVE_MAX_PAGES
        and sampled_pages == total_pages
        and image_aware
        and image_pages == 0
        and min_page_chars >= SHORT_NATIVE_MIN_CHARS
        and min_page_words >= SHORT_NATIVE_MIN_WORDS
    )
    if short_native_text:
        confidence = min(
            0.84,
            0.70
            + min(0.08, text_chars / 5000)
            + min(0.06, non_empty_ratio * 0.06),
        )
        return PDFTextLayerAnalysis(
            classification="native_text",
            confidence=round(confidence, 2),
            total_pages=total_pages,
            sampled_pages=sampled_pages,
            non_empty_pages=non_empty_pages,
            text_chars=text_chars,
            word_count=word_count,
            image_pages=image_pages,
            image_count=image_count,
        )

    no_text = text_chars == 0 or non_empty_pages == 0
    low_text = text_chars <= LOW_TEXT_MAX_CHARS and word_count <= LOW_TEXT_MAX_WORDS
    likely_scanned = image_aware and image_ratio >= 0.5
    if no_text or (low_text and likely_scanned):
        if image_aware and image_ratio >= 0.8:
            confidence = 0.98
        elif image_aware and image_ratio >= 0.5:
            confidence = 0.92
        elif no_text:
            confidence = 0.86
        else:
            confidence = 0.78
        return PDFTextLayerAnalysis(
            classification="ocr_required",
            confidence=round(confidence, 2),
            total_pages=total_pages,
            sampled_pages=sampled_pages,
            non_empty_pages=non_empty_pages,
            text_chars=text_chars,
            word_count=word_count,
            image_pages=image_pages,
            image_count=image_count,
        )

    confidence = 0.65 if low_text else 0.55
    if image_aware:
        confidence += min(0.20, image_ratio * 0.20)
    return PDFTextLayerAnalysis(
        classification="low_text_layer",
        confidence=round(min(0.85, confidence), 2),
        total_pages=total_pages,
        sampled_pages=sampled_pages,
        non_empty_pages=non_empty_pages,
        text_chars=text_chars,
        word_count=word_count,
        image_pages=image_pages,
        image_count=image_count,
    )


def analyze_pdf_text_layer(
    file_path: Path | str,
    *,
    max_sample_pages: int = DEFAULT_MAX_SAMPLE_PAGES,
) -> PDFTextLayerAnalysis:
    """Inspect sampled PDF pages for extractable text and image-only signals."""
    import fitz

    path = Path(file_path)
    with fitz.open(path) as document:
        total_pages = len(document)
        page_indexes = _sample_page_indexes(total_pages, max_sample_pages)
        text_chars = 0
        word_count = 0
        non_empty_pages = 0
        image_pages = 0
        image_count = 0
        page_char_counts: list[int] = []
        page_word_counts: list[int] = []

        for page_index in page_indexes:
            page = document.load_page(page_index)
            page_text = _normalized_text(page.get_text("text"))
            if page_text:
                non_empty_pages += 1
                text_chars += len(page_text)
                page_words = _count_words(page_text)
                word_count += page_words
                page_char_counts.append(len(page_text))
                page_word_counts.append(page_words)

            try:
                images = page.get_images(full=True)
            except Exception:
                images = []
            if images:
                image_pages += 1
                image_count += len(images)

    return _classify(
        total_pages=total_pages,
        sampled_pages=len(page_indexes),
        non_empty_pages=non_empty_pages,
        text_chars=text_chars,
        word_count=word_count,
        image_pages=image_pages,
        image_count=image_count,
        image_aware=True,
        min_page_chars=min(page_char_counts, default=0),
        min_page_words=min(page_word_counts, default=0),
    )


def analyze_extracted_pdf_text_layer(
    pages_data: list[dict[str, Any]],
    *,
    total_pages: int | None = None,
) -> PDFTextLayerAnalysis:
    """Classify text already extracted from a PDF parser backend."""
    text_chars = 0
    word_count = 0
    non_empty_pages = 0
    page_char_counts: list[int] = []
    page_word_counts: list[int] = []

    for page in pages_data:
        page_text = _normalized_text(page.get("text", "") if isinstance(page, dict) else "")
        if not page_text:
            continue
        non_empty_pages += 1
        text_chars += len(page_text)
        page_words = _count_words(page_text)
        word_count += page_words
        page_char_counts.append(len(page_text))
        page_word_counts.append(page_words)

    sampled_pages = len(pages_data)
    return _classify(
        total_pages=total_pages if total_pages is not None else sampled_pages,
        sampled_pages=sampled_pages,
        non_empty_pages=non_empty_pages,
        text_chars=text_chars,
        word_count=word_count,
        image_pages=0,
        image_count=0,
        image_aware=False,
        min_page_chars=min(page_char_counts, default=0),
        min_page_words=min(page_word_counts, default=0),
    )
