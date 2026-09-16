"""Installed pinned snapshots must work without a cached main branch reference."""

from modules.tts.model_snapshots import installed_model_path
from scripts.download_models import MODEL_REVISIONS


def test_resolves_curated_snapshot_at_installed_revision(monkeypatch, tmp_path):
    import huggingface_hub

    calls = []

    def snapshot_download(**kwargs):
        calls.append(kwargs)
        return str(tmp_path)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot_download)
    for model in ("hexgrad/Kokoro-82M", "Qwen/Qwen2.5-1.5B-Instruct"):
        assert installed_model_path(model) == str(tmp_path)
        assert calls[-1] == {
            "repo_id": model, "revision": MODEL_REVISIONS[model], "local_files_only": True,
        }


def test_missing_snapshot_preserves_repository_loading(monkeypatch):
    import huggingface_hub
    from huggingface_hub.errors import LocalEntryNotFoundError

    def missing(**kwargs):
        raise LocalEntryNotFoundError("missing")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", missing)
    assert installed_model_path("hexgrad/Kokoro-82M") == "hexgrad/Kokoro-82M"


def test_custom_repository_and_local_path_are_preserved(tmp_path):
    assert installed_model_path("custom/model") == "custom/model"
    assert installed_model_path(str(tmp_path)) == str(tmp_path)
