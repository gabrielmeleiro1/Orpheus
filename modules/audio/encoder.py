"""
Audio Encoder Module
====================
FFmpeg-based audio encoding for audiobook production (MP3 and AAC/M4A).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional
import json

from .ffmpeg import (
    atomic_output_path,
    require_ffmpeg_binary,
    run_ffmpeg_command,
    wav_duration_seconds,
)


class AudioEncoder:
    """
    FFmpeg-based audio encoder for audiobook production.

    Converts WAV files to MP3/AAC with configurable bitrate.
    """
    
    def __init__(self, bitrate: str = "128k", sample_rate: int = 24000):
        """
        Initialize the audio encoder.
        
        Args:
            bitrate: Target MP3 bitrate (default '128k')
            sample_rate: Audio sample rate in Hz (default 24000)
        """
        self.ffmpeg_path = require_ffmpeg_binary("ffmpeg")
        self.bitrate = bitrate
        self.sample_rate = sample_rate
    
    def wav_to_mp3(
        self,
        input_path: Path | str,
        output_path: Path | str,
        bitrate: Optional[str] = None,
        audio_filter: Optional[str] = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> Path:
        """
        Convert WAV file to MP3.
        
        Args:
            input_path: Path to input WAV file
            output_path: Path for output MP3 file
            bitrate: Override default bitrate (e.g., '128k', '192k')
            audio_filter: Optional FFmpeg audio filter expression
            
        Returns:
            Path to generated MP3 file
        """
        return self._wav_to_encoded(
            input_path,
            output_path,
            bitrate=bitrate,
            audio_filter=audio_filter,
            cancel_requested=cancel_requested,
            codec="libmp3lame",
            failure_message="FFmpeg encoding failed",
        )

    def _wav_to_encoded(
        self,
        input_path: Path | str,
        output_path: Path | str,
        *,
        bitrate: Optional[str],
        audio_filter: Optional[str],
        codec: str,
        failure_message: str,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> Path:
        input_path = Path(input_path)
        output_path = Path(output_path)
        bitrate = bitrate or self.bitrate

        if not input_path.exists():
            raise FileNotFoundError(f"Input file not found: {input_path}")

        output_path.parent.mkdir(parents=True, exist_ok=True)

        with atomic_output_path(output_path) as temp_output_path:
            cmd = [
                self.ffmpeg_path,
                "-y",  # Overwrite temp output
                "-i", str(input_path),
            ]
            if audio_filter:
                cmd.extend(["-af", audio_filter])
            cmd.extend([
                "-c:a", codec,
                "-b:a", bitrate,
                "-ar", str(self.sample_rate),
                "-ac", "1",  # Mono
                str(temp_output_path),
            ])

            run_ffmpeg_command(
                cmd,
                failure_message=failure_message,
                cancel_requested=cancel_requested,
                audio_seconds=wav_duration_seconds(input_path),
            )

        return output_path

    def wav_to_aac(
        self,
        input_path: Path | str,
        output_path: Path | str,
        bitrate: Optional[str] = None,
        audio_filter: Optional[str] = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> Path:
        """Convert WAV file to AAC/M4A."""
        return self._wav_to_encoded(
            input_path,
            output_path,
            bitrate=bitrate,
            audio_filter=audio_filter,
            cancel_requested=cancel_requested,
            codec="aac",
            failure_message="FFmpeg AAC encoding failed",
        )

    def get_duration(
        self, file_path: Path | str, *, cancel_requested: Callable[[], bool] | None = None,
    ) -> float:
        """
        Get audio file duration in seconds.
        
        Uses ffprobe for accurate duration measurement.
        
        Args:
            file_path: Path to audio file
            
        Returns:
            Duration in seconds
        """
        file_path = Path(file_path)
        
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        
        ffprobe_path = require_ffmpeg_binary("ffprobe")
        
        cmd = [
            ffprobe_path,
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            str(file_path)
        ]
        
        # Reading the container header takes no time proportional to its length.
        result = run_ffmpeg_command(
            cmd, failure_message="FFprobe failed", cancel_requested=cancel_requested,
            audio_seconds=0.0,
        )
        
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
