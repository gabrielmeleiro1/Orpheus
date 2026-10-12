"""Repair copied Hub snapshots using its real file-cache publication path."""

import hashlib
import importlib

import pytest

from modules.runtime_setup import downloads
from modules.runtime_setup.catalog import pinned_revision_for
from modules.runtime_setup.receipts import DOWNLOAD_RECEIPT_FILENAME, validate_download_receipt


@pytest.fixture
def copied_hub_cache(monkeypatch, tmp_path):
    import httpx
    from huggingface_hub import constants, file_download

    snapshot_module = importlib.import_module("huggingface_hub._snapshot_download")
    files = {"config.json": b'{"model": "original"}', "weights.bin": b"healthy weights"}
    model = "hexgrad/Kokoro-82M"
    revision = pinned_revision_for(model)
    transfers = []

    # Keep Hub's actual snapshot lookup, forced download, blob publication and
    # copy fallback. Only repository metadata and the HTTP payload are supplied
    # locally, so a fake downloader cannot accidentally repair the pointer.
    monkeypatch.setattr(constants, "HF_HUB_DISABLE_SYMLINKS", True)
    monkeypatch.setattr(file_download, "are_symlinks_supported", lambda *_: False)
    monkeypatch.setattr(snapshot_module, "read_tree_cache", lambda *_: dict.fromkeys(files))

    def metadata(*, filename, **_):
        content = files[filename]
        return filename, hashlib.sha256(content).hexdigest(), revision, len(content), None, None

    def transfer(url, handle, **_):
        transfers.append(url)
        handle.write(files[url])

    def reject_http(*_, **__):
        pytest.fail("Repair regression attempted a network request")

    monkeypatch.setattr(file_download, "_get_metadata_or_catch_error", metadata)
    monkeypatch.setattr(file_download, "http_get", transfer)
    monkeypatch.setattr(httpx.Client, "send", reject_http)
    snapshot = downloads.download_model_snapshot(model, cache_dir=tmp_path)
    assert all(not (snapshot / name).is_symlink() for name in files)
    transfers.clear()
    return snapshot, model, revision, files, transfers


@pytest.mark.parametrize("receipt_damage", [None, "invalid", "wrong_revision"])
def test_repair_replaces_copied_snapshot_bytes(copied_hub_cache, tmp_path, receipt_damage):
    import json

    snapshot, model, revision, files, transfers = copied_hub_cache
    weights = snapshot / "weights.bin"
    weights.write_bytes(b"damaged weights")
    receipt = snapshot / DOWNLOAD_RECEIPT_FILENAME
    original_receipt = receipt.read_bytes()
    if receipt_damage == "invalid":
        receipt.write_text("{", encoding="utf-8")
    elif receipt_damage == "wrong_revision":
        payload = json.loads(original_receipt)
        payload["revision"] = "0" * 40
        receipt.write_text(json.dumps(payload), encoding="utf-8")

    result = downloads.download_model_snapshot(model, cache_dir=tmp_path)

    assert result == snapshot
    assert weights.read_bytes() == files["weights.bin"]
    assert validate_download_receipt(snapshot, model_id=model, revision=revision)
    if receipt_damage is None:
        assert transfers == ["weights.bin"]
        assert receipt.read_bytes() == original_receipt
    else:
        assert sorted(transfers) == sorted(files)


def test_failed_copied_snapshot_repair_keeps_receipt(copied_hub_cache, monkeypatch, tmp_path):
    from huggingface_hub import file_download

    snapshot, model, revision, _files, _transfers = copied_hub_cache
    (snapshot / "weights.bin").write_bytes(b"damaged weights")
    receipt = snapshot / DOWNLOAD_RECEIPT_FILENAME
    original_receipt = receipt.read_bytes()

    def fail_transfer(*_, **__):
        raise OSError("transfer interrupted")

    monkeypatch.setattr(file_download, "http_get", fail_transfer)
    with pytest.raises(OSError, match="transfer interrupted"):
        downloads.download_model_snapshot(model, cache_dir=tmp_path)

    assert receipt.read_bytes() == original_receipt
    assert not validate_download_receipt(snapshot, model_id=model, revision=revision)
