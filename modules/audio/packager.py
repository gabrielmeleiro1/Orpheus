"""
M4B Packager Module
===================
Creates M4B audiobooks with chapter markers using FFmpeg.
Optimized for Apple Books and other audiobook players.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional
from dataclasses import dataclass
import json
import logging
import subprocess
import tempfile

from .ffmpeg import (
    atomic_output_path,
    ffmpeg_timeout_seconds,
    kill_ffmpeg_process,
    require_ffmpeg_binary,
    run_ffmpeg_command,
    wait_for_ffmpeg_process,
)

logger = logging.getLogger(__name__)


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


def audio_files_support_stream_copy(audio_files: list[Path]) -> bool:
    """Return True when chapter inputs can be concatenated with AAC stream copy."""
    if not audio_files:
        return False
    return all(path.suffix.lower() == ".m4a" for path in audio_files)


def _escape_ffmetadata_value(value: str) -> str:
    """Escape FFmetadata scalar values so titles cannot break key/value lines."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
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
    
    @property
    def start_seconds(self) -> float:
        return self.start_ms / 1000.0
    
    @property
    def end_seconds(self) -> float:
        return self.end_ms / 1000.0


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
    
    def create_m4b(
        self,
        mp3_files: list[Path | str],
        chapter_names: list[str],
        metadata: AudiobookMetadata,
        output_path: Path | str,
        chapter_durations_ms: Optional[list[int]] = None,
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
        if len(mp3_files) != len(chapter_names):
            raise ValueError("mp3_files and chapter_names must have same length")
        if chapter_durations_ms is not None and len(chapter_durations_ms) != len(mp3_files):
            raise ValueError("chapter_durations_ms must have same length as mp3_files")
        
        audio_files = [Path(f) for f in mp3_files]
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
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
                run_ffmpeg_command(concat_cmd, failure_message="M4B stream-copy concat failed")

                chapters = self._stream_copy_chapter_markers(
                    stream_copy_audio,
                    audio_files,
                    chapter_names,
                    fallback_durations_ms=chapter_durations_ms,
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
                run_ffmpeg_command(final_cmd, failure_message="M4B creation failed")

        return output_path

    def _generate_chapter_markers(
        self,
        audio_files: list[Path],
        chapter_names: list[str],
        chapter_durations_ms: Optional[list[int]] = None,
    ) -> list[ChapterMarker]:
        """Generate chapter markers from audio file durations."""
        chapters = []
        current_ms = 0

        if chapter_durations_ms is not None:
            durations = [max(0, int(ms)) for ms in chapter_durations_ms]
            for chapter_name, duration_ms in zip(chapter_names, durations):
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
            try:
                duration_sec = encoder.get_duration(audio_file)
            except (RuntimeError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                logger.warning(
                    "Could not probe duration of %s (%s); marking chapter %r with a "
                    "zero-length span so packaging can continue",
                    audio_file,
                    exc,
                    chapter_name,
                )
                duration_sec = 0.0
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
    ) -> list[ChapterMarker]:
        """Derive chapter markers from a stream-copied AAC concat's real timeline.

        Stream-copied segments contribute their encoder priming/padding to the
        joined presentation timeline, so per-source-WAV durations drift from where
        chapters actually begin. Read the true per-chapter start offsets from the
        encoded concat instead. Falls back to source-derived markers if the
        timeline cannot be probed, so packaging never fails on probe issues.
        """
        offsets = self._probe_stream_copy_offsets_ms(concat_path, audio_files)
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

            segment_packet_counts: list[int] = []
            for audio_file in audio_files:
                count_result = run_ffmpeg_command(
                    [
                        ffprobe, "-v", "error",
                        "-select_streams", "a",
                        "-count_packets",
                        "-show_entries", "stream=nb_read_packets",
                        "-of", "csv=p=0",
                        str(audio_file),
                    ],
                    failure_message="FFprobe failed",
                )
                count_lines = [
                    line.strip() for line in count_result.stdout.splitlines() if line.strip()
                ]
                if not count_lines:
                    return None
                segment_packet_counts.append(int(count_lines[0]))

            boundary_indices: set[int] = set()
            boundary_index = 0
            for count in segment_packet_counts:
                boundary_indices.add(boundary_index)
                boundary_index += count

            boundary_pts, packet_count = self._probe_concat_packet_starts(
                ffprobe, concat_path, boundary_indices
            )
            if packet_count == 0 or len(boundary_pts) != len(boundary_indices):
                return None

            duration_result = run_ffmpeg_command(
                [
                    ffprobe, "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "csv=p=0",
                    str(concat_path),
                ],
                failure_message="FFprobe failed",
            )
            total_ms = int(round(float(duration_result.stdout.strip()) * 1000))

            starts_ms = [
                int(round(boundary_pts[index] * 1000)) for index in sorted(boundary_pts)
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
            logger.warning(
                "Stream-copy chapter timeline probe failed for %s: %s", concat_path, exc
            )
            return None

    def _probe_concat_packet_starts(
        self,
        ffprobe_path: str,
        concat_path: Path,
        boundary_indices: set[int],
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
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        boundary_pts: dict[int, float] = {}
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
        except Exception:
            if process.poll() is None:
                kill_ffmpeg_process(process)
            raise
        return_code = wait_for_ffmpeg_process(
            process,
            failure_message="FFprobe failed",
            timeout_seconds=ffmpeg_timeout_seconds(),
        )
        if return_code != 0:
            raise RuntimeError("FFprobe failed while probing concat packet timestamps")
        return boundary_pts, packet_index

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
    
    def get_chapters(self, m4b_path: Path | str) -> list[ChapterMarker]:
        """
        Extract chapter markers from existing M4B file.
        
        Args:
            m4b_path: Path to M4B file
            
        Returns:
            List of ChapterMarker objects
        """
        m4b_path = Path(m4b_path)
        
        if not m4b_path.exists():
            raise FileNotFoundError(f"M4B file not found: {m4b_path}")
        
        cmd = [
            require_ffmpeg_binary("ffprobe"),
            "-v", "quiet",
            "-print_format", "json",
            "-show_chapters",
            str(m4b_path)
        ]
        
        result = run_ffmpeg_command(cmd, failure_message="FFprobe failed")
        
        data = json.loads(result.stdout)
        chapters = []
        
        for ch in data.get("chapters", []):
            chapters.append(ChapterMarker(
                title=ch.get("tags", {}).get("title", "Chapter"),
                start_ms=int(float(ch["start_time"]) * 1000),
                end_ms=int(float(ch["end_time"]) * 1000)
            ))
        
        return chapters
