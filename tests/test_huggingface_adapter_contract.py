"""Exercise Hub cache scanning and receipts against installed Hub APIs."""

import hashlib
import importlib
import inspect
import json
import os
from pathlib import Path

import pytest

from tests._factories_group2_hub import isolate_hub_credentials, isolate_hub_environment

pytestmark = pytest.mark.integration


@pytest.fixture
def cache(tmp_path, monkeypatch):
    home = tmp_path / "hf-home"
    root = home / "hub"
    isolate_hub_environment(monkeypatch, home)
    from modules.hf_cache import configure_hf_cache

    configure_hf_cache()
    hub = pytest.importorskip("huggingface_hub")
    from huggingface_hub import constants

    isolate_hub_credentials(monkeypatch, home)

    for name, value in (("HF_HOME", str(home)), ("HF_HUB_CACHE", str(root)),
                        ("HUGGINGFACE_HUB_CACHE", str(root)), ("HF_HUB_OFFLINE", True)):
        monkeypatch.setattr(constants, name, value)
    from huggingface_hub.utils import _cache_manager

    # Cache scanning keeps its own import-time copy of the Hub cache root.
    monkeypatch.setattr(_cache_manager, "HF_HUB_CACHE", str(root))
    for effective_root in (
        constants.HF_HOME, constants.HF_HUB_CACHE, constants.HUGGINGFACE_HUB_CACHE,
        constants.HF_TOKEN_PATH, constants.HF_STORED_TOKENS_PATH,
        _cache_manager.HF_HUB_CACHE,
    ):
        assert Path(effective_root).resolve().is_relative_to(tmp_path.resolve()), (
            f"Hub cache escaped the temporary directory: {effective_root}"
        )
    from scripts.download_models import pinned_revision_for

    model_id = "hexgrad/Kokoro-82M"
    revision = pinned_revision_for(model_id)
    repo = root / ("models--" + model_id.replace("/", "--"))
    snapshot = repo / "snapshots" / revision
    snapshot.mkdir(parents=True)
    (repo / "blobs").mkdir()
    (repo / "refs").mkdir()
    (repo / "refs" / "main").write_text(revision)
    payload = b'{"model_type":"kokoro"}\n'
    blob = repo / "blobs" / hashlib.sha256(payload).hexdigest()
    blob.write_bytes(payload)
    (snapshot / "config.json").symlink_to(Path("../../blobs") / blob.name)
    yield hub, root, model_id, revision, snapshot, blob


def test_cache_scan_and_pinned_offline_snapshot_agree(cache):
    hub, root, model_id, revision, snapshot, blob = cache
    from huggingface_hub.errors import LocalEntryNotFoundError

    info = hub.scan_cache_dir(root)
    assert not info.warnings, f"huggingface_hub {hub.__version__}: {info.warnings}"
    repo, = info.repos
    assert repo.repo_id == model_id, f"huggingface_hub {hub.__version__}"
    cached_revision, = repo.revisions
    assert cached_revision.commit_hash == revision
    cached_file, = cached_revision.files
    assert cached_file.blob_path == blob
    assert cached_file.size_on_disk == blob.stat().st_size
    assert Path(hub.snapshot_download(
        repo_id=model_id, revision=revision, cache_dir=root, local_files_only=True,
    )) == snapshot
    assert Path(hub.snapshot_download(
        repo_id=model_id, revision="main", cache_dir=root, local_files_only=True,
    )) == snapshot
    with pytest.raises(LocalEntryNotFoundError):
        hub.snapshot_download(
            repo_id=model_id, revision="0" * 40, cache_dir=root, local_files_only=True,
        )


