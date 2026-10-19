"""
Audio Encoder Module
====================
FFmpeg-based audio encoding for audiobook production (MP3 and AAC/M4A).
"""

from __future__ import annotations

import contextlib
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional
import json
import numpy as np

from ..subprocess_utils import background_process_kwargs

from .ffmpeg import (
    atomic_output_path,
    ffmpeg_failure,
    ffmpeg_timeout_seconds,
    kill_ffmpeg_process,
    require_ffmpeg_binary,
    run_ffmpeg_command,
    wait_for_ffmpeg_process,
    wav_duration_seconds,
)


class AudioEncoder:
    """
    FFmpeg-based audio encoder for audiobook production.

    Converts WAV files or in-memory PCM to MP3/AAC with configurable bitrate.
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

    def array_to_mp3(
        self,
        audio: np.ndarray,
        output_path: Path | str,
        sample_rate: Optional[int] = None,
        bitrate: Optional[str] = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> Path:
        """
        Convert in-memory mono float audio directly to MP3 via ffmpeg stdin.

        Args:
            audio: Audio array (float, range roughly -1..1)
            output_path: Destination MP3 path
            sample_rate: Input sample rate override
            bitrate: MP3 bitrate override
        """
        return self._array_to_encoded(
            audio,
            output_path,
            sample_rate=sample_rate,
            bitrate=bitrate,
            cancel_requested=cancel_requested,
            codec="libmp3lame",
            failure_message="FFmpeg encoding from array failed",
        )

    def _array_to_encoded(
        self,
        audio: np.ndarray,
        output_path: Path | str,
        *,
        sample_rate: Optional[int],
        bitrate: Optional[str],
        codec: str,
        failure_message: str,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        in_sample_rate = int(sample_rate or self.sample_rate)
        bitrate = bitrate or self.bitrate

        pcm = np.asarray(audio, dtype=np.float32).reshape(-1)

        with atomic_output_path(output_path) as temp_output_path:
            cmd = [
                self.ffmpeg_path,
                "-y",
                "-v",
                "error",
                "-f", "s16le",
                "-ar", str(in_sample_rate),
                "-ac", "1",
                "-i", "pipe:0",
                "-c:a", codec,
                "-b:a", bitrate,
                "-ar", str(self.sample_rate),
                "-ac", "1",
                str(temp_output_path),
            ]

            process = subprocess.Popen(
                cmd,
                **background_process_kwargs(),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                if process.stdin is None:  # pragma: no cover - defensive guard
                    raise RuntimeError("FFmpeg stdin pipe is unavailable")
                timeout = ffmpeg_timeout_seconds(audio_seconds=len(pcm) / in_sample_rate)
                deadline = time.monotonic() + timeout
                write_error: list[BaseException] = []

                def write_pcm() -> None:
                    try:
                        for start in range(0, len(pcm), 65_536):
                            chunk = pcm[start : start + 65_536]
                            pcm16 = (
                                np.clip(chunk, -1.0, 1.0) * 32767.0
                            ).astype(np.int16, copy=False)
                            process.stdin.write(pcm16.tobytes())
                    except BaseException as exc:  # pragma: no cover - surfaced below
                        write_error.append(exc)
                    finally:
                        with contextlib.suppress(Exception):
                            process.stdin.close()

                stderr_chunks: list[bytes] = []

                def read_stderr() -> None:
                    if process.stderr is None:
                        return
                    with contextlib.suppress(Exception):
                        stderr_chunks.append(process.stderr.read())

                writer = threading.Thread(target=write_pcm, daemon=True)
                # Drain stderr on its own thread. ffmpeg can emit more than the
                # ~64KB stderr pipe buffer holds; reading it only after wait()
                # lets a chatty ffmpeg block on a full pipe and deadlocks the
                # encode until the timeout.
                reader = threading.Thread(target=read_stderr, daemon=True)
                writer.start()
                reader.start()
                while writer.is_alive() and time.monotonic() < deadline:
                    if cancel_requested is not None and cancel_requested():
                        kill_ffmpeg_process(process)
                        raise RuntimeError(f"{failure_message}: cancelled")
                    writer.join(timeout=min(0.1, max(0.001, deadline - time.monotonic())))
                if writer.is_alive():
                    kill_ffmpeg_process(process)
                    raise RuntimeError(
                        f"{failure_message}: timed out after {timeout:g}s"
                    )

                # A failed stdin write is usually a symptom (ffmpeg died from a
                # real error such as a full disk). Wait for the process and read
                # stderr first so the root cause is reported, not "broken pipe".
                remaining_timeout = max(0.001, deadline - time.monotonic())
                return_code = wait_for_ffmpeg_process(
                    process,
                    failure_message=failure_message,
                    timeout_seconds=remaining_timeout,
                    cancel_requested=cancel_requested,
                )
                # The process has exited, so stderr is at EOF and the reader
                # finishes promptly; bound the join defensively regardless.
                reader.join(timeout=remaining_timeout)
                stderr_bytes = b"".join(stderr_chunks)
            except Exception:
                if process.poll() is None:
                    kill_ffmpeg_process(process)
                raise

            if return_code != 0:
                stderr = stderr_bytes.decode("utf-8", errors="replace")
                error = ffmpeg_failure(failure_message, stderr)
                if write_error:
                    raise error from write_error[0]
                raise error
            if write_error:
                raise write_error[0]
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

    def array_to_aac(
        self,
        audio: np.ndarray,
        output_path: Path | str,
        sample_rate: Optional[int] = None,
        bitrate: Optional[str] = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> Path:
        """Convert in-memory mono float audio directly to AAC/M4A via ffmpeg stdin."""
        return self._array_to_encoded(
            audio,
            output_path,
            sample_rate=sample_rate,
            bitrate=bitrate,
            cancel_requested=cancel_requested,
            codec="aac",
            failure_message="FFmpeg AAC encoding from array failed",
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
