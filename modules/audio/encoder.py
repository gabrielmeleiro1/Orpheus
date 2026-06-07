"""
Audio Encoder Module
====================
FFmpeg-based audio encoding for WAV to MP3 conversion.
Optimized for audiobook production with hardware acceleration.
"""

from __future__ import annotations

import contextlib
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional
import json
import numpy as np

from .ffmpeg import (
    atomic_output_path,
    ffmpeg_timeout_seconds,
    kill_ffmpeg_process,
    require_ffmpeg_binary,
    run_ffmpeg_command,
    wait_for_ffmpeg_process,
)


class AudioEncoder:
    """
    FFmpeg-based audio encoder for audiobook production.
    
    Converts WAV files to MP3 with configurable bitrate.
    Optimized for Apple Silicon with hardware acceleration.
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
        input_path = Path(input_path)
        output_path = Path(output_path)
        bitrate = bitrate or self.bitrate
        
        if not input_path.exists():
            raise FileNotFoundError(f"Input file not found: {input_path}")
        
        # Ensure output directory exists
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
                "-c:a", "libmp3lame",
                "-b:a", bitrate,
                "-ar", str(self.sample_rate),
                "-ac", "1",  # Mono
                "-q:a", "2",  # High quality
                str(temp_output_path),
            ])

            run_ffmpeg_command(cmd, failure_message="FFmpeg encoding failed")
        
        return output_path

    def array_to_mp3(
        self,
        audio: np.ndarray,
        output_path: Path | str,
        sample_rate: Optional[int] = None,
        bitrate: Optional[str] = None,
    ) -> Path:
        """
        Convert in-memory mono float audio directly to MP3 via ffmpeg stdin.

        Args:
            audio: Audio array (float, range roughly -1..1)
            output_path: Destination MP3 path
            sample_rate: Input sample rate override
            bitrate: MP3 bitrate override
        """
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
                "-c:a", "libmp3lame",
                "-b:a", bitrate,
                "-ar", str(self.sample_rate),
                "-ac", "1",
                "-q:a", "2",
                str(temp_output_path),
            ]

            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            try:
                if process.stdin is None:  # pragma: no cover - defensive guard
                    raise RuntimeError("FFmpeg stdin pipe is unavailable")
                timeout = ffmpeg_timeout_seconds()
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

                writer = threading.Thread(target=write_pcm, daemon=True)
                writer.start()
                writer.join(timeout=timeout)
                if writer.is_alive():
                    kill_ffmpeg_process(process)
                    raise RuntimeError(
                        f"FFmpeg encoding from array failed: timed out after {timeout:g}s"
                    )
                if write_error:
                    raise write_error[0]

                remaining_timeout = max(0.001, deadline - time.monotonic())
                return_code = wait_for_ffmpeg_process(
                    process,
                    failure_message="FFmpeg encoding from array failed",
                    timeout_seconds=remaining_timeout,
                )
                stderr_bytes = process.stderr.read() if process.stderr is not None else b""
            except Exception:
                if process.poll() is None:
                    kill_ffmpeg_process(process)
                raise

            if return_code != 0:
                stderr = stderr_bytes.decode("utf-8", errors="replace")
                raise RuntimeError(f"FFmpeg encoding from array failed: {stderr}")
        return output_path
    
    def get_duration(self, file_path: Path | str) -> float:
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
        
        result = run_ffmpeg_command(cmd, failure_message="FFprobe failed")
        
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    
    def get_duration_formatted(self, file_path: Path | str) -> str:
        """
        Get audio duration as formatted string (HH:MM:SS).
        
        Args:
            file_path: Path to audio file
            
        Returns:
            Duration as formatted string
        """
        duration_sec = self.get_duration(file_path)
        hours = int(duration_sec // 3600)
        minutes = int((duration_sec % 3600) // 60)
        seconds = int(duration_sec % 60)
        
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"
    
    def batch_encode(
        self,
        input_files: list[Path | str],
        output_dir: Path | str,
        bitrate: Optional[str] = None
    ) -> list[Path]:
        """
        Encode multiple WAV files to MP3.
        
        Args:
            input_files: List of input WAV file paths
            output_dir: Output directory for MP3 files
            bitrate: Override default bitrate
            
        Returns:
            List of paths to generated MP3 files
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        
        output_files = []
        for input_file in input_files:
            input_path = Path(input_file)
            output_path = output_dir / input_path.with_suffix(".mp3").name
            
            self.wav_to_mp3(input_path, output_path, bitrate)
            output_files.append(output_path)
        
        return output_files
    
    def check_hardware_acceleration(self) -> dict[str, bool]:
        """
        Check available hardware acceleration options.
        
        Returns:
            Dict of acceleration options and availability
        """
        result = run_ffmpeg_command(
            [self.ffmpeg_path, "-hwaccels"],
            failure_message="FFmpeg hardware acceleration probe failed",
        )
        
        output = result.stdout + result.stderr
        
        return {
            "videotoolbox": "videotoolbox" in output.lower(),
            "cuda": "cuda" in output.lower(),
            "vaapi": "vaapi" in output.lower(),
        }


def test_encoder() -> bool:
    """
    Quick test of AudioEncoder functionality.
    
    Returns:
        True if test passes
    """
    import tempfile
    import numpy as np
    
    try:
        encoder = AudioEncoder()
        
        # Check FFmpeg is available
        accel = encoder.check_hardware_acceleration()
        print(f"Hardware acceleration: {accel}")
        
        # Create a test WAV file
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            
            # Create simple test WAV using numpy and wave
            import wave
            
            test_wav = temp_path / "test.wav"
            test_mp3 = temp_path / "test.mp3"
            
            # Generate 1 second of 440Hz sine wave
            sample_rate = 24000
            duration = 1.0
            t = np.linspace(0, duration, int(sample_rate * duration))
            audio = (np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16)
            
            with wave.open(str(test_wav), 'w') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(audio.tobytes())
            
            # Test encoding
            result = encoder.wav_to_mp3(test_wav, test_mp3)
            assert result.exists(), "MP3 file not created"
            
            # Test duration
            duration = encoder.get_duration(test_mp3)
            assert 0.9 < duration < 1.1, f"Unexpected duration: {duration}"
            
            print(f"Encoded test.mp3: {encoder.get_duration_formatted(test_mp3)}")
            print("AudioEncoder test passed!")
            return True
        
    except Exception as e:
        print(f"AudioEncoder test failed: {e}")
        return False


if __name__ == "__main__":
    test_encoder()
