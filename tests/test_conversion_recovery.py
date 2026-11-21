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


def test_upload_is_retained_until_recovery_source_and_inventory_are_durable(tmp_path, monkeypatch):
    import os
    import stat
    import shutil

    from modules.app.services.library_service import LibraryService
    from modules.contracts.recovery_inventory import INVENTORY_NAME, SOURCE_DIRECTORY

    service = _make_service(tmp_path, ConversionPipeline, job_cls=ConversionJob, status_enum=JobStatus)
    repo = SQLiteRepository(service._config.db_path)
    service._repository = repo
    service.upload_reference_checker = lambda _path: False
    library = LibraryService(repo, output_dir=service._config.output_dir, temp_dir=service._config.temp_dir)
    handle = library.upload_text_source(text='Original prose.', title='Book')['source_handle']
    source = service._config.temp_dir / 'web_uploads' / handle.removeprefix('upload:')
    job = ConversionJob(id='book', book_id=None, status=JobStatus.RUNNING)
    job.upload_source_path = source.resolve()
    service._active_job = job
    document = SimpleNamespace(title='Book', author=None, chapters=[SimpleNamespace(number=1, title='One', content='Prose')])
    events = []
    copied = []
    copyfile, fsync, replace, unlink = shutil.copyfile, os.fsync, os.replace, Path.unlink

    def copy(src, dst, *args, **kwargs):
        result = copyfile(src, dst, *args, **kwargs)
        if Path(dst).parent.name == SOURCE_DIRECTORY:
            copied.append(Path(dst))
        return result

    def sync(fd):
        fsync(fd)
        info = os.fstat(fd)
        if copied:
            if stat.S_ISDIR(info.st_mode):
                parent = copied[0].parent
                if info.st_ino == parent.stat().st_ino:
                    events.append('source_directory_synced')
                elif info.st_ino == parent.parent.stat().st_ino:
                    events.append('inventory_directory_synced')
            elif copied[0].exists() and info.st_ino == copied[0].stat().st_ino:
                events.append('source_file_synced')
            else:
                events.append('inventory_file_synced')

    def publish(src, dst):
        replace(src, dst)
        if Path(dst).parent.name == SOURCE_DIRECTORY:
            events.append('source_published')
        elif Path(dst).name == INVENTORY_NAME:
            events.append('inventory_published')

    def remove(path, *args, **kwargs):
        if path == source:
            events.append('upload_deleted')
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(shutil, 'copyfile', copy)
    monkeypatch.setattr(os, 'fsync', sync)
    monkeypatch.setattr(os, 'replace', publish)
    monkeypatch.setattr(Path, 'unlink', remove)
    try:
        with patch('modules.pipeline.pipeline_stages.PipelineStages.ingest', return_value=document), patch('modules.pipeline.orchestrator.dispatch_chapters', side_effect=RuntimeError('Interrupted synthesis')):
            service._run_conversion(job, source, None)
        expected = ['source_file_synced', 'source_published']
        if os.name != 'nt':
            expected.append('source_directory_synced')
        expected.extend(['inventory_file_synced', 'inventory_published'])
        if os.name != 'nt':
            expected.append('inventory_directory_synced')
        expected.append('upload_deleted')
        assert events == expected
        assert job.book_id is not None
        assert not source.exists()
    finally:
        repo.close()


def test_repair_running_observer_can_read_the_active_job(tmp_path, monkeypatch):
    import threading

    from modules.app.controller import AppController, ConversionCallbacks
    from modules.app.events import JobState

    service = _make_service(tmp_path, None, job_cls=ConversionJob, status_enum=JobStatus)
    controller = object.__new__(AppController)
    controller._conversion_service = service
    lock = service._job_lock

    class BoundedLock:
        def __enter__(self):
            assert lock.acquire(timeout=0.2), 'Observer blocked reading the active job'

        def __exit__(self, *args):
            lock.release()

    monkeypatch.setattr(service._job_coordinator, '_job_lock', BoundedLock())
    monkeypatch.setattr(service._runtime_options_resolver, 'with_setup_defaults', lambda options: options)
    observed = []
    ran = threading.Event()

    def observe(event):
        if getattr(event, 'state', None) == JobState.RUNNING:
            observed.append(controller.get_active_job())
            assert not ran.is_set(), 'Repair ran before its RUNNING event'

    monkeypatch.setattr(service, '_run_repair', lambda **kwargs: ran.set())
    job = service.start_repair(1, ConversionCallbacks(on_event=observe))
    assert job.wait(3)
    assert observed == [job]
    assert ran.is_set()


