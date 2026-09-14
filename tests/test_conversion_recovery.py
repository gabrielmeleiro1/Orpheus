"""Interrupted new conversions remain repairable across application restarts."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json

import pytest

from modules.app.controller import ConversionJob, JobStatus
from modules.app.services.conversion_commands import resolve_repair_context
from modules.app.services.conversion_runtime import ConversionRuntimeOptions
from modules.pipeline.orchestrator import ChapterResult, ConversionPipeline
from modules.storage.models import BookFilters, PlaybackStatus
from modules.storage.sqlite_repo import SQLiteRepository
from tests.test_conversion_service import _make_service


@pytest.mark.parametrize('outcome', ['cancel', 'failure', 'success'])
@pytest.mark.parametrize('emit_mp3', [True, False])
def test_new_conversion_keeps_durable_inventory_and_publishes_only_real_audio(tmp_path, outcome, emit_mp3):
    source = tmp_path / 'input.md'
    source.write_text('# One\n\nFirst.\n\n# Two\n\nSecond.')
    chapters = [SimpleNamespace(number=i, title=title, content=title) for i, title in [(1, 'One'), (2, 'Two')]]
    document = SimpleNamespace(title='Recovery Book', author='Author', chapters=chapters)
    service = _make_service(tmp_path, ConversionPipeline, job_cls=ConversionJob, status_enum=JobStatus)
    repo = SQLiteRepository(service._config.db_path)
    service._repository = repo
    service._config.emit_chapter_mp3_artifacts = emit_mp3
    job = ConversionJob(id='recovery', book_id=None, status=JobStatus.RUNNING)
    service._active_job = job
    captured = {}

    def dispatch(context, _source_chapters, output_dir):
        # Inventory must be committed before the first audio is synthesized.
        books = repo.list_books(BookFilters())
        assert len(books) == 1
        assert books[0].completed_chapters == 0
        book, stored = repo.get_book_with_chapters(books[0].id)
        assert book.playback_status == PlaybackStatus.MISSING
        assert [ch.title for ch in stored] == ['One', 'Two']
        assert all(ch.mp3_path is None for ch in stored)
        assert Path(book.source_path).read_bytes() == source.read_bytes()
        assert Path(book.source_path).name == source.name
        from modules.storage.maintenance_audit import audit_storage_state
        audit = audit_storage_state(service._config.db_path, output_dir=service._config.output_dir, temp_dir=service._config.temp_dir, grace_seconds=0)
        assert audit['orphan_output_dirs'] == []
        assert json.loads(book.conversion_runtime_options)['voice'] == 'af_bella'
        assert job.book_id == book.id
        captured['book_id'] = book.id
        extension = 'mp3' if emit_mp3 else 'm4a'
        first = output_dir / f'01_One.{extension}'
        first.write_bytes(b'audio')
        captured['first'] = first
        # A fresh service/repository can resolve the just-published artifact,
        # even before any result/cancellation callback runs.
        restarted_repo = SQLiteRepository(service._config.db_path)
        restarted = _make_service(tmp_path, ConversionPipeline)
        restarted._repository = restarted_repo
        repair = resolve_repair_context(restarted, book_id=book.id)
        assert repair.resolve_chapter_audio(repair.chapters[0]) == first
        assert repair.resolve_chapter_audio(repair.chapters[1]) is None
        restarted_repo.close()
        result = ChapterResult(1, 'One', mp3_path=first, duration_ms=1000)
        if outcome == 'failure':
            raise RuntimeError('Interrupted worker')
        if outcome == 'cancel':
            job.pipeline.cancel()
            return [result]
        second = output_dir / f'02_Two.{extension}'
        second.write_bytes(b'audio')
        return [result, ChapterResult(2, 'Two', mp3_path=second, duration_ms=1000)]

    def package(**kwargs):
        output = kwargs['output_dir'] / 'book.m4b'
        output.write_bytes(b'packaged')
        return output

    with patch('modules.pipeline.pipeline_stages.PipelineStages.ingest', return_value=document), patch('modules.pipeline.orchestrator.dispatch_chapters', side_effect=dispatch), patch('modules.pipeline.pipeline_stages.PipelineStages.package', side_effect=package):
        service._run_conversion(job, source, None, runtime_options=ConversionRuntimeOptions(voice='af_bella'))

    assert 'book_id' in captured, job.error
    assert repo.count_books() == 1
    book, stored = repo.get_book_with_chapters(captured['book_id'])
    assert Path(stored[0].mp3_path) == captured['first']
    if outcome == 'success':
        assert job.status == JobStatus.COMPLETED
        assert book.playback_status == PlaybackStatus.PACKAGED
    else:
        assert job.status == (JobStatus.CANCELLED if outcome == 'cancel' else JobStatus.FAILED)
        assert book.playback_status != PlaybackStatus.PACKAGED
        assert stored[1].mp3_path is None
        assert repo.list_books(BookFilters())[0].completed_chapters == 1
        source.unlink()  # Repair must not depend on the upload or original location.
        repaired_numbers = []
        def repair_dispatch(context, missing, output_dir):
            assert context.config.voice == 'af_bella'
            repaired_numbers.extend(chapter.number for chapter in missing)
            second = output_dir / ('02_Two.mp3' if emit_mp3 else '02_Two.m4a')
            second.write_bytes(b'audio')
            return [ChapterResult(2, 'Two', mp3_path=second, duration_ms=2000)]
        with patch('modules.pipeline.pipeline_stages.PipelineStages.ingest', return_value=document), patch('modules.pipeline.orchestrator.dispatch_chapters', side_effect=repair_dispatch), patch('modules.pipeline.pipeline_stages.PipelineStages.package', side_effect=package), patch('modules.audio.encoder.AudioEncoder.get_duration', return_value=1.0):
            repair_job = service.start_repair(book.id)
            repair_job._thread.join(timeout=5)
        assert repair_job.status == JobStatus.COMPLETED, repair_job.error
        assert repaired_numbers == [2]
        assert repo.count_books() == 1
        repaired_book, repaired_chapters = repo.get_book_with_chapters(book.id)
        assert repaired_book.playback_status == PlaybackStatus.PACKAGED
        assert [chapter.duration_ms for chapter in repaired_chapters] == [1000, 2000]
        assert Path(repaired_chapters[0].mp3_path) == captured['first']
    repo.close()


def test_repair_probes_checkpoint_duration_instead_of_packaging_zero_length(tmp_path):
    from modules.pipeline.repair import _existing_repair_result
    stored = SimpleNamespace(chapter_number=1, title='One', duration_ms=0)
    audio = tmp_path / 'one.m4a'
    audio.write_bytes(b'audio')
    with patch('modules.audio.encoder.AudioEncoder.get_duration', return_value=1.75) as probe:
        result = _existing_repair_result(stored, audio)
    assert result.duration_ms == 1750
    probe.assert_called_once_with(audio)


def test_empty_recovery_inventory_owns_reserved_folder_for_open_and_delete(tmp_path):
    from modules.app.services.library_service import LibraryService
    from modules.pipeline.book_workspace import prepare_book_workspace
    from modules.pipeline.recovery import prepare_recovery_source
    from modules.storage.models import BookCreate, ChapterCreate, SourceType
    service = _make_service(tmp_path, ConversionPipeline)
    repo = SQLiteRepository(service._config.db_path)
    library = LibraryService(repo, output_dir=service._config.output_dir, temp_dir=service._config.temp_dir)
    source = tmp_path / 'original.txt'
    source.write_text('First chapter')
    old = prepare_book_workspace(service._config.output_dir, 'Same Title')
    old_marker = old.output_dir / 'keep.txt'
    old_marker.write_text('existing unrelated book')
    workspace = prepare_book_workspace(service._config.output_dir, 'Same Title')
    durable = prepare_recovery_source(source, workspace, [SimpleNamespace(number=1, title='One')], emit_mp3=True)
    book, chapters = repo.create_book_with_chapters(
        BookCreate(title='Same Title', source_path=str(durable), source_type=SourceType.TXT, total_chapters=1),
        [ChapterCreate(book_id=0, chapter_number=1, title='One')],
    )
    assert library._book_output_dir(book, chapters) == workspace.output_dir
    assert library.delete_book(book.id)
    assert not workspace.output_dir.exists()
    assert old_marker.is_file()
    repo.close()


def test_terminal_recovery_checkpoint_error_does_not_replace_original_failure(tmp_path):
    from modules.pipeline.orchestrator import PipelineConfig
    source = tmp_path / 'input.txt'
    source.write_text('Text')
    pipeline = ConversionPipeline(PipelineConfig(output_dir=tmp_path / 'out', temp_dir=tmp_path / 'temp'))
    document = SimpleNamespace(title='Book', author=None, chapters=[SimpleNamespace(number=1, title='One', content='Text')])
    calls = []
    def checkpoint(*args):
        calls.append(args)
        if len(calls) > 1:
            raise OSError('Database temporarily unavailable')
    pipeline.set_recovery_callback(checkpoint)
    with patch.object(pipeline.stages, 'ingest', return_value=document), patch('modules.pipeline.orchestrator.dispatch_chapters', side_effect=RuntimeError('Original failure')):
        result = pipeline.convert(source)
    assert result.success is False
    assert result.error == 'Original failure'
    assert len(calls) == 2


@pytest.mark.parametrize('emit_mp3', [True, False])
@pytest.mark.parametrize('has_audio', [True, False])
def test_repair_preserves_inventory_audio_mode_across_config_change_and_second_interruption(tmp_path, emit_mp3, has_audio):
    from modules.app.services.conversion_commands import RepairRunCommand
    from modules.pipeline.book_workspace import prepare_book_workspace
    from modules.pipeline.orchestrator import PipelineConfig
    from modules.pipeline.recovery import prepare_recovery_source, chapter_audio_path
    from tests.test_repair import _write_playable_audio

    source = tmp_path / 'original.md'
    source.write_text('# One\nText\n# Two\nText')
    chapters = [SimpleNamespace(number=number, title=title) for number, title in [(1, 'One'), (2, 'Two')]]
    workspace = prepare_book_workspace(tmp_path / 'output', 'Book')
    durable = prepare_recovery_source(source, workspace, chapters, emit_mp3=emit_mp3)
    book = SimpleNamespace(title='Book', author=None, source_path=str(durable))
    stored = [SimpleNamespace(chapter_number=chapter.number, title=chapter.title, mp3_path=None) for chapter in chapters]
    service = SimpleNamespace(
        _repository=SimpleNamespace(get_book_with_chapters=lambda book_id: (book, stored)),
        _config=SimpleNamespace(db_path=tmp_path / 'app.db', output_dir=tmp_path / 'output', temp_dir=tmp_path / 'temp'),
    )
    if has_audio:
        _write_playable_audio(chapter_audio_path(chapters[0], workspace.chapters_dir, '.mp3' if emit_mp3 else '.m4a'))
    context = resolve_repair_context(service, book_id=77)
    config = PipelineConfig(emit_chapter_mp3_artifacts=not emit_mp3, enable_chapter_aac_packaging=emit_mp3)
    command = RepairRunCommand(
        job=SimpleNamespace(), callbacks=None, runtime_options=ConversionRuntimeOptions(),
        conversion_mode='audiobook', failure_message='Repair failed',
        invalid_artifacts_message='Invalid repair', cancel_message_text='Cancelled',
        book_id=77, repair_context=context,
    )
    command.adjust_pipeline_config(config)
    assert config.emit_chapter_mp3_artifacts is emit_mp3
    assert config.enable_chapter_aac_packaging is (not emit_mp3)
    # Simulate a repair encoder publishing a file followed by process death
    # before any repair result has updated chapter rows.
    second = chapter_audio_path(chapters[1], workspace.chapters_dir, '.mp3' if config.emit_chapter_mp3_artifacts else '.m4a')
    _write_playable_audio(second)
    restarted = resolve_repair_context(service, book_id=77)
    assert restarted.resolve_chapter_audio(stored[1]) == second
