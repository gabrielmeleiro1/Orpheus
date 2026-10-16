"""Portable byte limits for generated source filenames."""

from pathlib import Path


def source_filename(filename: str, *, prefix: str = "") -> str:
    """Fit a source basename and prefix in 255 UTF-8 bytes, keeping its extension."""
    path = Path(filename)
    suffix = path.suffix
    stem_budget = 255 - len((prefix + suffix).encode("utf-8"))
    if stem_budget < 1:
        raise ValueError("Source filename prefix and extension are too long")
    stem = path.stem.encode("utf-8")[:stem_budget].decode("utf-8", errors="ignore")
    return f"{prefix}{stem}{suffix}"
