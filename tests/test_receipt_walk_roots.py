"""Receipt walks reuse only a stable snapshot anchor, never file identities."""

from collections import Counter
import hashlib
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import pytest

from modules.runtime_setup import receipts


MODEL_ID = "test/model"
REVISION = "a" * 40
FILES = {
    "config.json": b"{}",
    "LICENSE": b"license",
    "README.md": b"model",
    "weights.bin": b"12345678",
    "voices/Jane, Doe.pt": b"voice-a",
    "voices/声.pt": b"voice-b",
}


@pytest.fixture(autouse=True)
def mature_metadata(monkeypatch):
    # Exercise mature Windows metadata without sleeping or changing real clocks.
    monkeypatch.setattr(receipts, "_now_ns", lambda: time.time_ns() + 4_000_000_000)


@pytest.fixture
def snapshot(tmp_path):
    root = tmp_path / "hub" / "models--test--model" / "snapshots" / REVISION
    root.mkdir(parents=True)
    for relative, payload in FILES.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    for relative in (".cache/metadata.json", ".locks/request.lock", "pending.incomplete"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ignored")
    receipts.write_download_receipt(root, model_id=MODEL_ID, revision=REVISION)
    return root.resolve()


def _walk(root, kind):
    if kind == "manifest":
        return lambda: receipts._snapshot_manifest(root)
    parsed = receipts._parse_download_receipt(root, model_id=MODEL_ID, revision=REVISION)
    assert parsed is not None
    return lambda: receipts._snapshot_metadata_fingerprint(parsed)


def _record_resolutions(monkeypatch):
    original = Path.resolve
    calls = Counter()

    def resolve(path, strict=False):
        calls[path, strict] += 1
        return original(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    return calls


def _verify(verifier, root):
    return verifier.verify(root, model_id=MODEL_ID, revision=REVISION)


@pytest.mark.parametrize("kind", ["manifest", "fingerprint"])
def test_walk_matches_fresh_root_resolution_exactly(snapshot, monkeypatch, kind):
    walk = _walk(snapshot, kind)
    original_target = receipts._resolved_allowed_file_target

    def resolve_root_for_each_file(root, path, **_kwargs):
        # The existing two-argument contract is the reference resolution policy.
        return original_target(root, path)

    with monkeypatch.context() as fresh:
        fresh.setattr(receipts, "_resolved_allowed_file_target", resolve_root_for_each_file)
        expected = walk()
        expected_valid = _verify(receipts.ModelReceiptVerifier(), snapshot)
    actual = walk()
    assert actual == expected
    assert _verify(receipts.ModelReceiptVerifier(), snapshot) == expected_valid is True
    if kind == "manifest":
        assert actual == [
            {"path": path, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
            for path, payload in sorted(FILES.items())
        ]
    else:
        assert actual is not None and actual.cacheable


@pytest.mark.parametrize("kind", ["manifest", "fingerprint"])
def test_root_resolution_is_bounded_but_every_file_is_resolved(snapshot, monkeypatch, kind):
    walk = _walk(snapshot, kind)
    identities = []
    original_identity = receipts._target_identity

    def record_identity(path):
        identities.append(path)
        return original_identity(path)

    monkeypatch.setattr(receipts, "_target_identity", record_identity)
    calls = _record_resolutions(monkeypatch)
    assert walk() is not None
    assert calls[snapshot, True] <= 2
    for relative in FILES:
        assert calls[snapshot / relative, True] == 1
    if kind == "fingerprint":
        assert Counter(identities) == Counter(snapshot / relative for relative in FILES)


def test_warm_receipt_keeps_file_checks_without_rehashing(snapshot, monkeypatch):
    verifier = receipts.ModelReceiptVerifier()
    assert _verify(verifier, snapshot)
    assert verifier._entries

    def unexpected_hash(_path):
        pytest.fail("A mature unchanged receipt should reuse its content hashes")

    monkeypatch.setattr(receipts, "_file_size_and_sha256", unexpected_hash)
    identities = []
    original_identity = receipts._target_identity

    def record_identity(path):
        identities.append(path)
        return original_identity(path)

    monkeypatch.setattr(receipts, "_target_identity", record_identity)
    calls = _record_resolutions(monkeypatch)
    assert _verify(verifier, snapshot)
    assert Counter(identities) == Counter(snapshot / relative for relative in FILES)
    assert all(calls[snapshot / relative, True] == 1 for relative in FILES)


def test_two_argument_helpers_resolve_the_root_on_every_call(snapshot, monkeypatch):
    path = snapshot / "weights.bin"
    calls = _record_resolutions(monkeypatch)
    assert receipts._resolved_allowed_file_target(snapshot, path) == path
    assert receipts._allowed_file_target(snapshot, path)
    assert calls[snapshot, True] == 2
    assert calls[path, True] == 2


def test_two_argument_helper_observes_a_retargeted_alias(snapshot, tmp_path):
    alias = tmp_path / "alias"
    other = tmp_path / "other"
    other.mkdir()
    (other / "weights.bin").write_bytes(b"other")
    try:
        alias.symlink_to(snapshot, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Directory symlinks are unavailable: {error}")
    assert receipts._resolved_allowed_file_target(alias, alias / "weights.bin") == snapshot / "weights.bin"
    alias.unlink()
    alias.symlink_to(other, target_is_directory=True)
    assert receipts._resolved_allowed_file_target(alias, alias / "weights.bin") == other / "weights.bin"


@pytest.mark.parametrize("kind", ["manifest", "fingerprint"])
def test_unavailable_root_inode_keeps_per_file_root_resolution(snapshot, monkeypatch, kind):
    original_stat = Path.stat

    def stat_without_root_inode(path, *, follow_symlinks=True):
        value = original_stat(path, follow_symlinks=follow_symlinks)
        if path != snapshot:
            return value
        fields = {name: getattr(value, name) for name in dir(value) if name.startswith("st_")}
        fields["st_ino"] = 0
        return SimpleNamespace(**fields)

    monkeypatch.setattr(Path, "stat", stat_without_root_inode)
    walk = _walk(snapshot, kind)
    calls = _record_resolutions(monkeypatch)
    assert walk() is not None
    assert calls[snapshot, True] >= len(FILES)
    assert all(calls[snapshot / relative, True] == 1 for relative in FILES)


@pytest.mark.parametrize("kind", ["manifest", "fingerprint"])
def test_ordinary_files_do_not_resolve_unused_blob_roots(snapshot, monkeypatch, kind):
    walk = _walk(snapshot, kind)
    original = Path.resolve
    repo = snapshot.parent.parent
    blob_roots = {repo / "blobs", repo.parent / "blobs"}

    def resolve(path, strict=False):
        assert path not in blob_roots, "Unused blob roots were resolved eagerly"
        return original(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    assert walk() is not None


@pytest.mark.parametrize("kind", ["manifest", "fingerprint"])
@pytest.mark.parametrize("store", ["repo", "shared"])
def test_blob_roots_and_symlink_targets_are_resolved_for_each_file(tmp_path, monkeypatch, kind, store):
    cache = tmp_path / "hub"
    repo = cache / "models--test--model"
    root = repo / "snapshots" / REVISION
    root.mkdir(parents=True)
    repo_blobs, shared_blobs = repo / "blobs", cache / "blobs"
    repo_blobs.mkdir()
    shared_blobs.mkdir()
    names = ("first.bin", "second.bin")
    for name in names:
        target = (repo_blobs if store == "repo" else shared_blobs) / name
        target.write_bytes(name.encode())
        try:
            if store == "shared":
                (repo_blobs / name).symlink_to(target)
            (root / name).symlink_to(repo_blobs / name)
        except OSError as error:
            pytest.skip(f"File symlinks are unavailable: {error}")
    receipts.write_download_receipt(root, model_id=MODEL_ID, revision=REVISION)
    walk = _walk(root.resolve(), kind)
    calls = _record_resolutions(monkeypatch)
    assert walk() is not None
    # Both the link gate and the regular-file gate retain strict target resolution.
    assert all(calls[root / name, True] == 2 for name in names)
    assert calls[repo_blobs, False] == 2 * len(names)
    assert calls[shared_blobs, False] == (2 * len(names) if store == "shared" else 0)


def _directory_link(link, target, kind):
    if kind == "symlink":
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"Directory symlinks are unavailable: {error}")
        return
    if os.name != "nt":
        pytest.skip("Native junction qualification requires Windows")
    # Fixture paths are data in environment variables, never interpolated shell code.
    command = (
        "$ErrorActionPreference = 'Stop'; "
        "New-Item -ItemType Junction -Path $env:ORPHEUS_RECEIPT_TEST_LINK "
        "-Target $env:ORPHEUS_RECEIPT_TEST_TARGET | Out-Null"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            env={**os.environ, "ORPHEUS_RECEIPT_TEST_LINK": str(link),
                 "ORPHEUS_RECEIPT_TEST_TARGET": str(target)},
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        pytest.skip(f"Junction fixture creation unavailable: {error}")
    if result.returncode:
        pytest.skip(f"Junction fixture creation failed: {result.stderr.strip()}")
    assert link.is_junction()


@pytest.mark.parametrize("replacement", ["directory", "missing", "symlink", "junction"])
def test_warm_receipt_rejects_root_drift_after_last_file_identity(snapshot, tmp_path, monkeypatch, replacement):
    verifier = receipts.ModelReceiptVerifier()
    assert _verify(verifier, snapshot)
    assert verifier._entries
    outside = tmp_path / "replacement-target"
    outside.mkdir()
    if replacement in {"symlink", "junction"}:
        probe = tmp_path / "link-probe"
        _directory_link(probe, outside, replacement)
        probe.unlink() if replacement == "symlink" else probe.rmdir()
    original = receipts._target_identity
    seen = 0

    def replace_after_identity(path):
        nonlocal seen
        identity = original(path)
        seen += 1
        if seen == len(FILES):
            snapshot.rename(snapshot.with_name("retired-snapshot"))
            if replacement == "directory":
                snapshot.mkdir()
            elif replacement in {"symlink", "junction"}:
                _directory_link(snapshot, outside, replacement)
        return identity

    monkeypatch.setattr(receipts, "_target_identity", replace_after_identity)
    assert not _verify(verifier, snapshot)
    assert seen == len(FILES)


def test_manifest_rejects_root_replacement_after_last_hash(snapshot, monkeypatch):
    original = receipts._file_size_and_sha256
    seen = 0

    def replace_after_hash(path):
        nonlocal seen
        result = original(path)
        seen += 1
        if seen == len(FILES):
            snapshot.rename(snapshot.with_name("retired-snapshot"))
            snapshot.mkdir()
        return result

    monkeypatch.setattr(receipts, "_file_size_and_sha256", replace_after_hash)
    with pytest.raises((OSError, ValueError)):
        receipts._snapshot_manifest(snapshot)
    assert seen == len(FILES)


@pytest.mark.parametrize("kind", ["manifest", "fingerprint"])
def test_ignored_transient_churn_does_not_look_like_root_replacement(snapshot, monkeypatch, kind):
    walk = _walk(snapshot, kind)
    expected = walk()
    hook = "_file_size_and_sha256" if kind == "manifest" else "_target_identity"
    original = getattr(receipts, hook)
    seen = 0

    def add_transient_after_last_file(path):
        nonlocal seen
        result = original(path)
        seen += 1
        if seen == len(FILES):
            (snapshot / "new-transient.lock").write_bytes(b"ignored")
        return result

    monkeypatch.setattr(receipts, hook, add_transient_after_last_file)
    assert walk() == expected
    assert seen == len(FILES)


@pytest.mark.parametrize("kind", ["manifest", "fingerprint"])
def test_strict_file_resolution_errors_are_not_hidden_by_root_reuse(snapshot, monkeypatch, kind):
    walk = _walk(snapshot, kind)
    original = Path.resolve

    def resolve(path, strict=False):
        if path == snapshot / "weights.bin":
            assert strict is True
            raise OSError("file became unavailable")
        return original(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    if kind == "manifest":
        with pytest.raises(ValueError, match="unsafe or missing"):
            walk()
    else:
        assert walk() is None
