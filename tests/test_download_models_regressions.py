"""Regression coverage for download models."""

from unittest.mock import Mock

import pytest

from modules.runtime_setup import downloads
from scripts import download_models


@pytest.mark.parametrize("repair", ["success", "failure", "unchanged"])
@pytest.mark.parametrize("destination", ["cache_dir", "local_dir"])
def test_corrupt_download_retry_preserves_trusted_receipt(monkeypatch, tmp_path, repair, destination):
    import huggingface_hub

    model = "hexgrad/Kokoro-82M"
    revision = download_models.MODEL_REVISIONS[model]
    weights = tmp_path / "weights.bin"
    weights.write_bytes(b"healthy")
    receipt = download_models.write_download_receipt(tmp_path, model_id=model, revision=revision)
    trusted = receipt.read_bytes()
    weights.write_bytes(b"damaged")
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda **kwargs: str(tmp_path))
    calls = []

    def download(**kwargs):
        calls.append(kwargs)
        assert receipt.read_bytes() == trusted
        if repair == "failure":
            raise OSError("offline")
        if repair == "success":
            weights.write_bytes(b"healthy")
        return str(weights)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", download)
    if repair == "success":
        download_models.download_model_snapshot(model, **{destination: tmp_path})
        assert weights.read_bytes() == b"healthy"
        assert download_models.validate_download_receipt(tmp_path, model_id=model, revision=revision)
    else:
        with pytest.raises((OSError, RuntimeError)):
            download_models.download_model_snapshot(model, **{destination: tmp_path})
        assert receipt.read_bytes() == trusted
        assert not download_models.validate_download_receipt(tmp_path, model_id=model, revision=revision)
    assert len(calls) == 1
    assert calls[0]["filename"] == "weights.bin"
    assert calls[0]["force_download"] is True
    assert calls[0]["revision"] == revision
    assert calls[0][destination] == str(tmp_path)


def test_healthy_download_retry_keeps_receipt(monkeypatch, tmp_path):
    import huggingface_hub

    model = "hexgrad/Kokoro-82M"
    revision = download_models.MODEL_REVISIONS[model]
    (tmp_path / "weights.bin").write_bytes(b"healthy")
    download_models.write_download_receipt(tmp_path, model_id=model, revision=revision)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda **kwargs: str(tmp_path))
    transfer = Mock(side_effect=AssertionError("Healthy cache must not be downloaded again"))
    writer = Mock(side_effect=AssertionError("Trusted receipt must not be rewritten"))
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", transfer)
    monkeypatch.setattr(downloads, "write_download_receipt", writer)
    assert download_models.download_model_snapshot(model) == tmp_path
    transfer.assert_not_called()
    writer.assert_not_called()