def test_repair_launch_failure_observer_can_read_the_active_job(tmp_path, monkeypatch):
    import threading

    from modules.app.controller import ConversionCallbacks

    service = _make_service(tmp_path, None, job_cls=ConversionJob, status_enum=JobStatus)
    lock = service._job_lock
    observed = []

    class BoundedLock:
        def __enter__(self):
            assert lock.acquire(timeout=0.2), 'Observer blocked reading the active job'

        def __exit__(self, *args):
            lock.release()

    def fail_start(_thread):
        raise RuntimeError('Thread could not start')

    def observe(error):
        observed.append((error, service.get_active_job().status))

    monkeypatch.setattr(service._job_coordinator, '_job_lock', BoundedLock())
    monkeypatch.setattr(service._runtime_options_resolver, 'with_setup_defaults', lambda options: options)
    monkeypatch.setattr(threading.Thread, 'start', fail_start)
    with pytest.raises(RuntimeError, match='Thread could not start'):
        service.start_repair(1, ConversionCallbacks(on_error=observe))
    assert observed == [('Thread could not start', JobStatus.FAILED)]
    assert service.get_active_job() is None


@pytest.mark.parametrize('filename', ['a' * 220 + '.txt', 'é' * 120 + '.txt', '漢' * 80 + '.txt'], ids=['ascii', 'accented', 'cjk'])
def test_recovery_copies_long_source_filenames_without_changing_bytes(tmp_path, filename):
    from modules.pipeline.book_workspace import prepare_book_workspace
    from modules.pipeline.recovery import prepare_recovery_source

    source = tmp_path / filename
    source.write_bytes('Original source bytes: café.\n'.encode('utf-8'))
    workspace = prepare_book_workspace(tmp_path / 'output', 'Book')
    durable = prepare_recovery_source(source, workspace, [], emit_mp3=True)

    assert durable.name == filename
    assert len(durable.name.encode('utf-8')) <= 255
    assert durable.read_bytes() == source.read_bytes()
    inventory = json.loads((workspace.output_dir / '.orpheus-recovery.json').read_text())
    assert inventory['source'] == durable.name


@pytest.mark.parametrize('title', ['a' * 200, 'é' * 120, '漢' * 120], ids=['ascii', 'accented', 'cjk'])
def test_pasted_text_with_long_title_registers_recovery_before_synthesis(tmp_path, title):
    from modules.app.services.library_service import LibraryService

    service = _make_service(tmp_path, ConversionPipeline, job_cls=ConversionJob, status_enum=JobStatus)
    repo = SQLiteRepository(service._config.db_path)
    service._repository = repo
    library = LibraryService(repo, output_dir=service._config.output_dir, temp_dir=service._config.temp_dir)
    uploaded = library.upload_text_source(text='Original prose for synthesis.', title=title)
    stored_name = uploaded['source_handle'].removeprefix('upload:')
    source = service._config.temp_dir / 'web_uploads' / stored_name
    assert len(stored_name.encode('utf-8')) <= 255
    assert source.suffix == '.md'
    document = SimpleNamespace(
        title=title, author=None,
        chapters=[SimpleNamespace(number=1, title='One', content='Original prose for synthesis.')],
    )
    job = ConversionJob(id='long-title', book_id=None, status=JobStatus.RUNNING)
    service._active_job = job

    def dispatch(context, chapters, output_dir):
        books = repo.list_books(BookFilters())
        assert len(books) == 1
        durable = Path(books[0].source_path)
        assert durable.read_bytes() == source.read_bytes()
        assert len(durable.name.encode('utf-8')) <= 255
        raise RuntimeError('Interrupted synthesis')

    try:
        with patch('modules.pipeline.pipeline_stages.PipelineStages.ingest', return_value=document), patch('modules.pipeline.orchestrator.dispatch_chapters', side_effect=dispatch) as synthesis:
            service._run_conversion(job, source, None)
        assert synthesis.called, job.error
        assert job.book_id is not None
        assert job.error == 'Interrupted synthesis'
    finally:
        repo.close()


