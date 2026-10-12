"""Readiness inspects curated snapshots without traversing unrelated models."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from modules.runtime_setup import hub_cache
from modules.runtime_setup.catalog import pinned_revision_for, resolve_model_groups
from modules.runtime_setup.receipts import ModelReceiptVerifier, write_download_receipt


@pytest.fixture
def pinned_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_cache.hf_constants, "HF_HUB_CACHE", str(tmp_path))
    model = resolve_model_groups(("tts-cpu-cuda",))[0]
    revision = pinned_revision_for(model)
    assert revision is not None
    snapshot = tmp_path / ("models--" + model.replace("/", "--")) / "snapshots" / revision
    snapshot.mkdir(parents=True)
    (snapshot / "model.safetensors").write_bytes(b"original model bytes")
    write_download_receipt(snapshot, model_id=model, revision=revision)
    return snapshot, model, revision


def _status(verifier=None):
    return hub_cache._check_model_group_assets(("tts-cpu-cuda",), receipt_verifier=verifier)


def test_pinned_readiness_does_not_scan_unrelated_cache(pinned_cache, monkeypatch, tmp_path):
    unrelated = tmp_path / "models--other--model"
    unrelated.mkdir()
    (unrelated / "corrupt-metadata").write_text("not a valid cached repository")

    def forbid_scan():
        pytest.fail("Readiness must not enumerate unrelated model repositories")

    monkeypatch.setattr(hub_cache, "scan_cache_dir", forbid_scan)
    assert _status() == {"tts-cpu-cuda": {"ready": True, "reason": None}}


@pytest.mark.parametrize("change", ["remove", "replace", "extra", "receipt"])
def test_pinned_readiness_revalidates_warm_snapshot(pinned_cache, change):
    snapshot, _model, _revision = pinned_cache
    verifier = ModelReceiptVerifier()
    assert _status(verifier)["tts-cpu-cuda"]["ready"]
    weights = snapshot / "model.safetensors"
    if change == "remove":
        weights.unlink()
    elif change == "replace":
        weights.write_bytes(b"tampered model bytes")
    elif change == "extra":
        (snapshot / "unexpected.json").write_text("{}")
    else:
        (snapshot / ".orpheus-download-complete.json").write_text("{}")
    assert _status(verifier) == {"tts-cpu-cuda": {"ready": False, "reason": "incomplete"}}


def test_pinned_readiness_does_not_accept_other_revision(pinned_cache):
    snapshot, _model, _revision = pinned_cache
    snapshot.rename(snapshot.with_name("f" * 40))
    assert _status() == {"tts-cpu-cuda": {"ready": False, "reason": "missing"}}


def test_pinned_readiness_checks_blob_links(pinned_cache):
    snapshot, model, revision = pinned_cache
    weights = snapshot / "model.safetensors"
    blob = snapshot.parent.parent / "blobs" / "blob"
    blob.parent.mkdir()
    weights.replace(blob)
    try:
        weights.symlink_to(blob)
    except OSError:
        pytest.skip("The current user cannot create file symlinks")
    write_download_receipt(snapshot, model_id=model, revision=revision)
    verifier = ModelReceiptVerifier()
    assert _status(verifier)["tts-cpu-cuda"]["ready"]
    blob.write_bytes(b"tampered model bytes")
    assert _status(verifier) == {"tts-cpu-cuda": {"ready": False, "reason": "incomplete"}}


def test_unpinned_catalog_keeps_revision_discovery(pinned_cache, monkeypatch):
    snapshot, model, revision = pinned_cache
    monkeypatch.setattr(hub_cache, "pinned_revision_for", lambda _model: None)
    monkeypatch.setattr(hub_cache, "scan_cache_dir", lambda: SimpleNamespace(repos=[
        SimpleNamespace(repo_id=model, repo_type="model", revisions=[
            SimpleNamespace(snapshot_path=Path(snapshot), commit_hash=revision),
        ]),
    ]))
    assert _status() == {"tts-cpu-cuda": {"ready": True, "reason": None}}