def test_download_receipt_round_trips_and_rejects_changed_blob(cache):
    hub, root, model_id, revision, snapshot, blob = cache
    from scripts.download_models import (
        ModelReceiptVerifier,
        validate_download_receipt,
        write_download_receipt,
    )
    from modules.tts.model_snapshots import installed_model_path

    receipt = write_download_receipt(snapshot, model_id=model_id, revision=revision)
    payload = json.loads(receipt.read_text())
    assert payload["revision"] == revision, f"huggingface_hub {hub.__version__}"
    assert payload["files"] == [{
        "path": "config.json", "size": blob.stat().st_size,
        "sha256": hashlib.sha256(blob.read_bytes()).hexdigest(),
    }]
    verifier = ModelReceiptVerifier()
    assert validate_download_receipt(snapshot, model_id=model_id, revision=revision)
    assert verifier.verify(snapshot, model_id=model_id, revision=revision)
    assert Path(installed_model_path(model_id)) == snapshot
    assert not validate_download_receipt(snapshot, model_id=model_id, revision="0" * 40)
    blob.write_bytes(b"changed payload")
    assert not validate_download_receipt(snapshot, model_id=model_id, revision=revision)
    assert not verifier.verify(snapshot, model_id=model_id, revision=revision)


def test_download_reuses_verified_pinned_snapshot_when_hub_is_offline(cache):
    hub, root, model_id, revision, snapshot, _ = cache
    from scripts.download_models import download_model_snapshot, write_download_receipt

    write_download_receipt(snapshot, model_id=model_id, revision=revision)
    assert download_model_snapshot(model_id, cache_dir=root) == snapshot, (
        f"huggingface_hub {hub.__version__}"
    )


def test_shared_blob_helpers_accept_cleanup_arguments_or_preserve_payloads(cache):
    hub, root, _, _, _, blob = cache
    from modules.runtime_setup import hub_cache

    store = root / "blobs"
    store.mkdir()
    payload = store / "unowned-payload"
    payload.write_bytes(b"retain me")
    try:
        shared = importlib.import_module("huggingface_hub.utils._shared_blobs")
        is_shared = getattr(shared, "is_shared_blobs_dir")
        sweep = getattr(shared, "sweep_shared_blob")
    except (ImportError, AttributeError):
        assert hub_cache._owned_shared_store(root) is None, f"huggingface_hub {hub.__version__}"
    else:
        try:
            inspect.signature(is_shared).bind(store)
            inspect.signature(sweep).bind(payload, cache_dir=root)
        except TypeError as exc:
            pytest.fail(f"huggingface_hub {hub.__version__}: {exc}")
        assert is_shared(store) is False
        assert hub_cache._owned_shared_store(root) is None
    assert hub_cache._release_shared_store_files({payload}, root) == 0
    assert payload.read_bytes() == b"retain me"
    assert blob.is_file()


def test_fixture_isolates_previously_imported_credentials(tmp_path, monkeypatch, request):
    pytest.importorskip("huggingface_hub")
    from huggingface_hub import constants

    for name in ("HF_TOKEN_PATH", "HF_STORED_TOKENS_PATH"):
        previous = str(tmp_path / "previous-home" / name.lower())
        monkeypatch.setenv(name, previous)
        monkeypatch.setattr(constants, name, previous)
    for name in (
        "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HF_OIDC_RESOURCE", "HF_OIDC_ID_TOKEN",
        "OAUTH_CLIENT_ID", "OAUTH_CLIENT_SECRET", "OAUTH_SCOPES",
    ):
        monkeypatch.setenv(name, "unused-test-credential")
    request.getfixturevalue("cache")

    home = tmp_path / "hf-home"
    for name in ("HF_TOKEN_PATH", "HF_STORED_TOKENS_PATH"):
        assert Path(getattr(constants, name)).is_relative_to(home)
        assert Path(os.environ[name]).is_relative_to(home)
    for name in (
        "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HF_OIDC_RESOURCE", "HF_OIDC_ID_TOKEN",
        "OAUTH_CLIENT_ID", "OAUTH_CLIENT_SECRET", "OAUTH_SCOPES",
    ):
        assert name not in os.environ
    from huggingface_hub.utils import build_hf_headers, get_token, get_stored_tokens

    assert get_token() is None
    assert get_stored_tokens() == {}
    assert "authorization" not in build_hf_headers()
