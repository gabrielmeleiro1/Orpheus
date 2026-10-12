"""Sanitize bounded runtime setup diagnostics."""

from __future__ import annotations

from pathlib import Path
import re


DIAGNOSTIC_MAX_CHARS = 2000


def _sanitize_diagnostic(value: str, *, max_chars: int = DIAGNOSTIC_MAX_CHARS) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value))
    text = re.sub(r"(?i)\b(sk-[a-z0-9_-]{8,})\b", "[redacted]", text)
    text = re.sub(
        r"(?i)\b(api[_-]?key|token|authorization|password)\s*[:=]\s*[^\s,;]+",
        lambda match: f"{match.group(1)}=[redacted]",
        text,
    )
    text = re.sub(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@", r"\1[redacted]@", text)
    home = str(Path.home())
    if home:
        text = text.replace(home, "<home>")
    text = re.sub(r"(?<![A-Za-z0-9:])/(?:[^/\s]+/)*[^/\s]*", "<path>", text)
    text = re.sub(r"(?i)\b[A-Z]:\\(?:[^\\\s]+\\)*[^\\\s]*", "<path>", text)
    text = text.strip()
    if len(text) > max_chars:
        text = "…" + text[-max_chars:]
    return text
