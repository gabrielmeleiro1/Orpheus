"""Resolve installed catalog and custom snapshots without network access."""

from pathlib import Path

from modules.errors import TTSInstallationError
from modules.runtime_setup.catalog import pinned_revision_for
from modules.runtime_setup.receipts import ModelReceiptVerifier

_receipt_verifier = ModelReceiptVerifier()


def installed_model_path(model_name: str) -> str:
    """Resolve local assets, requiring pinned receipts for catalog models."""
    if Path(model_name).is_dir():
        return str(Path(model_name).resolve())

    revision = pinned_revision_for(model_name)

    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError

    try:
        if revision is None:
            snapshot = snapshot_download(repo_id=model_name, local_files_only=True)
        else:
            snapshot = snapshot_download(
                repo_id=model_name, revision=revision, local_files_only=True,
            )
    except LocalEntryNotFoundError as exc:
        if revision is None:
            raise TTSInstallationError(
                f"Model {model_name} is not installed. Download it with "
                f"python scripts/download_models.py --model {model_name}, "
                "or point the model setting at a local directory."
            ) from exc
        raise TTSInstallationError(
            f"Model {model_name} is not installed. Reinstall the model from Setup."
        ) from exc
    if revision is not None and not _receipt_verifier.verify(
        Path(snapshot), model_id=model_name, revision=revision,
    ):
        raise TTSInstallationError(
            f"Incomplete or damaged cache for {model_name}. Reinstall the model from Setup."
        )
    return str(Path(snapshot).resolve())


def installed_voice_path(model_name: str, voice: str, *, snapshot: str | None = None) -> str:
    """Resolve a single Kokoro voice within the verified model snapshot."""
    if voice.endswith(".safetensors"):
        voice_path = Path(voice)
    else:
        if snapshot is None:
            snapshot = installed_model_path(model_name)
        voice_path = Path(snapshot) / "voices" / f"{voice}.safetensors"
    if not voice_path.is_file():
        raise TTSInstallationError(
            f"Voice {voice} is missing from the installed snapshot for {model_name}. "
            "Reinstall the model from Setup."
        )
    return str(voice_path)


def require_local_phonemizer() -> None:
    """Prevent Misaki from installing its English spaCy model during inference."""
    from importlib import metadata

    try:
        metadata.distribution("en_core_web_sm")
    except metadata.PackageNotFoundError as exc:
        raise TTSInstallationError(
            "The English phonemizer model is missing. Reinstall the runtime from Setup."
        ) from exc
