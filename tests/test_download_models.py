"""Model download helper tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import hashlib
import os
import re
from pathlib import Path
import threading
import time
from types import ModuleType

import pytest

from modules.runtime_profiles import runtime_profiles
from modules.tts.runtime_spec import (
    CLEANER_CUDA_DEFAULT_MODEL,
    CLEANER_MLX_DEFAULT_MODEL,
    KOKORO_MLX_DEFAULT_MODEL_REPO,
    KOKORO_PACKAGE_DEFAULT_MODEL,
)
from scripts import download_models


def test_default_model_list_tracks_package_kokoro_default() -> None:
    assert download_models.DEFAULT_MODEL_IDS == (KOKORO_PACKAGE_DEFAULT_MODEL,)


def test_dry_run_prints_default_model_without_downloading(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = download_models.main(["--dry-run"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert f"Would download {KOKORO_PACKAGE_DEFAULT_MODEL}" in captured.out


def test_named_groups_resolve_to_curated_runtime_models() -> None:
    assert download_models.resolve_model_groups(["tts-mlx"]) == (
        KOKORO_MLX_DEFAULT_MODEL_REPO,
    )
    # The bare base name is not a Hub repo; the engine loads the bf16 variant
    # by default and downloads must match it (regression for a 401/404 on
    # every tts-mlx install).
    assert KOKORO_MLX_DEFAULT_MODEL_REPO == "mlx-community/Kokoro-82M-bf16"
    assert download_models.resolve_model_groups(["tts-cpu-cuda"]) == (
        KOKORO_PACKAGE_DEFAULT_MODEL,
    )
    assert download_models.resolve_model_groups(["cleaner-mlx"]) == (
        "mlx-community/Llama-3.2-3B-Instruct-4bit",
    )
    assert download_models.resolve_model_groups(["cleaner-cuda"]) == (
        "Qwen/Qwen2.5-1.5B-Instruct",
    )
    assert CLEANER_MLX_DEFAULT_MODEL == "mlx-community/Llama-3.2-3B-Instruct-4bit"
    assert CLEANER_CUDA_DEFAULT_MODEL == "Qwen/Qwen2.5-1.5B-Instruct"


def test_named_groups_deduplicate_models_and_reject_unknown_groups() -> None:
    assert download_models.resolve_model_groups(["tts-mlx", "tts-mlx"]) == (
        KOKORO_MLX_DEFAULT_MODEL_REPO,
    )

    with pytest.raises(ValueError, match="Unknown model group"):
        download_models.resolve_model_groups(["custom-url"])


def test_group_dry_run_reports_exact_resolved_models(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = download_models.main(["--group", "tts-mlx", "--dry-run", "--json"])
    captured = capsys.readouterr()

    assert exit_code == 0
    output = json.loads(captured.out)
    assert output == {
        "dry_run": True,
        "event": "plan",
        "models": [KOKORO_MLX_DEFAULT_MODEL_REPO],
    }


def test_groups_and_explicit_models_deduplicate_in_stable_order(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = download_models.main(
        [
            "--group",
            "tts-mlx",
            "--group",
            "cleaner-mlx",
            "--model",
            KOKORO_MLX_DEFAULT_MODEL_REPO,
            "--model",
            "owner/custom-model",
            "--dry-run",
            "--json",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["models"] == [
        KOKORO_MLX_DEFAULT_MODEL_REPO,
        "mlx-community/Llama-3.2-3B-Instruct-4bit",
        "owner/custom-model",
    ]


def test_list_groups_json_reports_stable_allowlist(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = download_models.main(["--list-groups", "--json"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output == {
        "groups": [
            {"id": group_id, "models": list(model_ids)}
            for group_id, model_ids in download_models.MODEL_GROUPS.items()
        ]
    }


def test_runtime_profile_model_groups_are_downloadable() -> None:
    profile_groups = tuple(
        group_id
        for profile in runtime_profiles()
        for group_id in profile.model_groups
    )

    assert set(profile_groups) <= set(download_models.MODEL_GROUPS)
    assert download_models.resolve_model_groups(profile_groups)


def test_unknown_group_exits_without_downloading(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = download_models.main(
        ["--group", "unknown", "--dry-run", "--json"]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    error = json.loads(captured.err)
    assert error["event"] == "error"
    assert "Unknown model group" in error["error"]
    assert captured.out == ""


def test_explicit_model_rejects_urls(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = download_models.main(
        ["--model", "https://example.com/model", "--dry-run", "--json"]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    error = json.loads(captured.err)
    assert error["event"] == "error"
    assert "repository id, not a URL" in error["error"]
    assert captured.out == ""


def test_json_reports_plan_before_downloading(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    output_before_download: list[str] = []

    def fake_download_model_snapshot(
        model_id: str,
        *,
        cache_dir: Path | None = None,
        local_dir: Path | None = None,
    ) -> Path:
        del model_id, cache_dir, local_dir
        output_before_download.append(capsys.readouterr().out)
        return tmp_path / "snapshot"

    monkeypatch.setattr(
        download_models,
        "download_model_snapshot",
        fake_download_model_snapshot,
    )

    exit_code = download_models.main(["--group", "tts-mlx", "--json"])
    remaining_output = capsys.readouterr().out

    assert exit_code == 0
    assert json.loads(output_before_download[0]) == {
        "dry_run": False,
        "event": "plan",
        "models": [KOKORO_MLX_DEFAULT_MODEL_REPO],
    }
    assert json.loads(remaining_output) == {
        "event": "downloaded",
        "model": KOKORO_MLX_DEFAULT_MODEL_REPO,
        "path": str(tmp_path / "snapshot"),
    }


def test_json_download_failure_returns_error_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_download(*args: object, **kwargs: object) -> Path:
        del args, kwargs
        raise OSError("disk full")

    monkeypatch.setattr(download_models, "download_model_snapshot", fail_download)

    exit_code = download_models.main(["--group", "tts-mlx", "--json"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert json.loads(captured.err) == {
        "error": "Model download failed: disk full",
        "event": "error",
    }
    assert "Traceback" not in captured.err


def test_download_does_not_swallow_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt_download(*args: object, **kwargs: object) -> Path:
        del args, kwargs
        raise KeyboardInterrupt

    monkeypatch.setattr(download_models, "download_model_snapshot", interrupt_download)

    with pytest.raises(KeyboardInterrupt):
        download_models.main(["--group", "tts-mlx", "--json"])


def test_multiple_models_get_safe_distinct_local_subdirectories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    local_dirs: list[Path | None] = []

    def fake_download(
        model_id: str,
        *,
        cache_dir: Path | None = None,
        local_dir: Path | None = None,
    ) -> Path:
        del model_id, cache_dir
        local_dirs.append(local_dir)
        assert local_dir is not None
        return local_dir

    monkeypatch.setattr(download_models, "download_model_snapshot", fake_download)

    snapshots = download_models.download_required_models(
        ["owner/model", "owner-model"],
        local_dir=tmp_path / "models",
        emit_output=False,
    )

    assert snapshots == local_dirs
    assert len(set(local_dirs)) == 2
    assert all(path is not None and path.parent == tmp_path / "models" for path in local_dirs)
    assert all(
        path is not None and re.fullmatch(r"[A-Za-z0-9._-]+-[0-9a-f]{12}", path.name)
        for path in local_dirs
    )


def test_single_model_preserves_explicit_local_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    received_local_dirs: list[Path | None] = []

    def fake_download(
        model_id: str,
        *,
        cache_dir: Path | None = None,
        local_dir: Path | None = None,
    ) -> Path:
        del model_id, cache_dir
        received_local_dirs.append(local_dir)
        assert local_dir is not None
        return local_dir

    monkeypatch.setattr(download_models, "download_model_snapshot", fake_download)
    target = tmp_path / "bundle"

    download_models.download_required_models(
        ["owner/model"],
        local_dir=target,
        emit_output=False,
    )

    assert received_local_dirs == [target]


def test_download_model_snapshot_passes_cache_and_local_dirs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    fake_module = ModuleType("huggingface_hub")

    def fake_snapshot_download(**kwargs: object) -> str:
        calls.append(kwargs)
        snapshot = tmp_path / "snapshot"
        snapshot.mkdir()
        (snapshot / "config.json").write_text("{}", encoding="utf-8")
        return str(snapshot)

    fake_module.snapshot_download = fake_snapshot_download  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "huggingface_hub", fake_module)

    snapshot_path = download_models.download_model_snapshot(
        "hexgrad/Kokoro-82M",
        cache_dir=tmp_path / "cache",
        local_dir=tmp_path / "bundle",
    )

    assert snapshot_path == tmp_path / "snapshot"
    assert calls == [
        {
            "repo_id": "hexgrad/Kokoro-82M",
            "revision": download_models.MODEL_REVISIONS["hexgrad/Kokoro-82M"],
            "cache_dir": str(tmp_path / "cache"),
            "local_dir": str(tmp_path / "bundle"),
        }
    ]


def test_every_curated_model_is_pinned_to_a_commit_revision() -> None:
    pinned = download_models.MODEL_REVISIONS
    curated = {
        model_id
        for model_ids in download_models.MODEL_GROUPS.values()
        for model_id in model_ids
    }
    assert curated <= set(pinned), curated - set(pinned)
    for model_id, revision in pinned.items():
        assert download_models._REVISION_RE.fullmatch(revision), (model_id, revision)


def test_pinned_model_download_passes_revision_and_records_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_id = "hexgrad/Kokoro-82M"
    expected_revision = download_models.MODEL_REVISIONS[model_id]
    calls: list[dict[str, object]] = []
    snapshot = tmp_path / "models--hexgrad--Kokoro-82M" / "snapshots" / expected_revision
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}", encoding="utf-8")

    def fake_snapshot_download(**kwargs: object) -> str:
        calls.append(kwargs)
        return str(snapshot)

    fake_module = ModuleType("huggingface_hub")
    fake_module.snapshot_download = fake_snapshot_download  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "huggingface_hub", fake_module)

    download_models.download_model_snapshot(model_id)

    assert calls[0]["revision"] == expected_revision
    payload = json.loads(
        (snapshot / download_models.DOWNLOAD_RECEIPT_FILENAME).read_text(encoding="utf-8")
    )
    assert payload["revision"] == expected_revision


def test_successful_snapshot_download_writes_valid_completion_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_id = "owner/model"
    revision = "a" * 40
    snapshot = tmp_path / "models--owner--model" / "snapshots" / revision
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}", encoding="utf-8")
    weights = snapshot / "weights.safetensors"
    weights.write_bytes(b"weights")
    fake_module = ModuleType("huggingface_hub")
    fake_module.snapshot_download = lambda **kwargs: str(snapshot)  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "huggingface_hub", fake_module)

    result = download_models.download_model_snapshot(model_id)

    assert result == snapshot
    receipt = snapshot / download_models.DOWNLOAD_RECEIPT_FILENAME
    assert receipt.is_file()
    assert download_models.validate_download_receipt(
        snapshot,
        model_id=model_id,
        revision=revision,
    )
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["model_id"] == model_id
    assert payload["revision"] == revision
    assert payload["files"] == [
        {
            "path": "config.json",
            "size": 2,
            "sha256": hashlib.sha256(b"{}").hexdigest(),
        },
        {
            "path": "weights.safetensors",
            "size": 7,
            "sha256": hashlib.sha256(b"weights").hexdigest(),
        },
    ]


def test_failed_or_dry_run_download_never_writes_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_module = ModuleType("huggingface_hub")

    def fail(**kwargs: object) -> str:
        del kwargs
        raise OSError("download failed")

    fake_module.snapshot_download = fail  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "huggingface_hub", fake_module)

    with pytest.raises(OSError, match="download failed"):
        download_models.download_model_snapshot("owner/model", local_dir=tmp_path)
    assert not list(tmp_path.rglob(download_models.DOWNLOAD_RECEIPT_FILENAME))

    download_models.download_required_models(
        ["owner/model"],
        local_dir=tmp_path,
        dry_run=True,
        emit_output=False,
    )
    assert not list(tmp_path.rglob(download_models.DOWNLOAD_RECEIPT_FILENAME))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(schema_version=1),
        lambda payload: payload.update(model_id="other/model"),
        lambda payload: payload.update(revision="not a revision"),
        lambda payload: payload.update(files=[]),
        lambda payload: payload["files"].append(dict(payload["files"][0])),
        lambda payload: payload["files"][0].update(path="../escape"),
        lambda payload: payload["files"][0].update(path="/absolute"),
        lambda payload: payload["files"][0].update(path=download_models.DOWNLOAD_RECEIPT_FILENAME),
        lambda payload: payload["files"][0].update(sha256="not-a-digest"),
        lambda payload: payload["files"][0].pop("sha256"),
    ],
)
def test_completion_receipt_rejects_malformed_or_unsafe_payload(
    tmp_path: Path,
    mutate,
) -> None:
    model_id = "owner/model"
    revision = "b" * 40
    snapshot = tmp_path / "snapshots" / revision
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}", encoding="utf-8")
    download_models.write_download_receipt(snapshot, model_id=model_id, revision=revision)
    receipt = snapshot / download_models.DOWNLOAD_RECEIPT_FILENAME
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    mutate(payload)
    receipt.write_text(json.dumps(payload), encoding="utf-8")

    assert not download_models.validate_download_receipt(
        snapshot,
        model_id=model_id,
        revision=revision,
    )


def test_completion_receipt_rejects_missing_size_changed_and_extra_files(
    tmp_path: Path,
) -> None:
    model_id = "owner/model"
    revision = "c" * 40
    snapshot = tmp_path / "snapshots" / revision
    snapshot.mkdir(parents=True)
    model_file = snapshot / "model.bin"
    model_file.write_bytes(b"model")
    download_models.write_download_receipt(snapshot, model_id=model_id, revision=revision)

    model_file.unlink()
    assert not download_models.validate_download_receipt(snapshot, model_id=model_id, revision=revision)
    model_file.write_bytes(b"changed size")
    assert not download_models.validate_download_receipt(snapshot, model_id=model_id, revision=revision)
    model_file.write_bytes(b"model")
    (snapshot / "stale.tmp.json").write_text("stale", encoding="utf-8")
    assert not download_models.validate_download_receipt(snapshot, model_id=model_id, revision=revision)


def test_completion_receipt_rejects_same_size_content_corruption(tmp_path: Path) -> None:
    model_id = "owner/model"
    revision = "9" * 40
    snapshot = tmp_path / "snapshots" / revision
    snapshot.mkdir(parents=True)
    model_file = snapshot / "model.bin"
    model_file.write_bytes(b"original")
    download_models.write_download_receipt(snapshot, model_id=model_id, revision=revision)

    model_file.write_bytes(b"tampered")

    assert not download_models.validate_download_receipt(
        snapshot,
        model_id=model_id,
        revision=revision,
    )


def test_receipt_write_is_atomic_and_cleans_temporary_file_on_replace_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "snapshots" / ("d" * 40)
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}", encoding="utf-8")

    def fail_replace(source: object, destination: object) -> None:
        del source, destination
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        download_models.write_download_receipt(
            snapshot,
            model_id="owner/model",
            revision="d" * 40,
        )

    assert not (snapshot / download_models.DOWNLOAD_RECEIPT_FILENAME).exists()
    assert not list(snapshot.glob(".*.tmp"))


def test_receipt_allows_hf_blob_symlink_but_rejects_external_symlink(tmp_path: Path) -> None:
    model_id = "owner/model"
    revision = "f" * 40
    repo_root = tmp_path / "models--owner--model"
    snapshot = repo_root / "snapshots" / revision
    blobs = repo_root / "blobs"
    snapshot.mkdir(parents=True)
    blobs.mkdir()
    blob = blobs / "abc123"
    blob.write_bytes(b"weights")
    (snapshot / "model.safetensors").symlink_to(blob)

    download_models.write_download_receipt(snapshot, model_id=model_id, revision=revision)
    assert download_models.validate_download_receipt(
        snapshot,
        model_id=model_id,
        revision=revision,
    )

    receipt = snapshot / download_models.DOWNLOAD_RECEIPT_FILENAME
    external_receipt = tmp_path / "external-receipt.json"
    external_receipt.write_text(receipt.read_text(encoding="utf-8"), encoding="utf-8")
    receipt.unlink()
    receipt.symlink_to(external_receipt)
    assert not download_models.validate_download_receipt(
        snapshot,
        model_id=model_id,
        revision=revision,
    )
    receipt.unlink()
    download_models.write_download_receipt(snapshot, model_id=model_id, revision=revision)

    external = tmp_path / "outside.bin"
    external.write_bytes(b"outside")
    (snapshot / "unsafe.bin").symlink_to(external)
    assert not download_models.validate_download_receipt(
        snapshot,
        model_id=model_id,
        revision=revision,
    )

    second_revision = "0" * 40
    second_snapshot = repo_root / "snapshots" / second_revision
    second_snapshot.mkdir()
    external_dir = tmp_path / "outside-directory"
    external_dir.mkdir()
    (external_dir / "secret.bin").write_bytes(b"secret")
    (second_snapshot / "unsafe-directory").symlink_to(
        external_dir,
        target_is_directory=True,
    )
    with pytest.raises(ValueError, match="unsafe"):
        download_models.write_download_receipt(
            second_snapshot,
            model_id=model_id,
            revision=second_revision,
        )


def _verified_snapshot(tmp_path: Path, *, model_id: str, revision: str) -> tuple[Path, Path]:
    snapshot = tmp_path / model_id.replace("/", "--") / "snapshots" / revision
    snapshot.mkdir(parents=True)
    model_file = snapshot / "model.bin"
    model_file.write_bytes(b"original")
    download_models.write_download_receipt(
        snapshot,
        model_id=model_id,
        revision=revision,
    )
    return snapshot, model_file


def test_receipt_verification_accepts_windows_case_insensitive_file_order(tmp_path: Path) -> None:
    model_id, revision = "owner/model", "a" * 40
    snapshot = tmp_path / "snapshots" / revision
    snapshot.mkdir(parents=True)
    for name in ("config.json", "LICENSE", "README.md", "weights.bin"):
        (snapshot / name).write_bytes(name.encode())
    receipt = download_models.write_download_receipt(snapshot, model_id=model_id, revision=revision)
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    # Simulate an existing receipt written with Windows Path ordering. It must
    # validate on Windows and Unix without rewriting or trusting corrupt files.
    payload["files"].sort(key=lambda entry: entry["path"].lower())
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    assert download_models.validate_download_receipt(snapshot, model_id=model_id, revision=revision)
    verifier = download_models.ModelReceiptVerifier()
    assert verifier.verify(snapshot, model_id=model_id, revision=revision)
    (snapshot / "weights.bin").write_bytes(b"corrupted!!")
    assert not verifier.verify(snapshot, model_id=model_id, revision=revision)


def test_integrity_verifier_reuses_hash_when_metadata_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_id = "owner/model"
    revision = "3" * 40
    snapshot, _ = _verified_snapshot(tmp_path, model_id=model_id, revision=revision)
    real_hash = download_models._file_size_and_sha256
    calls = 0

    def counted_hash(path: Path) -> tuple[int, str]:
        nonlocal calls
        calls += 1
        return real_hash(path)

    monkeypatch.setattr(download_models, "_file_size_and_sha256", counted_hash)
    verifier = download_models.ModelReceiptVerifier(max_entries=8)

    assert verifier.verify(snapshot, model_id=model_id, revision=revision)
    assert verifier.verify(snapshot, model_id=model_id, revision=revision)
    assert calls == 1


def test_integrity_verifier_rehashes_same_size_metadata_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_id = "owner/model"
    revision = "4" * 40
    snapshot, model_file = _verified_snapshot(
        tmp_path,
        model_id=model_id,
        revision=revision,
    )
    real_hash = download_models._file_size_and_sha256
    calls = 0

    def counted_hash(path: Path) -> tuple[int, str]:
        nonlocal calls
        calls += 1
        return real_hash(path)

    monkeypatch.setattr(download_models, "_file_size_and_sha256", counted_hash)
    verifier = download_models.ModelReceiptVerifier(max_entries=8)
    assert verifier.verify(snapshot, model_id=model_id, revision=revision)

    model_file.write_bytes(b"tampered")
    stat = model_file.stat()
    os.utime(model_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))

    assert not verifier.verify(snapshot, model_id=model_id, revision=revision)
    assert calls == 2


def test_integrity_verifier_rehashes_when_receipt_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_id = "owner/model"
    revision = "5" * 40
    snapshot, _ = _verified_snapshot(tmp_path, model_id=model_id, revision=revision)
    real_hash = download_models._file_size_and_sha256
    calls = 0

    def counted_hash(path: Path) -> tuple[int, str]:
        nonlocal calls
        calls += 1
        return real_hash(path)

    monkeypatch.setattr(download_models, "_file_size_and_sha256", counted_hash)
    verifier = download_models.ModelReceiptVerifier(max_entries=8)
    assert verifier.verify(snapshot, model_id=model_id, revision=revision)

    receipt = snapshot / download_models.DOWNLOAD_RECEIPT_FILENAME
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["files"][0]["sha256"] = "0" * 64
    receipt.write_text(json.dumps(payload), encoding="utf-8")

    assert not verifier.verify(snapshot, model_id=model_id, revision=revision)
    assert calls == 2


def test_integrity_verifier_is_single_flight_for_concurrent_hashes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_id = "owner/model"
    revision = "6" * 40
    snapshot, _ = _verified_snapshot(tmp_path, model_id=model_id, revision=revision)
    real_hash = download_models._file_size_and_sha256
    calls = 0
    lock = threading.Lock()

    def counted_hash(path: Path) -> tuple[int, str]:
        nonlocal calls
        with lock:
            calls += 1
        time.sleep(0.05)
        return real_hash(path)

    monkeypatch.setattr(download_models, "_file_size_and_sha256", counted_hash)
    verifier = download_models.ModelReceiptVerifier(max_entries=8)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda _: verifier.verify(snapshot, model_id=model_id, revision=revision),
                range(8),
            )
        )

    assert results == [True] * 8
    assert calls == 1


def test_integrity_verifiers_do_not_share_results_across_apps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_id = "owner/model"
    revision = "7" * 40
    snapshot, _ = _verified_snapshot(tmp_path, model_id=model_id, revision=revision)
    real_hash = download_models._file_size_and_sha256
    calls = 0

    def counted_hash(path: Path) -> tuple[int, str]:
        nonlocal calls
        calls += 1
        return real_hash(path)

    monkeypatch.setattr(download_models, "_file_size_and_sha256", counted_hash)
    first = download_models.ModelReceiptVerifier(max_entries=8)
    second = download_models.ModelReceiptVerifier(max_entries=8)

    assert first.verify(snapshot, model_id=model_id, revision=revision)
    assert second.verify(snapshot, model_id=model_id, revision=revision)
    assert calls == 2


def test_integrity_verifier_evicts_lru_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first_model = "owner/first"
    second_model = "owner/second"
    first_revision = "8" * 40
    second_revision = "a" * 40
    first, _ = _verified_snapshot(
        tmp_path,
        model_id=first_model,
        revision=first_revision,
    )
    second, _ = _verified_snapshot(
        tmp_path,
        model_id=second_model,
        revision=second_revision,
    )
    real_hash = download_models._file_size_and_sha256
    calls = 0

    def counted_hash(path: Path) -> tuple[int, str]:
        nonlocal calls
        calls += 1
        return real_hash(path)

    monkeypatch.setattr(download_models, "_file_size_and_sha256", counted_hash)
    verifier = download_models.ModelReceiptVerifier(max_entries=1)

    assert verifier.verify(first, model_id=first_model, revision=first_revision)
    assert verifier.verify(second, model_id=second_model, revision=second_revision)
    assert verifier.verify(first, model_id=first_model, revision=first_revision)
    assert calls == 3
