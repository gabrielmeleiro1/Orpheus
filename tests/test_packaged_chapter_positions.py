"""Packaged chapter positions survive conversion, repair, and schema upgrades."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from modules.app.controller import ConversionJob, JobStatus
from modules.app.services.library_service import LibraryService
from modules.audio.packager import ChapterMarker
from modules.pipeline.orchestrator import ChapterResult, ConversionPipeline
from modules.storage import migrations
from modules.storage.migrations import LATEST_SCHEMA_VERSION
from modules.storage.models import BookCreate, ChapterCreate, SourceType
from modules.storage.sqlite_repo import SQLiteRepository
from tests.test_conversion_service import _make_service
from tests.test_web_api import _make_client_with_controller


def test_book_detail_keeps_chapter_offsets_from_before_concurrent_repair(tmp_path):
    repo = SQLiteRepository(tmp_path / 'library.db')
    projected = Event()
    repaired = Event()
    try:
        book, chapters = repo.create_book_with_chapters(
            BookCreate(title='Book', source_path=str(tmp_path / 'source.md'),
                       source_type=SourceType.MD, playback_path='output/Book/old.m4b'),
            [ChapterCreate(book_id=0, chapter_number=1, title='One',
                           duration_ms=300, packaged_start_ms=0)],
        )
        library = LibraryService(repo, output_dir=tmp_path / 'output', temp_dir=tmp_path / 'temp')

        def get_book(book_id):
            detail = library.get_book(book_id)
            projected.set()
            assert repaired.wait(timeout=5)
            return detail

        client = _make_client_with_controller(SimpleNamespace(get_book=get_book, repository=repo))
        with patch.object(repo, 'get_chapters', wraps=repo.get_chapters) as read_chapters:
            with ThreadPoolExecutor(max_workers=1) as executor:
                response = executor.submit(client.get, f'/api/library/{book.id}')
                try:
                    assert projected.wait(timeout=5)
                    repo.apply_repair_result(
                        book.id,
                        repaired_chapter_updates=[{
                            'chapter_id': chapters[0].id, 'duration_ms': 400,
                            'packaged_start_ms': 75, 'mp3_path': 'output/Book/01.m4a',
                        }],
                        playback_path='output/Book/repaired.m4b',
                        playback_status='packaged', total_chapters=1,
                    )
                finally:
                    repaired.set()
                result = response.result(timeout=5)
            assert result.status_code == 200
            payload = result.json()
            assert payload['playback_path'] == 'output/Book/old.m4b'
            assert payload['playback_url'] == f'/api/library/{book.id}/audio'
            assert payload['created_at'] == book.created_at.isoformat()
            assert payload['chapters'] == [{
                'id': chapters[0].id, 'number': 1, 'title': 'One',
                'duration_ms': 300, 'mp3_path': None, 'packaged_start_ms': 0,
            }]
            read_chapters.assert_not_called()
    finally:
        repo.close()


@pytest.mark.parametrize('operation', ['conversion', 'repair'])
def test_packaged_chapter_starts_are_persisted_and_exposed(tmp_path, operation):
    source = tmp_path / 'source.md'
    source.write_text('# One\n\nFirst.\n\n# Two\n\nSecond.\n\n# Three\n\nThird.')
    document = SimpleNamespace(
        title='Book', author=None,
        chapters=[SimpleNamespace(number=n, title=f'Chapter {n}', content='Prose.') for n in (1, 2, 3)],
    )
    service = _make_service(tmp_path, ConversionPipeline, job_cls=ConversionJob, status_enum=JobStatus)
    repo = SQLiteRepository(service._config.db_path)
    service._repository = repo
    starts = [0, 350, 800]

    def dispatch(context, chapters, output_dir):
        results = []
        for chapter in chapters:
            audio = output_dir / f'{chapter.number:02d}.m4a'
            audio.write_bytes(b'aac')
            results.append(ChapterResult(
                chapter.number, chapter.title, aac_path=audio, mp3_path=audio,
                duration_ms=[300, 400, 500][chapter.number - 1],
            ))
        return results

    def run_ffmpeg(command, **kwargs):
        Path(command[-1]).write_bytes(b'packaged audio')

    def markers(_packager, _concat, _audio, names, **kwargs):
        return [ChapterMarker(name, start, end) for name, start, end in zip(names, starts, [*starts[1:], 1400])]

    try:
        with patch('modules.pipeline.pipeline_stages.PipelineStages.ingest', return_value=document), patch('modules.pipeline.orchestrator.dispatch_chapters', side_effect=dispatch), patch('modules.audio.packager.run_ffmpeg_command', side_effect=run_ffmpeg), patch('modules.audio.packager.M4BPackager._stream_copy_chapter_markers', autospec=True, side_effect=markers):
            job = ConversionJob(id='convert', book_id=None, status=JobStatus.RUNNING)
            service._active_job = job
            service._run_conversion(job, source, None)
            assert job.status == JobStatus.COMPLETED, job.error
            book, stored = repo.get_book_with_chapters(job.book_id)
            if operation == 'repair':
                Path(stored[1].mp3_path).unlink()
                starts[:] = [0, 375, 850]
                with patch('modules.audio.encoder.AudioEncoder.get_duration', return_value=0.5):
                    repair_job = service.start_repair(book.id)
                    repair_job._thread.join(timeout=5)
                assert repair_job.status == JobStatus.COMPLETED, repair_job.error
                book, stored = repo.get_book_with_chapters(book.id)
            assert [getattr(chapter, 'packaged_start_ms', None) for chapter in stored] == starts
            assert [chapter.duration_ms for chapter in stored] == [300, 400, 500]
            assert [chapter.packaged_start_ms for chapter in repo.get_chapters(book.id)] == starts
            library = LibraryService(repo, output_dir=service._config.output_dir, temp_dir=service._config.temp_dir)
            controller = SimpleNamespace(get_book=library.get_book, repository=repo)
            response = _make_client_with_controller(controller).get(f'/api/library/{book.id}')
            assert response.status_code == 200
            assert [chapter['packaged_start_ms'] for chapter in response.json()['chapters']] == starts
    finally:
        repo.close()


def test_previous_schema_migrates_chapter_starts_without_changing_durations(tmp_path):
    db_path = tmp_path / 'previous.db'
    with sqlite3.connect(db_path) as conn:
        migrations._bootstrap_schema_v5(conn)
        columns = {row[1] for row in conn.execute('PRAGMA table_info(chapters)')}
        if 'packaged_start_ms' in columns:
            conn.execute('ALTER TABLE chapters DROP COLUMN packaged_start_ms')
        conn.execute('CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)')
        conn.execute('INSERT INTO schema_migrations VALUES (7)')
        conn.execute("INSERT INTO books (title, source_path, source_type) VALUES ('Old Book', 'source.md', 'md')")
        conn.execute("INSERT INTO chapters (book_id, chapter_number, title, duration_ms, mp3_path) VALUES (1, 1, 'One', 300, 'one.m4a')")
    repo = SQLiteRepository(db_path)
    try:
        with sqlite3.connect(db_path) as conn:
            assert conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == LATEST_SCHEMA_VERSION
            assert conn.execute('SELECT duration_ms, packaged_start_ms FROM chapters').fetchone() == (300, None)
        chapter = repo.get_chapters(1)[0]
        assert chapter.packaged_start_ms is None
        assert chapter.mp3_path == 'one.m4a'
        new_book, chapters = repo.create_book_with_chapters(
            BookCreate(title='New Book', source_path='new.md', source_type=SourceType.MD),
            [ChapterCreate(book_id=0, chapter_number=1, title='One', packaged_start_ms=0)],
        )
        assert chapters[0].packaged_start_ms == 0
        chapter = repo.create_chapter(ChapterCreate(book_id=new_book.id, chapter_number=2, title='Two', packaged_start_ms=350))
        assert chapter.packaged_start_ms == 350
    finally:
        repo.close()
