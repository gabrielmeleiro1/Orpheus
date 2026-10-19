from pathlib import Path
import os
import stat
from types import SimpleNamespace

import pytest

from modules.contracts import durable_files
from modules.storage import deletion


@pytest.mark.parametrize("kind", ["file", "child", "directory"])
def test_staged_read_only_artifacts_retry_with_writable_mode(tmp_path, monkeypatch, kind):
    staged = tmp_path / "staged"
    if kind == "file":
        staged.write_bytes(b"source")
        protected = staged
    else:
        staged.mkdir()
        child = staged / "chapter.mp3"
        if kind == "child":
            child.write_bytes(b"audio")
        protected = child if kind == "child" else staged
    protected.chmod(0o444 if kind != "directory" else 0o555)
    denials = []
    modes = []
    unlink, rmdir, chmod = os.unlink, os.rmdir, os.chmod

    def remove(candidate, *args, **kwargs):
        if Path(candidate).name == protected.name and not protected.stat().st_mode & stat.S_IWRITE:
            denials.append(candidate)
            raise PermissionError("Windows read-only file")
        operation = rmdir if kind == "directory" else unlink
        return operation(candidate, *args, **kwargs)

    def make_writable(candidate, mode, *args, **kwargs):
        modes.append((Path(candidate), mode))
        return chmod(candidate, mode, *args, **kwargs)

    platform = SimpleNamespace(**(vars(os) | {"name": "nt", "chmod": make_writable}))
    monkeypatch.setattr(durable_files, "os", platform)
    monkeypatch.setattr(deletion, "os", platform)
    with monkeypatch.context() as patch:
        patch.setattr(os, "rmdir" if kind == "directory" else "unlink", remove)
        try:
            deletion.remove_staged_artifacts([(tmp_path / "original", staged)])
        except PermissionError:
            pass
    try:
        assert modes == [(protected, stat.S_IWRITE)], "Read-only removal must retry with writable mode"
        assert len(denials) == 1
        assert not staged.exists()
    finally:
        if protected.exists():
            chmod(protected, 0o700)


def test_staged_tree_deletion_does_not_change_a_symlink_target(tmp_path, monkeypatch):
    target = tmp_path / "target.txt"
    target.write_bytes(b"retained")
    staged = tmp_path / "staged"
    staged.mkdir()
    link = staged / "chapter.mp3"
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"Symlinks are unavailable: {error}")
    modes = []

    def deny_unlink(*args, **kwargs):
        raise PermissionError("link cannot be deleted")

    platform = SimpleNamespace(**(vars(os) | {"name": "nt"}))
    monkeypatch.setattr(deletion, "os", platform)
    monkeypatch.setattr(platform, "chmod", lambda *args: modes.append(args))
    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", deny_unlink)
        with pytest.raises(PermissionError, match="link cannot be deleted"):
            deletion.remove_staged_artifacts([(tmp_path / "original", staged)])
    assert modes == []
    assert link.is_symlink()
    assert target.read_bytes() == b"retained"


def test_staged_tree_deletion_retries_a_sharing_violation_only_once(tmp_path, monkeypatch):
    staged = tmp_path / "staged"
    staged.mkdir()
    child = staged / "chapter.mp3"
    child.write_bytes(b"audio")
    attempts = []

    def deny_unlink(candidate, *args, **kwargs):
        attempts.append(candidate)
        raise PermissionError("file in use")

    platform = SimpleNamespace(**(vars(os) | {"name": "nt"}))
    monkeypatch.setattr(deletion, "os", platform)
    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", deny_unlink)
        with pytest.raises(PermissionError, match="file in use"):
            deletion.remove_staged_artifacts([(tmp_path / "original", staged)])
    assert len(attempts) == 2
    assert child.exists()
    child.chmod(0o600)


@pytest.mark.parametrize("windows", [False, True])
def test_permission_denied_deletion_retries_only_once_on_windows(tmp_path, monkeypatch, windows):
    path = tmp_path / "source.txt"
    path.write_bytes(b"source")
    attempts = []
    modes = []

    def deny_unlink(candidate):
        attempts.append(candidate)
        raise PermissionError("file in use")

    platform = SimpleNamespace(**(vars(os) | {"name": "nt" if windows else "posix"}))
    monkeypatch.setattr(durable_files, "os", platform)
    monkeypatch.setattr(platform, "chmod", lambda candidate, mode: modes.append((candidate, mode)))
    monkeypatch.setattr(Path, "unlink", deny_unlink)
    with pytest.raises(PermissionError, match="file in use"):
        durable_files.unlink_file(path)
    assert len(attempts) == (2 if windows else 1)
    assert len(modes) == (1 if windows else 0)


def test_read_only_deletion_does_not_change_a_symlink_target(tmp_path, monkeypatch):
    target = tmp_path / "target.txt"
    target.write_bytes(b"retained")
    target.chmod(0o444)
    link = tmp_path / "source.txt"
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"Symlinks are unavailable: {error}")
    modes = []

    def deny_unlink(candidate):
        raise PermissionError("link cannot be deleted")

    platform = SimpleNamespace(**(vars(os) | {"name": "nt"}))
    monkeypatch.setattr(durable_files, "os", platform)
    monkeypatch.setattr(platform, "chmod", lambda *args: modes.append(args))
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", deny_unlink)
        with pytest.raises(PermissionError, match="link cannot be deleted"):
            durable_files.unlink_file(link)
    assert modes == []
    assert target.read_bytes() == b"retained"
    target.chmod(0o600)