@pytest.mark.parametrize('filename', ['a' * 246 + '.mp3', 'é' * 123 + '.mp3'], ids=['ascii', 'accented'])
def test_atomic_publish_accepts_a_250_byte_final_filename(tmp_path, filename):
    from modules.audio.ffmpeg import atomic_output_path

    output = tmp_path / filename
    with atomic_output_path(output) as pending:
        assert pending.parent == output.parent
        assert len(pending.name.encode('utf-8')) <= 255
        pending.write_bytes(b'complete audio')

    assert output.read_bytes() == b'complete audio'
    assert not pending.exists()


@pytest.mark.parametrize('filename', ['a' * 300 + '.md', 'é' * 200 + '.md', '漢' * 120 + '.md'], ids=['ascii', 'accented', 'cjk'])
@pytest.mark.parametrize('prefix', ['', '1234567890_12345678_'])
def test_source_filename_budgets_utf8_bytes_including_prefix_and_extension(filename, prefix):
    from modules.ingestion.filenames import source_filename

    result = source_filename(filename, prefix=prefix)

    assert result.startswith(prefix)
    assert result.endswith('.md')
    stem = result[len(prefix):-3]
    assert filename[:-3].startswith(stem)
    assert len(result.encode('utf-8')) <= 255
    assert len((prefix + stem + filename[len(stem)] + '.md').encode('utf-8')) > 255


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


@pytest.mark.parametrize(('operation', 'observer'), [
    ('conversion', 'complete'), ('conversion', 'event'), ('conversion', 'log'),
    ('repair', 'complete'), ('repair', 'event'), ('repair', 'log'),
])
def test_completion_observer_failure_preserves_committed_success(tmp_path, caplog, observer, operation):
    from modules.app.controller import ConversionCallbacks
    from modules.app.events import JobState
    from modules.pipeline.models import RepairResult
    from tests.test_conversion_service import _PipelineStub

    source = tmp_path / 'source.md'
    source.write_text('Book text')
    output = tmp_path / 'book.m4b'
    output.write_bytes(b'audio')
    result = RepairResult(success=True, title='Book', author=None, output_path=output,
                          chapters=[ChapterResult(1, 'One', duration_ms=1000)], total_duration_ms=1000)

    class Pipeline(_PipelineStub):
        def __init__(self, **kwargs):
            super().__init__(result=result, **kwargs)

        def repair(self, **kwargs):
            return result

    service = _make_service(tmp_path, Pipeline, job_cls=ConversionJob, status_enum=JobStatus)
    repo = SQLiteRepository(service._config.db_path)
    service._repository = repo
    events = []
    completed = []
    errors = []

    def on_complete(value):
        completed.append(value)
        if observer == 'complete':
            raise RuntimeError('completion unavailable')

    def on_event(event):
        events.append(event)
        if observer == 'event' and getattr(event, 'state', None) == JobState.COMPLETED:
            raise RuntimeError('completion event unavailable')

    def on_log(*args):
        if observer == 'log':
            raise RuntimeError('completion log unavailable')

    callbacks = ConversionCallbacks(on_complete=on_complete, on_event=on_event,
                                    on_error=errors.append, on_log=on_log)
    job = ConversionJob(id='completed', book_id=None, status=JobStatus.RUNNING)
    service._active_job = job
    try:
        if operation == 'repair':
            service._run_conversion(job, source, None)
            book_id = job.book_id
            job = ConversionJob(id='repair', book_id=book_id, status=JobStatus.RUNNING)
            service._active_job = job
            service._run_repair(job=job, book_id=book_id, callbacks=callbacks)
        else:
            service._run_conversion(job, source, callbacks)
        assert job.status == JobStatus.COMPLETED
        assert job.result.success
        assert job.error is None
        assert errors == []
        assert sum(getattr(event, 'state', None) == JobState.COMPLETED for event in events) == 1
        assert repo.count_books() == 1
        assert repo.get_book(job.book_id).playback_status == PlaybackStatus.PACKAGED
        assert completed == [job.result]
        assert 'observer delivery failed' in caplog.text
        assert any(getattr(event, 'message', '').startswith('[PERF]') for event in events)
    finally:
        repo.close()
