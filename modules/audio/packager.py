"""
M4B Packager Module
===================
Creates M4B audiobooks with chapter markers using FFmpeg.
Optimized for Apple Books and other audiobook players.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from collections import deque
import json
import logging
import re
import subprocess
import tempfile
import threading
import time

from ..subprocess_utils import background_process_kwargs
from .ffmpeg import (
    atomic_output_path,
    ffmpeg_timeout_seconds,
    kill_ffmpeg_process,
    require_ffmpeg_binary,
    run_ffmpeg_command,
    wait_for_ffmpeg_process,
    wav_duration_seconds,
)

logger = logging.getLogger(__name__)


def _raise_if_cancelled(cancel_requested: Callable[[], bool] | None) -> None:
    if cancel_requested is not None and cancel_requested():
        raise RuntimeError("M4B creation failed: cancelled")


def quote_ffmpeg_concat_path(path: Path | str) -> str:
    """Quote a path for FFmpeg concat demuxer list files.

    The concat demuxer copies bytes verbatim inside single quotes, so the only
    character that needs escaping is the single quote itself (close the quote,
    emit an escaped quote, reopen). Backslashes must be left untouched or
    Windows drive and UNC paths point at nonexistent files.
    """
    escaped_path = str(Path(path).absolute()).replace("'", "'\\''")
    return f"'{escaped_path}'"


def write_ffmpeg_concat_file(concat_file: Path, audio_files: list[Path]) -> None:
    """Write an FFmpeg concat demuxer file with safely quoted paths."""
    with open(concat_file, "w", encoding="utf-8") as handle:
        for audio_file in audio_files:
            handle.write(f"file {quote_ffmpeg_concat_path(audio_file)}\n")


def expected_audio_seconds(
    audio_files: list[Path], chapter_durations_ms: Optional[list[int]] = None,
) -> float | None:
    """Total length of the chapters being packaged, or None when unknown.

    Uses known chapter durations, else WAV headers; compressed inputs without
    known durations leave it unknown (the fixed FFmpeg deadline then applies).
    """
    if chapter_durations_ms is not None and len(chapter_durations_ms) == len(audio_files):
        return sum(max(0, int(ms)) for ms in chapter_durations_ms) / 1000
    total = 0.0
    for audio_file in audio_files:
        seconds = wav_duration_seconds(audio_file) if audio_file.suffix.lower() == ".wav" else None
        if seconds is None:
            return None
        total += seconds
    return total


def audio_files_support_stream_copy(audio_files: list[Path]) -> bool:
    """Return True when chapter inputs can be concatenated with AAC stream copy."""
    if not audio_files:
        return False
    return all(path.suffix.lower() == ".m4a" for path in audio_files)


def _escape_ffmetadata_value(value: str) -> str:
    """Escape FFmetadata scalar values so titles cannot break key/value lines."""
    # ffmpeg reads a backslash as "the next character is literal", so escaping
    # a newline as \n would decode to the letter n. These are single-line
    # display strings, so collapse whitespace instead.
    collapsed = re.sub(r"\s+", " ", str(value)).strip()
    return (
        collapsed
        .replace("\\", "\\\\")
        .replace("=", "\\=")
        .replace(";", "\\;")
        .replace("#", "\\#")
    )


@dataclass
class ChapterMarker:
    """Chapter marker for M4B TOC."""
    title: str
    start_ms: int
    end_ms: int


@dataclass
class AudiobookMetadata:
    """Metadata for audiobook files."""
    title: str
    author: str
    narrator: Optional[str] = None
    year: Optional[str] = None
    genre: str = "Audiobook"
    comment: Optional[str] = None
    cover_path: Optional[Path | str] = None


class M4BPackager:
    """
    M4B audiobook packager using FFmpeg.
    
    Creates M4B files with chapter markers, metadata, and cover art.
    Compatible with Apple Books and other audiobook players.
    """
    
    def __init__(self):
        """Initialize the M4B packager."""
        self.ffmpeg_path = require_ffmpeg_binary("ffmpeg")
        self.chapter_markers: list[ChapterMarker] = []
        # Length of the book being packaged, when known; FFmpeg deadlines for
        # the concat, probes, and final mux scale with it.
        self._audio_seconds: float | None = None
    
    def create_m4b(
        self,
        mp3_files: list[Path | str],
        chapter_names: list[str],
        metadata: AudiobookMetadata,
        output_path: Path | str,
        chapter_durations_ms: Optional[list[int]] = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> Path:
        """
        Create M4B audiobook from chapter audio files.

        Args:
            mp3_files: Historical parameter name; accepts ordered chapter audio
                files such as WAV masters or MP3 compatibility artifacts.
            chapter_names: List of chapter names (same length as mp3_files)
            metadata: Audiobook metadata
            output_path: Path for output M4B file
            chapter_durations_ms: Optional precomputed chapter durations
            
        Returns:
            Path to generated M4B file
        """
        self.chapter_markers = []
        if not mp3_files:
            raise ValueError("mp3_files must not be empty")
        if len(mp3_files) != len(chapter_names):
            raise ValueError("mp3_files and chapter_names must have same length")
        if chapter_durations_ms is not None and len(chapter_durations_ms) != len(mp3_files):
            raise ValueError("chapter_durations_ms must have same length as mp3_files")
        
        audio_files = [Path(f) for f in mp3_files]
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._audio_seconds = expected_audio_seconds(audio_files, chapter_durations_ms)
        
        # Verify all input files exist
        for audio_file in audio_files:
            if not audio_file.exists():
                raise FileNotFoundError(f"Chapter audio file not found: {audio_file}")
        
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            concat_file = temp_path / "concat.txt"
            write_ffmpeg_concat_file(concat_file, audio_files)

            has_cover = bool(metadata.cover_path and Path(metadata.cover_path).exists())
            use_stream_copy = audio_files_support_stream_copy(audio_files)

            if use_stream_copy:
                # Stream-copied AAC chapters keep the fast copy path, but the concat
                # demuxer re-bakes each segment's encoder priming/padding into the
                # joined presentation timeline. Source WAV durations therefore no
                # longer describe where chapters land, so markers derived from them
                # progressively lag the real audio. Concatenate first, read each
                # chapter's true start offset from the encoded timeline, then mux the
                # markers onto the copied audio.
                stream_copy_audio = temp_path / "chapters_streamcopy.m4a"
                concat_cmd = [
                    self.ffmpeg_path, "-y",
                    "-f", "concat",
                    "-safe", "0",
                    "-i", str(concat_file),
                    "-vn",
                    "-c:a", "copy",
                    str(stream_copy_audio),
                ]
                run_ffmpeg_command(
                    concat_cmd,
                    failure_message="M4B stream-copy concat failed",
                    cancel_requested=cancel_requested,
                    audio_seconds=self._audio_seconds,
                )

                chapters = self._stream_copy_chapter_markers(
                    stream_copy_audio,
                    audio_files,
                    chapter_names,
                    fallback_durations_ms=chapter_durations_ms,
                    cancel_requested=cancel_requested,
                )
                chapter_file = temp_path / "chapters.txt"
                self._write_ffmetadata(chapter_file, chapters, metadata)

                final_cmd = [
                    self.ffmpeg_path, "-y",
                    "-i", str(stream_copy_audio),
                    "-f", "ffmetadata",
                    "-i", str(chapter_file),
                ]
                if has_cover:
                    final_cmd.extend(["-i", str(metadata.cover_path)])
                final_cmd.extend(["-map_metadata", "1", "-map_chapters", "1", "-c:a", "copy"])
                if has_cover:
                    final_cmd.extend([
                        "-map", "0:a",
                        "-map", "2:v",
                        "-c:v", "mjpeg",
                        "-disposition:v", "attached_pic",
                    ])
                final_cmd.extend(["-movflags", "+faststart"])
            else:
                # Re-encode path (WAV/MP3 -> single continuous encode): the encoder
                # lays down one gapless stream, so source durations already describe
                # the timeline correctly.
                chapters = self._generate_chapter_markers(
                    audio_files,
                    chapter_names,
                    chapter_durations_ms=chapter_durations_ms,
                    cancel_requested=cancel_requested,
                )
                chapter_file = temp_path / "chapters.txt"
                self._write_ffmetadata(chapter_file, chapters, metadata)

                final_cmd = [
                    self.ffmpeg_path, "-y",
                    "-f", "concat",
                    "-safe", "0",
                    "-i", str(concat_file),
                    "-f", "ffmetadata",
                    "-i", str(chapter_file),
                ]
                if has_cover:
                    final_cmd.extend(["-i", str(metadata.cover_path)])
                final_cmd.extend(["-map_metadata", "1", "-map_chapters", "1", "-c:a", "aac", "-b:a", "128k"])
                if has_cover:
                    final_cmd.extend([
                        "-map", "0:a",
                        "-map", "2:v",
                        "-c:v", "mjpeg",
                        "-disposition:v", "attached_pic",
                    ])
                final_cmd.extend(["-movflags", "+faststart"])

            with atomic_output_path(output_path) as temp_output_path:
                final_cmd.append(str(temp_output_path))
                run_ffmpeg_command(
                    final_cmd,
                    failure_message="M4B creation failed",
                    cancel_requested=cancel_requested,
                    audio_seconds=self._audio_seconds,
                )

        self.chapter_markers = chapters
        return output_path

    def _generate_chapter_markers(
        self,
        audio_files: list[Path],
        chapter_names: list[str],
        chapter_durations_ms: Optional[list[int]] = None,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> list[ChapterMarker]:
        """Generate chapter markers from audio file durations."""
        _raise_if_cancelled(cancel_requested)
        chapters = []
        current_ms = 0

        if chapter_durations_ms is not None:
            durations = [max(0, int(ms)) for ms in chapter_durations_ms]
            for chapter_name, duration_ms in zip(chapter_names, durations):
                _raise_if_cancelled(cancel_requested)
                chapters.append(ChapterMarker(
                    title=chapter_name,
                    start_ms=current_ms,
                    end_ms=current_ms + duration_ms,
                ))
                current_ms += duration_ms
            return chapters

        from .encoder import AudioEncoder
        encoder = AudioEncoder()

        for audio_file, chapter_name in zip(audio_files, chapter_names):
            _raise_if_cancelled(cancel_requested)
            try:
                if cancel_requested is None:
                    duration_sec = encoder.get_duration(audio_file)
                else:
                    duration_sec = encoder.get_duration(audio_file, cancel_requested=cancel_requested)
            except (RuntimeError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                _raise_if_cancelled(cancel_requested)
                logger.warning(
                    "Could not probe duration of %s (%s); marking chapter %r with a "
                    "zero-length span so packaging can continue",
                    audio_file,
                    exc,
                    chapter_name,
                )
                duration_sec = 0.0
            _raise_if_cancelled(cancel_requested)
            duration_ms = int(duration_sec * 1000)
            
            chapters.append(ChapterMarker(
                title=chapter_name,
                start_ms=current_ms,
                end_ms=current_ms + duration_ms
            ))
            
            current_ms += duration_ms

        return chapters

    def _stream_copy_chapter_markers(
        self,
        concat_path: Path,
        audio_files: list[Path],
        chapter_names: list[str],
        fallback_durations_ms: Optional[list[int]] = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> list[ChapterMarker]:
        """Derive chapter markers from a stream-copied AAC concat's real timeline.

        Stream-copied segments contribute their encoder priming/padding to the
        joined presentation timeline, so per-source-WAV durations drift from where
        chapters actually begin. Read the true per-chapter start offsets from the
        encoded concat instead. Falls back to source-derived markers if the
        timeline cannot be probed, so packaging never fails on probe issues.
        """
        offsets = self._probe_stream_copy_offsets_ms(
            concat_path, audio_files, cancel_requested=cancel_requested
        )
        _raise_if_cancelled(cancel_requested)
        if offsets is None:
            logger.warning(
                "Could not probe stream-copied chapter timeline for %s; "
                "falling back to source-derived chapter markers",
                concat_path,
            )
            return self._generate_chapter_markers(
                audio_files,
                chapter_names,
                chapter_durations_ms=fallback_durations_ms,
                cancel_requested=cancel_requested,
            )

        starts_ms, total_ms = offsets
        chapters: list[ChapterMarker] = []
        for index, (chapter_name, start_ms) in enumerate(zip(chapter_names, starts_ms)):
            end_ms = starts_ms[index + 1] if index + 1 < len(starts_ms) else total_ms
            chapters.append(ChapterMarker(
                title=chapter_name,
                start_ms=start_ms,
                end_ms=max(start_ms, end_ms),
            ))
        return chapters

    def _probe_stream_copy_offsets_ms(
        self,
        concat_path: Path,
        audio_files: list[Path],
        cancel_requested: Callable[[], bool] | None = None,
    ) -> Optional[tuple[list[int], int]]:
        """Return (per-chapter start ms, total ms) for a stream-copied concat.

        Stream copy preserves every input packet in order, so each chapter starts
        at the joined presentation-timestamp of its first packet. That timestamp
        cannot be predicted from any single input file (the concat demuxer
        redistributes priming/padding at seams), so probe the encoded concat itself
        and index it by each input's packet count.
        """
        try:
            ffprobe = require_ffmpeg_binary("ffprobe")

            segment_packet_counts = self._probe_segment_packet_counts(
                ffprobe, audio_files, cancel_requested, self._audio_seconds
            )

            # One boundary packet index per chapter, in chapter order. A chapter
            # with zero audio packets shares its successor's boundary index, so a
            # set would dedup it away: the returned starts list would then be
            # shorter than chapter_names, zip() would truncate the TOC, every
            # chapter after the empty one would get a wrong start, and the final
            # chapter would vanish from M4B navigation. Keep a per-chapter list so
            # each chapter retains its own start (an empty chapter is a valid
            # zero-span marker at its successor's offset).
            per_chapter_boundaries: list[int] = []
            boundary_index = 0
            for count in segment_packet_counts:
                per_chapter_boundaries.append(boundary_index)
                boundary_index += count

            unique_boundaries = set(per_chapter_boundaries)
            boundary_pts, packet_count = self._probe_concat_packet_starts(
                ffprobe, concat_path, unique_boundaries, cancel_requested=cancel_requested
            )
            if packet_count == 0 or len(boundary_pts) != len(unique_boundaries):
                return None

            duration_result = run_ffmpeg_command(
                [
                    ffprobe, "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "csv=p=0",
                    str(concat_path),
                ],
                failure_message="FFprobe failed",
                cancel_requested=cancel_requested,
                audio_seconds=self._audio_seconds,
            )
            total_ms = int(round(float(duration_result.stdout.strip()) * 1000))

            starts_ms = [
                int(round(boundary_pts[index] * 1000))
                for index in per_chapter_boundaries
            ]
            return starts_ms, total_ms
        except (
            RuntimeError,
            ValueError,
            KeyError,
            TypeError,
            IndexError,
            MemoryError,
            json.JSONDecodeError,
        ) as exc:
            # A failed probe can use source durations; a user cancellation must
            # stop packaging before any fallback probes or final mux begin.
            _raise_if_cancelled(cancel_requested)
            logger.warning(
                "Stream-copy chapter timeline probe failed for %s: %s", concat_path, exc
            )
            return None

    @staticmethod
    def _probe_segment_packet_counts(
        ffprobe: str,
        audio_files: list[Path],
        cancel_requested: Callable[[], bool] | None,
        audio_seconds: float | None = None,
    ) -> list[int]:
        """Count independently encoded chapters with at most two active probes.

        Counts remain in chapter order. Keep only two futures queued so a long
        book does not create an unbounded task list, and stop the sibling probe
        before falling back if a count fails or cancellation is requested.
        """
        stopped = threading.Event()
        parallel_probes = len(audio_files) >= 4

        def cancelled() -> bool:
            return stopped.is_set() or bool(cancel_requested and cancel_requested())

        def count_packets(audio_file: Path) -> int:
            try:
                if cancelled():
                    raise RuntimeError("FFprobe failed: cancelled")
                result = run_ffmpeg_command(
                    [
                        ffprobe, "-v", "error", "-select_streams", "a",
                        "-count_packets", "-show_entries", "stream=nb_read_packets",
                        "-of", "csv=p=0", str(audio_file),
                    ],
                    failure_message="FFprobe failed",
                    cancel_requested=cancelled if parallel_probes else cancel_requested,
                    # Each chapter is part of the book, so the book length bounds it.
                    audio_seconds=audio_seconds,
                )
                lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
                if not lines:
                    raise ValueError(f"Missing audio packet count: {audio_file}")
                count = int(lines[0])
                if count < 0:
                    raise ValueError(f"Invalid audio packet count: {audio_file}")
                return count
            except BaseException:
                stopped.set()
                raise

        if not parallel_probes:
            return [count_packets(path) for path in audio_files]

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="chapter-probe") as executor:
            paths = iter(audio_files)
            pending = deque(executor.submit(count_packets, next(paths)) for _ in range(2))
            counts = []
            try:
                while pending:
                    counts.append(pending.popleft().result())
                    path = next(paths, None)
                    if path is not None:
                        pending.append(executor.submit(count_packets, path))
                return counts
            finally:
                stopped.set()
                for future in pending:
                    future.cancel()

    def _probe_concat_packet_starts(
        self,
        ffprobe_path: str,
        concat_path: Path,
        boundary_indices: set[int],
        cancel_requested: Callable[[], bool] | None = None,
    ) -> tuple[dict[int, float], int]:
        """Stream packet pts_time values, keeping only chapter-boundary timestamps.

        A whole-book packet probe emits one line per AAC packet (hundreds of
        thousands of lines), so parse the CSV stream incrementally instead of
        materializing a JSON document for every packet.
        """
        process = subprocess.Popen(
            [
                ffprobe_path, "-v", "error",
                "-select_streams", "a",
                "-show_entries", "packet=pts_time",
                "-of", "csv=p=0",
                str(concat_path),
            ],
            stdout=subprocess.PIPE,
            **background_process_kwargs(),
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        boundary_pts: dict[int, float] = {}
        read_state = {"packet_index": 0}
        read_error: list[BaseException] = []

        def consume_stdout() -> None:
            packet_index = 0
            try:
                if process.stdout is None:  # pragma: no cover - defensive guard
                    raise RuntimeError("FFprobe stdout pipe is unavailable")
                for line in process.stdout:
                    value = line.strip().rstrip(",")
                    if not value or value == "N/A":
                        continue
                    if packet_index in boundary_indices:
                        boundary_pts[packet_index] = float(value)
                    packet_index += 1
            except BaseException as exc:  # surfaced on the caller thread below
                read_error.append(exc)
            finally:
                read_state["packet_index"] = packet_index

        # Bound the streaming read like every other subprocess call in this
        # module: a stalled ffprobe would otherwise block `for line in stdout`
        # forever. Drain on a background thread and enforce the module timeout,
        # killing the process (which closes stdout and unwinds the reader) if it
        # exceeds the deadline.
        timeout = ffmpeg_timeout_seconds(audio_seconds=getattr(self, "_audio_seconds", None))
        reader = threading.Thread(target=consume_stdout, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        while reader.is_alive() and time.monotonic() < deadline:
            if cancel_requested is not None and cancel_requested():
                kill_ffmpeg_process(process)
                raise RuntimeError("FFprobe failed: cancelled")
            reader.join(timeout=0.1)
        if reader.is_alive():
            kill_ffmpeg_process(process)
            reader.join(timeout=timeout)
            raise RuntimeError(
                f"FFprobe failed: timed out after {timeout:g}s "
                "while probing concat packet timestamps"
            )
        if read_error:
            if process.poll() is None:
                kill_ffmpeg_process(process)
            raise read_error[0]
        return_code = wait_for_ffmpeg_process(
            process,
            failure_message="FFprobe failed",
            timeout_seconds=timeout,
            cancel_requested=cancel_requested,
        )
        if return_code != 0:
            raise RuntimeError("FFprobe failed while probing concat packet timestamps")
        return boundary_pts, read_state["packet_index"]

    def _write_ffmetadata(
        self,
        output_path: Path,
        chapters: list[ChapterMarker],
        metadata: AudiobookMetadata
    ) -> None:
        """Write FFmpeg metadata file with chapters."""
        lines = [";FFMETADATA1"]
        
        # Add metadata
        lines.append(f"title={_escape_ffmetadata_value(metadata.title)}")
        if str(metadata.author or "").strip():
            lines.append(f"artist={_escape_ffmetadata_value(metadata.author)}")
        lines.append(f"album={_escape_ffmetadata_value(metadata.title)}")
        lines.append(f"genre={_escape_ffmetadata_value(metadata.genre)}")

        if metadata.narrator:
            lines.append(f"composer={_escape_ffmetadata_value(metadata.narrator)}")
        if metadata.year:
            lines.append(f"date={_escape_ffmetadata_value(metadata.year)}")
        if metadata.comment:
            lines.append(f"comment={_escape_ffmetadata_value(metadata.comment)}")
        
        # Add chapters
        for chapter in chapters:
            lines.append("")
            lines.append("[CHAPTER]")
            lines.append("TIMEBASE=1/1000")
            lines.append(f"START={chapter.start_ms}")
            lines.append(f"END={chapter.end_ms}")
            lines.append(f"title={_escape_ffmetadata_value(chapter.title)}")

        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
