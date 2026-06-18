"""Model download helper tests."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest

from modules.tts.runtime_spec import KOKORO_PACKAGE_DEFAULT_MODEL
from scripts import download_models


def test_default_model_list_tracks_package_kokoro_default() -> None:
    assert download_models.DEFAULT_MODEL_IDS == (KOKORO_PACKAGE_DEFAULT_MODEL,)


def test_dry_run_prints_default_model_without_downloading(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = download_models.main(["--dry-run"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert f"Would download {KOKORO_PACKAGE_DEFAULT_MODEL}" in captured.out


def test_download_model_snapshot_passes_cache_and_local_dirs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    fake_module = ModuleType("huggingface_hub")

    def fake_snapshot_download(**kwargs: object) -> str:
        calls.append(kwargs)
        return str(tmp_path / "snapshot")

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
            "cache_dir": str(tmp_path / "cache"),
            "local_dir": str(tmp_path / "bundle"),
        }
    ]
