"""Regression coverage for hf cache."""

from unittest.mock import Mock

from modules import hf_cache


def test_configure_updates_already_imported_hub(monkeypatch, tmp_path):
    from huggingface_hub import constants, file_download

    monkeypatch.setattr(hf_cache, "_configured", False)
    monkeypatch.setattr(hf_cache.sys, "platform", "win32")
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    monkeypatch.delenv("HF_HUB_DISABLE_SYMLINKS", raising=False)
    monkeypatch.delenv("HF_HUB_DISABLE_SYMLINKS_WARNING", raising=False)
    monkeypatch.setattr(constants, "HF_HUB_DISABLE_SYMLINKS", False)
    monkeypatch.setattr(constants, "HF_HUB_DISABLE_SYMLINKS_WARNING", False)
    monkeypatch.setattr(hf_cache.os, "symlink", Mock(side_effect=OSError(1314, "denied")))
    hf_cache.configure_hf_cache()
    assert constants.HF_HUB_DISABLE_SYMLINKS is True
    assert file_download.are_symlinks_supported(tmp_path) is False
