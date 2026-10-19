"""Stored absolute paths are independent of the workspace used for relative paths."""
from pathlib import Path

import pytest

from modules.app.services.library_service import LibraryService
from modules.storage.maintenance_paths import resolve_stored_path


@pytest.fixture
def service(tmp_path):
    result = LibraryService.__new__(LibraryService)
    result._storage_root = tmp_path / 'workspace'
    result._output_dir = tmp_path / 'custom-output'
    result._temp_dir = tmp_path / 'custom-temp'
    for directory in (result._storage_root, result._output_dir, result._temp_dir):
        directory.mkdir()
    return result


@pytest.mark.parametrize('relative', ['book.epub', 'nested/../book.epub', 'output/book.m4b', 'temp/upload/source.epub'])
def test_relative_paths_match_canonical_resolver_after_each_workspace_change(service, tmp_path, relative):
    for index in range(2):
        root = tmp_path / f'workspace-{index}'
        root.mkdir()
        service._storage_root = root
        assert service._resolve_stored_path(relative) == resolve_stored_path(
            relative, root.resolve(), output_root=service._output_dir, temp_root=service._temp_dir,
        )


@pytest.mark.parametrize('relative', ['book.epub', 'output/book.m4b', 'temp/upload.epub'])
def test_relative_paths_keep_workspace_resolution_failures(service, monkeypatch, relative):
    def unavailable_root():
        raise OSError('Workspace is unavailable')
    monkeypatch.setattr(service, '_workspace_root', unavailable_root)
    with pytest.raises(OSError, match='Workspace is unavailable'):
        service._resolve_stored_path(relative)


@pytest.mark.parametrize('raw', [None, ''])
def test_missing_paths_do_not_consult_workspace(service, monkeypatch, raw):
    monkeypatch.setattr(service, '_workspace_root', lambda: pytest.fail('Empty path must return directly'))
    assert service._resolve_stored_path(raw) is None


def test_absolute_path_keeps_lexical_form_without_unrelated_workspace_io(service, tmp_path, monkeypatch):
    raw = str(tmp_path / 'missing-subdirectory' / '..' / 'audio.m4b')
    expected = resolve_stored_path(raw, service._storage_root,
                                   output_root=service._output_dir, temp_root=service._temp_dir)
    monkeypatch.setattr(service, '_workspace_root', lambda: pytest.fail('Absolute path does not use workspace'))
    assert service._resolve_stored_path(raw) == expected == Path(raw).expanduser()
    assert '..' in service._resolve_stored_path(raw).parts


def test_home_expansion_becomes_an_absolute_path_without_workspace_io(service, tmp_path, monkeypatch):
    user_directory = tmp_path / 'synthetic-profile'
    user_directory.mkdir()
    monkeypatch.setenv('USERPROFILE', str(user_directory))
    monkeypatch.setenv('HOME', str(user_directory))
    monkeypatch.setattr(service, '_workspace_root', lambda: pytest.fail('Expanded absolute path does not use workspace'))
    assert service._resolve_stored_path('~/source.epub') == user_directory / 'source.epub'


def test_relative_path_observes_retargeted_workspace_alias(service, tmp_path):
    first, second, alias = (tmp_path / name for name in ('first', 'second', 'alias'))
    first.mkdir()
    second.mkdir()
    try:
        alias.symlink_to(first, target_is_directory=True)
    except OSError as error:
        pytest.skip(f'Directory symlink is unavailable: {error}')
    service._storage_root = alias
    assert service._resolve_stored_path('source.epub') == first / 'source.epub'
    alias.unlink()
    alias.symlink_to(second, target_is_directory=True)
    assert service._resolve_stored_path('source.epub') == second / 'source.epub'
