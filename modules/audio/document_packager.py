"""Document audio packager.

Concatenates ordered section audio into a single M4A file.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from .ffmpeg import atomic_output_path, require_ffmpeg_binary, run_ffmpeg_command
from .packager import AudiobookMetadata, write_ffmpeg_concat_file


class DocumentAudioPackager:
    """Create a single M4A file from ordered MP3 section audio."""

    def __init__(self):
        self.ffmpeg_path = require_ffmpeg_binary("ffmpeg")

    def create_m4a(
        self,
        mp3_files: list[Path | str],
        metadata: AudiobookMetadata,
        output_path: Path | str,
        bitrate: str = "128k",
    ) -> Path:
        if not mp3_files:
            raise ValueError("mp3_files must not be empty")

        audio_files = [Path(file_path) for file_path in mp3_files]
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        for audio_file in audio_files:
            if not audio_file.exists():
                raise FileNotFoundError(f"Section audio file not found: {audio_file}")

        with tempfile.TemporaryDirectory() as temp_dir:
            concat_file = Path(temp_dir) / "concat.txt"
            write_ffmpeg_concat_file(concat_file, audio_files)

            command = [
                self.ffmpeg_path,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-vn",
                # document_audio is one continuous narration split into sections
                # with no natural pauses. Stream-copying independently encoded AAC
                # sections would re-bake each segment's encoder priming/padding at
                # every seam, inserting audible mid-narration gaps/clicks. Always
                # single-pass re-encode so the joined audio stays gapless.
                "-c:a",
                "aac",
                "-b:a",
                bitrate,
                "-movflags",
                "+faststart",
                "-metadata",
                f"title={metadata.title}",
                "-metadata",
                f"artist={metadata.author}",
                "-metadata",
                f"album={metadata.title}",
                "-metadata",
                f"genre={metadata.genre}",
            ]

            if metadata.narrator:
                command.extend(["-metadata", f"composer={metadata.narrator}"])
            if metadata.year:
                command.extend(["-metadata", f"date={metadata.year}"])
            if metadata.comment:
                command.extend(["-metadata", f"comment={metadata.comment}"])

            with atomic_output_path(output_path) as temp_output_path:
                command.append(str(temp_output_path))
                run_ffmpeg_command(command, failure_message="M4A creation failed")

        return output_path
