"""Plain-language diagnosis of sources that cannot be converted at all.

Parsers fail on these with library messages ("document closed or encrypted",
"Failed to open file … as type pdf"). The checks here name the actual
problem for the user and keep the library's wording as detail.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SourceProblem:
    """A stable ``code`` and plain ``message``, with the library wording as ``details``."""

    code: str
    message: str
    details: str | None = None


EMPTY_FILE = "The file is empty."
PASSWORD_PROTECTED_PDF = (
    "This PDF is password-protected. Open it in a PDF viewer, save a copy without the "
    "password, and add that copy."
)
DAMAGED_PDF = "This PDF is damaged or is not a PDF, so it cannot be opened."


def pdf_problem(path: Path, *, require_pages: bool = True) -> SourceProblem | None:
    """Return why a PDF cannot be opened for reading, or None when it can.

    ``require_pages`` also reports a PDF with no pages as damaged; the parser
    leaves that case to its own "no readable chapters" error.
    """
    import pymupdf

    try:
        document = pymupdf.open(path, filetype="pdf")
    except Exception as exc:  # PyMuPDF raises several unrelated types for bad files.
        # Keep the library wording but not the folders of the local path it quotes.
        details = re.sub(r"'[^']*[\\/]([^'\\/]+)'", r"'\1'", str(exc))
        return SourceProblem("pdf_damaged", DAMAGED_PDF, details)
    try:
        if document.needs_pass:
            return SourceProblem(
                "pdf_password_protected",
                PASSWORD_PROTECTED_PDF,
                "document is encrypted with a user password",
            )
        if require_pages and document.page_count < 1:
            return SourceProblem("pdf_damaged", DAMAGED_PDF, "document has no pages")
    finally:
        document.close()
    return None


def file_problem(path: Path | str) -> SourceProblem | None:
    """Return a problem visible without opening the file (missing access, no bytes)."""
    try:
        size = Path(path).stat().st_size
    except OSError as exc:
        return SourceProblem("source_unreadable", "The file cannot be read.", str(exc))
    if size == 0:
        return SourceProblem("source_empty", EMPTY_FILE)
    return None


def source_problem(path: Path | str, *, require_pages: bool = True) -> SourceProblem | None:
    """Return a blocking problem for ``path`` that parsing would only report vaguely."""
    path = Path(path)
    problem = file_problem(path)
    if problem is None and path.suffix.lower() == ".pdf":
        return pdf_problem(path, require_pages=require_pages)
    return problem
