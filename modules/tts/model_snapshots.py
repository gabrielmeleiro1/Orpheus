"""Resolve the exact snapshots installed by runtime setup without network access."""

from pathlib import Path


def installed_model_path(model_name: str) -> str:
    """Prefer the curated pinned cache; preserve custom repository/path behavior."""
    if Path(model_name).is_dir():
        return model_name

    from scripts.download_models import pinned_revision_for

    revision = pinned_revision_for(model_name)
    if revision is None:
        return model_name

    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError

    try:
        return snapshot_download(
            repo_id=model_name, revision=revision, local_files_only=True,
        )
    except LocalEntryNotFoundError:
        return model_name
