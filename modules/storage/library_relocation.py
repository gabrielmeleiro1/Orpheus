"""Keep reversible deletion inventories usable after moving managed storage."""

from __future__ import annotations

import logging
from pathlib import Path

from modules.storage.deletion import (
    DELETE_MANIFEST,
    LOCAL_DELETE_STAGING,
    read_delete_transaction,
    write_delete_transaction,
)
from modules.storage.migrations import remap_absolute_path

logger = logging.getLogger(__name__)


def rebase_delete_transactions(
    temp_root: Path, current_roots: tuple[Path, ...], mappings: list[tuple[Path, Path]],
) -> None:
    """Publish moved inventories before their ownership markers are replaced."""
    root = temp_root / "delete_staging"
    if not root.is_dir() or root.is_symlink() or root.is_junction():
        return
    allowed_roots = (*current_roots, *(old for old, _new in mappings))
    for entry in root.iterdir():
        manifest = entry / DELETE_MANIFEST
        if (entry.is_symlink() or entry.is_junction() or manifest.is_symlink() or manifest.is_junction()
                or not manifest.exists()):
            continue
        try:
            book_id, artifacts = read_delete_transaction(entry, allowed_roots=allowed_roots)
            rebased = [
                (Path(remap_absolute_path(str(original), mappings)), Path(remap_absolute_path(str(staged), mappings)))
                for original, staged in artifacts
            ]
            if rebased == artifacts:
                continue
            for original, staged in rebased:
                if (
                    not any(original.resolve().is_relative_to(path.resolve()) for path in current_roots)
                    or staged.parent != original.parent / LOCAL_DELETE_STAGING / entry.name
                    or staged.is_symlink()
                    or any(path.is_symlink() or path.is_junction() for path in (staged.parent, staged.parent.parent))
                ):
                    raise ValueError("Invalid moved deletion artifact path")
        except (ValueError, TypeError, KeyError):
            # The deletion sweep preserves inventories it cannot validate.
            logger.warning("Could not rebase deletion transaction: %s", entry, exc_info=True)
            continue
        # Keep the previous inventory recoverable if publication is interrupted.
        write_delete_transaction(entry, book_id, rebased)
