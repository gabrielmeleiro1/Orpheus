"""Regression coverage for model snapshots."""

import pytest

from modules.tts.model_snapshots import installed_model_path


def test_incomplete_pinned_snapshot_reports_repair(monkeypatch, tmp_path):
    import huggingface_hub

    (tmp_path / "config.json").write_text("{}")
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda **kwargs: str(tmp_path))
    with pytest.raises(RuntimeError, match="hexgrad/Kokoro-82M.*Reinstall.*Setup"):
        installed_model_path("hexgrad/Kokoro-82M")
