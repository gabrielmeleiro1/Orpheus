"""Resolve the exact snapshots installed by runtime setup without network access."""

from pathlib import Path

from scripts.download_models import ModelReceiptVerifier, pinned_revision_for

_receipt_verifier = ModelReceiptVerifier()


def installed_model_path(model_name: str) -> str:
    """Prefer the curated pinned cache; preserve custom repository/path behavior."""
    if Path(model_name).is_dir():
        return model_name

    revision = pinned_revision_for(model_name)
    if revision is None:
        return model_name

    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError

    try:
        snapshot = snapshot_download(
            repo_id=model_name, revision=revision, local_files_only=True,
        )
    except LocalEntryNotFoundError:
        return model_name
    if not _receipt_verifier.verify(Path(snapshot), model_id=model_name, revision=revision):
        raise RuntimeError(
            f"Incomplete or damaged cache for {model_name}. Repair it with "
            f"python scripts/download_models.py --model {model_name}."
        )
    return snapshot
