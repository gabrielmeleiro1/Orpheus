"""Windows Hub bootstrap and partial-download recovery regressions."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from modules import hf_cache

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cache_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in (
        "HF_HUB_CACHE",
        "HUGGINGFACE_HUB_CACHE",
        "HF_HUB_DISABLE_SYMLINKS",
        "HF_HUB_DISABLE_SYMLINKS_WARNING",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.setattr(hf_cache, "sys", SimpleNamespace(platform="win32", modules={}))
    monkeypatch.setattr(hf_cache, "_configured", False)
    return tmp_path / "hub"


def test_privilege_failure_disables_symlinks_once(monkeypatch, cache_env):
    probe = Mock(side_effect=OSError(1314, "A required privilege is not held by the client"))
    monkeypatch.setattr(hf_cache.os, "symlink", probe)

    hf_cache.configure_hf_cache()
    hf_cache.configure_hf_cache()

    assert os.environ["HF_HUB_DISABLE_SYMLINKS"] == "1"
    assert os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] == "1"
    probe.assert_called_once()
    source, link = probe.call_args.args
    assert source == "source"  # Relative target, on the cache's filesystem.
    assert link.parent.parent == cache_env
    assert list(cache_env.iterdir()) == []


def test_supported_symlinks_leave_environment_unchanged(monkeypatch, cache_env):
    # Simulate a working filesystem even on Windows hosts without the privilege.
    def symlink(source, link):
        link.write_bytes((link.parent / source).read_bytes())

    probe = Mock(side_effect=symlink)
    monkeypatch.setattr(hf_cache.os, "symlink", probe)
    monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == "link")
    hf_cache.configure_hf_cache()
    hf_cache.configure_hf_cache()

    assert "HF_HUB_DISABLE_SYMLINKS" not in os.environ
    assert "HF_HUB_DISABLE_SYMLINKS_WARNING" not in os.environ
    probe.assert_called_once()


@pytest.mark.parametrize("value", ["0", "1", "false", ""])
def test_explicit_user_setting_is_preserved(monkeypatch, cache_env, value):
    monkeypatch.setenv("HF_HUB_DISABLE_SYMLINKS", value)
    probe = Mock()
    monkeypatch.setattr(hf_cache.os, "symlink", probe)
    hf_cache.configure_hf_cache()
    assert os.environ["HF_HUB_DISABLE_SYMLINKS"] == value
    assert "HF_HUB_DISABLE_SYMLINKS_WARNING" not in os.environ
    assert not cache_env.exists()
    probe.assert_not_called()


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_non_windows_does_nothing(monkeypatch, cache_env, platform):
    monkeypatch.setattr(hf_cache, "sys", SimpleNamespace(platform=platform))
    hf_cache.configure_hf_cache()
    assert "HF_HUB_DISABLE_SYMLINKS" not in os.environ
    assert "HF_HUB_DISABLE_SYMLINKS_WARNING" not in os.environ
    assert not cache_env.exists()


@pytest.mark.parametrize("variable", ["HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"])
def test_cache_override_is_probed(monkeypatch, cache_env, variable):
    override = cache_env.parent / "override"
    monkeypatch.setenv(variable, str(override))
    probe = Mock(side_effect=OSError(1314, "denied"))
    monkeypatch.setattr(hf_cache.os, "symlink", probe)
    hf_cache.configure_hf_cache()
    assert probe.call_args.args[1].parent.parent == override
    assert not cache_env.exists()


def test_inaccessible_cache_disables_symlinks(monkeypatch, cache_env):
    cache_env.write_text("not a directory")
    hf_cache.configure_hf_cache()
    assert os.environ["HF_HUB_DISABLE_SYMLINKS"] == "1"


def test_existing_read_only_cache_fails_without_retrying(monkeypatch, cache_env):
    cache_env.mkdir()
    mkdir = Path.mkdir
    attempts = []

    def denied_probe(path, *args, **kwargs):
        if path.parent == cache_env:
            attempts.append(path)
            raise PermissionError("cache is read-only")
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", denied_probe)
    hf_cache.configure_hf_cache()
    assert os.environ["HF_HUB_DISABLE_SYMLINKS"] == "1"
    assert len(attempts) == 1


@pytest.mark.parametrize(
    "module",
    [
        "apps.launchers.desktop_backend",
        "scripts.download_models",
        "modules.app.services.model_download_service",  # multiprocessing spawn entry
    ],
)
def test_entry_configures_before_first_hub_import(tmp_path, module):
    code = textwrap.dedent("""
        import importlib
        import os
        import sys
        from types import SimpleNamespace
        from modules import hf_cache

        assert 'huggingface_hub' not in sys.modules
        hf_cache.sys = SimpleNamespace(platform='win32', modules=sys.modules)
        def denied(*args, **kwargs):
            assert 'huggingface_hub' not in sys.modules
            raise OSError(1314, 'privilege not held')
        hf_cache.os.symlink = denied
        class CheckHubImport:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == 'huggingface_hub':
                    assert hf_cache._configured
                    assert os.environ['HF_HUB_DISABLE_SYMLINKS'] == '1'
                return None
        sys.meta_path.insert(0, CheckHubImport())
        importlib.import_module(sys.argv[1])
        from huggingface_hub import constants
        assert constants.HF_HUB_DISABLE_SYMLINKS
        assert constants.HF_HUB_DISABLE_SYMLINKS_WARNING
        # Child interpreters inherit the decision before importing Hub, too.
        import subprocess
        subprocess.run([sys.executable, '-c',
            'from huggingface_hub import constants; '
            'assert constants.HF_HUB_DISABLE_SYMLINKS; '
            'assert constants.HF_HUB_DISABLE_SYMLINKS_WARNING'], check=True)
    """)
    env = dict(os.environ, HF_HOME=str(tmp_path))
    for name in (
        "HF_HUB_CACHE",
        "HUGGINGFACE_HUB_CACHE",
        "HF_HUB_DISABLE_SYMLINKS",
        "HF_HUB_DISABLE_SYMLINKS_WARNING",
    ):
        env.pop(name, None)
    result = subprocess.run(
        [sys.executable, "-c", code, module],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_frozen_entry_uses_configured_launcher():
    spec = (REPO_ROOT / "desktop" / "backend-sidecar.spec").read_text(encoding="utf-8")
    assert 'entry_script = project_root / "apps" / "launchers" / "desktop_backend.py"' in spec
    assert "[str(entry_script)]" in spec


def test_existing_blob_recovers_missing_snapshot_without_symlinks(monkeypatch, tmp_path):
    from huggingface_hub import constants, hf_hub_download
    from huggingface_hub import file_download

    monkeypatch.setattr(constants, "HF_HUB_DISABLE_SYMLINKS", True)
    revision = "a" * 40
    etag = "b" * 40
    repo = tmp_path / "models--owner--model"
    blob = repo / "blobs" / etag
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"downloaded before symlink failure")
    monkeypatch.setattr(
        file_download,
        "_get_metadata_or_catch_error",
        lambda **kwargs: (
            "https://huggingface.co/unused",
            etag,
            revision,
            blob.stat().st_size,
            None,
            None,
        ),
    )
    transfer = Mock(side_effect=AssertionError("Existing blob must not be downloaded again"))
    monkeypatch.setattr(file_download, "_download_to_tmp_and_move", transfer)
    symlink = Mock(side_effect=OSError(1314, "denied"))
    monkeypatch.setattr(file_download.os, "symlink", symlink)
    # A provisional True from the broken Hub probe must not override the flag.
    monkeypatch.setitem(file_download._are_symlinks_supported_in_dir, str(repo.resolve()), True)

    snapshot = Path(
        hf_hub_download(
            "owner/model",
            "weights.bin",
            revision=revision,
            cache_dir=tmp_path,
        )
    )

    assert snapshot == repo / "snapshots" / revision / "weights.bin"
    assert snapshot.is_file() and not snapshot.is_symlink()
    assert snapshot.read_bytes() == blob.read_bytes()
    transfer.assert_not_called()
    symlink.assert_not_called()
