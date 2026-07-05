
import os
import pytest
import numpy as np
import subprocess
import sys
import json
import shutil
import time
import wave
from unittest.mock import MagicMock, patch, mock_open
from pathlib import Path

import modules.audio as audio_module

from modules.audio.encoder import AudioEncoder
from modules.audio.ffmpeg import (
    atomic_output_path,
    resolve_ffmpeg_binary,
    run_ffmpeg_command,
    wait_for_ffmpeg_process,
)
from modules.audio.packager import M4BPackager, AudiobookMetadata, ChapterMarker


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _successful_ffmpeg_run(cmd, **_kwargs):  # noqa: ANN001, ANN202
    output_path = Path(cmd[-1])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"audio")
    return MagicMock(returncode=0, stdout="", stderr="")


def _write_executable(path: Path, content: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def _clear_ffmpeg_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for env_var in (
        "AUDIOBOOK_FFMPEG_PATH",
        "FFMPEG_PATH",
        "AUDIOBOOK_FFPROBE_PATH",
        "FFPROBE_PATH",
    ):
        monkeypatch.delenv(env_var, raising=False)


class TestFFmpegBinaryDiscovery:
    def test_resolve_ffmpeg_binary_prefers_bundled_executable_bin_before_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _clear_ffmpeg_env(monkeypatch)
        app_root = tmp_path / "sidecar"
        bundled_ffmpeg = _write_executable(app_root / "bin" / "ffmpeg")
        path_ffmpeg = _write_executable(tmp_path / "path-bin" / "ffmpeg")

        monkeypatch.setattr(sys, "executable", str(app_root / "orpheus-desktop-backend"))
        monkeypatch.setattr(
            "modules.audio.ffmpeg.shutil.which",
            lambda name: str(path_ffmpeg) if name == "ffmpeg" else None,
        )

        assert resolve_ffmpeg_binary("ffmpeg") == str(bundled_ffmpeg)

    def test_resolve_ffmpeg_binary_checks_pyinstaller_meipass_bin_before_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _clear_ffmpeg_env(monkeypatch)
        bundled_ffprobe = _write_executable(tmp_path / "_MEI12345" / "bin" / "ffprobe")
        path_ffprobe = _write_executable(tmp_path / "path-bin" / "ffprobe")

        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_MEI12345"), raising=False)
        monkeypatch.setattr(
            "modules.audio.ffmpeg.shutil.which",
            lambda name: str(path_ffprobe) if name == "ffprobe" else None,
        )

        assert resolve_ffmpeg_binary("ffprobe") == str(bundled_ffprobe)

    def test_resolve_ffmpeg_binary_keeps_env_override_ahead_of_bundled_paths(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _clear_ffmpeg_env(monkeypatch)
        app_root = tmp_path / "sidecar"
        _write_executable(app_root / "bin" / "ffmpeg")
        configured_ffmpeg = _write_executable(tmp_path / "configured" / "ffmpeg")

        monkeypatch.setenv("AUDIOBOOK_FFMPEG_PATH", str(configured_ffmpeg))
        monkeypatch.setattr(sys, "executable", str(app_root / "orpheus-desktop-backend"))
        monkeypatch.setattr("modules.audio.ffmpeg.shutil.which", lambda _name: None)

        assert resolve_ffmpeg_binary("ffmpeg") == str(configured_ffmpeg)

    def test_run_ffmpeg_command_replaces_undecodable_subprocess_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured_kwargs = {}

        def fake_run(cmd, **kwargs):  # noqa: ANN001
            captured_kwargs.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)

        result = run_ffmpeg_command(["ffmpeg", "-version"], failure_message="probe failed")

        assert result.returncode == 0
        assert captured_kwargs["encoding"] == "utf-8"
        assert captured_kwargs["errors"] == "replace"
        assert captured_kwargs["text"] is True


class TestAudioEncoder:
    @patch("shutil.which")
    def test_ffmpeg_check(self, mock_which):
        mock_which.return_value = "/usr/bin/ffmpeg"
        encoder = AudioEncoder()
        assert encoder.sample_rate == 24000

    @patch("subprocess.Popen")
    @patch("shutil.which")
    @pytest.mark.skipif(sys.platform != "darwin", reason="Homebrew fallback is macOS-specific")
    def test_array_to_mp3_uses_homebrew_ffmpeg_when_path_lookup_misses_it(
        self,
        mock_which,
        mock_popen,
        tmp_path,
    ):
        mock_which.return_value = None
        process = MagicMock()
        process.stdin = MagicMock()
        process.stderr.read.return_value = b""
        mock_popen.return_value = process

        def fake_wait(timeout=None):  # noqa: ANN001, ANN202
            _ = timeout
            Path(mock_popen.call_args.args[0][-1]).write_bytes(b"mp3")
            return 0

        process.wait.side_effect = fake_wait

        def fake_is_file(path):
            return str(path) == "/opt/homebrew/bin/ffmpeg" or Path(path).exists()

        def fake_access(path, mode):
            return str(path) == "/opt/homebrew/bin/ffmpeg"

        with patch("pathlib.Path.is_file", fake_is_file), patch("os.access", fake_access):
            encoder = AudioEncoder()
            encoder.array_to_mp3(np.zeros(8, dtype=np.float32), tmp_path / "out.mp3")

        args = mock_popen.call_args.args[0]
        assert args[0] == "/opt/homebrew/bin/ffmpeg"
        process.stdin.write.assert_called()
        process.stdin.close.assert_called_once()

    @patch("subprocess.Popen")
    @patch("shutil.which")
    def test_array_to_mp3_streams_pcm_chunks_to_ffmpeg(self, mock_which, mock_popen, tmp_path):
        mock_which.return_value = "/usr/bin/ffmpeg"
        process = MagicMock()
        process.stdin = MagicMock()
        process.stderr.read.return_value = b""
        mock_popen.return_value = process

        def fake_wait(timeout=None):  # noqa: ANN001, ANN202
            _ = timeout
            Path(mock_popen.call_args.args[0][-1]).write_bytes(b"mp3")
            return 0

        process.wait.side_effect = fake_wait

        encoder = AudioEncoder()
        encoder.array_to_mp3(np.linspace(-1, 1, 200_000, dtype=np.float32), tmp_path / "out.mp3")

        assert process.stdin.write.call_count > 1
        assert all(isinstance(call.args[0], bytes) for call in process.stdin.write.call_args_list)

    @patch("subprocess.Popen")
    @patch("shutil.which")
    def test_array_to_mp3_times_out_blocked_stdin_write_and_cleans_temp(
        self,
        mock_which,
        mock_popen,
        tmp_path,
    ):
        mock_which.return_value = "/usr/bin/ffmpeg"
        output_path = tmp_path / "out.mp3"
        output_path.write_bytes(b"existing")

        class _BlockingStdin:
            def write(self, _data):  # noqa: ANN001, ANN201
                time.sleep(0.05)

            def close(self) -> None:
                return None

        process = MagicMock()
        process.stdin = _BlockingStdin()
        process.stderr.read.return_value = b""
        process.wait.return_value = 0
        process.poll.return_value = None
        mock_popen.return_value = process

        encoder = AudioEncoder()

        with patch("modules.audio.encoder.ffmpeg_timeout_seconds", return_value=0.001):
            with pytest.raises(RuntimeError, match="timed out"):
                encoder.array_to_mp3(np.ones(128, dtype=np.float32), output_path)

        process.kill.assert_called()
        assert output_path.read_bytes() == b"existing"
        temp_output = Path(mock_popen.call_args.args[0][-1])
        assert not temp_output.exists()
    
    @patch("subprocess.run")
    @patch("shutil.which")
    def test_wav_to_mp3(self, mock_which, mock_run):
        mock_which.return_value = "/usr/bin/ffmpeg"
        mock_run.side_effect = _successful_ffmpeg_run
        
        encoder = AudioEncoder()
        
        # We need to mock input path exists
        with patch("pathlib.Path.exists", return_value=True):
            encoder.wav_to_mp3("input.wav", "output.mp3")
            
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "/usr/bin/ffmpeg"
        assert "-b:a" in args
        assert "128k" in args
        assert "-af" not in args

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_wav_to_mp3_includes_optional_audio_filter(self, mock_which, mock_run):
        mock_which.return_value = "/usr/bin/ffmpeg"
        mock_run.side_effect = _successful_ffmpeg_run

        encoder = AudioEncoder()

        with patch("pathlib.Path.exists", return_value=True):
            encoder.wav_to_mp3("input.wav", "output.mp3", audio_filter="volume=3.0dB")

        args = mock_run.call_args[0][0]
        assert "-af" in args
        assert args[args.index("-af") + 1] == "volume=3.0dB"
        assert args.index("-af") < args.index("-c:a")

    @patch("shutil.which")
    def test_wav_to_mp3_timeout_preserves_existing_output_and_cleans_temp(self, mock_which, tmp_path):
        mock_which.return_value = "/usr/bin/ffmpeg"
        input_path = tmp_path / "input.wav"
        output_path = tmp_path / "output.mp3"
        input_path.write_bytes(b"wav")
        output_path.write_bytes(b"existing")
        temp_outputs: list[Path] = []

        def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN202
            assert kwargs["timeout"] > 0
            temp_output = Path(cmd[-1])
            temp_outputs.append(temp_output)
            temp_output.write_bytes(b"partial")
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

        encoder = AudioEncoder()

        with patch("subprocess.run", side_effect=fake_run):
            with pytest.raises(RuntimeError, match="timed out"):
                encoder.wav_to_mp3(input_path, output_path)

        assert output_path.read_bytes() == b"existing"
        assert temp_outputs
        assert not any(path.exists() for path in temp_outputs)

    def test_atomic_output_path_requires_temp_file(self, tmp_path):
        output_path = tmp_path / "missing-output.mp3"

        with pytest.raises(RuntimeError, match="did not create expected output"):
            with atomic_output_path(output_path) as temp_output_path:
                assert temp_output_path.parent == output_path.parent

        assert not output_path.exists()

    def test_wait_for_ffmpeg_process_bounds_wait_after_kill(self):
        class _HungProcess:
            def __init__(self) -> None:
                self.killed = False
                self.wait_timeouts = []

            def wait(self, timeout=None):  # noqa: ANN001, ANN201
                self.wait_timeouts.append(timeout)
                if len(self.wait_timeouts) == 1:
                    raise subprocess.TimeoutExpired(["ffmpeg"], timeout)
                return 0

            def kill(self) -> None:
                self.killed = True

        process = _HungProcess()

        with pytest.raises(RuntimeError, match="timed out"):
            wait_for_ffmpeg_process(
                process,
                failure_message="FFmpeg test failed",
                timeout_seconds=0.001,
            )

        assert process.killed is True
        assert process.wait_timeouts[1] is not None
        assert process.wait_timeouts[1] > 0

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_get_duration_supports_m4a(self, mock_which, mock_run, tmp_path):
        mock_which.side_effect = lambda name: f"/usr/bin/{name}"
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"format": {"duration": "12.34"}}',
            stderr="",
        )

        encoder = AudioEncoder()
        audio_file = tmp_path / "document.m4a"
        audio_file.write_bytes(b"fake m4a content")

        duration = encoder.get_duration(audio_file)

        assert duration == pytest.approx(12.34)


class TestDocumentAudioPackager:
    @patch("subprocess.run")
    @patch("shutil.which")
    def test_create_m4a(self, mock_which, mock_run, tmp_path):
        assert hasattr(audio_module, "DocumentAudioPackager")

        mock_which.return_value = "/usr/bin/ffmpeg"
        mock_run.side_effect = _successful_ffmpeg_run

        packager = audio_module.DocumentAudioPackager()
        metadata = AudiobookMetadata(title="Document", author="Author")

        first = tmp_path / "01_intro.mp3"
        second = tmp_path / "02_body.mp3"
        first.write_bytes(b"one")
        second.write_bytes(b"two")

        output = tmp_path / "Document.m4a"
        result = packager.create_m4a(
            mp3_files=[first, second],
            metadata=metadata,
            output_path=output,
        )

        assert result == output
        assert mock_run.call_count == 1
        args = mock_run.call_args[0][0]
        assert args[0] == "/usr/bin/ffmpeg"
        assert "aac" in args
        temp_output = Path(args[-1])
        assert temp_output.parent == output.parent
        assert temp_output.name.startswith(f".{output.name}.")
        assert temp_output.name.endswith(f".tmp{output.suffix}")

class TestM4BPackager:
    def test_chapter_marker_creation(self):
        marker = ChapterMarker("Chapter 1", 0, 10000)
        assert marker.start_seconds == 0.0
        assert marker.end_seconds == 10.0
        
    @patch("subprocess.run")
    @patch("shutil.which")
    def test_create_m4b(self, mock_which, mock_run):
        mock_which.return_value = "/usr/bin/ffmpeg"
        mock_run.side_effect = _successful_ffmpeg_run
        
        packager = M4BPackager()
        metadata = AudiobookMetadata(title="Test", author="Author")
        
        # Mocking file operations
        with patch("builtins.open", mock_open()), \
             patch("pathlib.Path.mkdir"), \
             patch("pathlib.Path.exists", return_value=True), \
             patch.object(packager, '_generate_chapter_markers', return_value=[]), \
             patch.object(packager, '_write_ffmetadata'):
             
             packager.create_m4b(["ch1.mp3"], ["Chapter 1"], metadata, "out.m4b")
             
        assert mock_run.call_count == 1

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_create_m4b_without_cover_uses_single_pass_metadata_mux(self, mock_which, mock_run, tmp_path):
        mock_which.return_value = "/usr/bin/ffmpeg"
        mock_run.side_effect = _successful_ffmpeg_run

        first = tmp_path / "one.wav"
        second = tmp_path / "two.wav"
        first.write_bytes(b"one")
        second.write_bytes(b"two")
        output = tmp_path / "book.m4b"

        packager = M4BPackager()
        metadata = AudiobookMetadata(title="Book", author="Author")

        packager.create_m4b(
            [first, second],
            ["One", "Two"],
            metadata,
            output,
            chapter_durations_ms=[1000, 1500],
        )

        assert mock_run.call_count == 1
        final_args = mock_run.call_args_list[0].args[0]

        assert final_args[0] == "/usr/bin/ffmpeg"
        concat_f_index = final_args.index("-f")
        assert final_args[concat_f_index + 1] == "concat"
        assert final_args[concat_f_index + 2] == "-safe"
        assert final_args[concat_f_index + 4] == "-i"
        assert final_args[final_args.index("-c:a") + 1] == "aac"
        assert final_args[final_args.index("-b:a") + 1] == "128k"
        assert final_args[final_args.index("-movflags") + 1] == "+faststart"
        ffmetadata_f_index = final_args.index("-f")
        ffmetadata_f_index = final_args.index("-f", ffmetadata_f_index + 1)
        assert final_args[ffmetadata_f_index + 1] == "ffmetadata"
        assert final_args[ffmetadata_f_index + 2] == "-i"
        assert final_args[final_args.index("-map_metadata") + 1] == "1"
        assert final_args[final_args.index("-map_chapters") + 1] == "1"
        temp_output = Path(final_args[-1])
        assert temp_output.parent == output.parent
        assert temp_output.name.startswith(f".{output.name}.")
        assert temp_output.name.endswith(f".tmp{output.suffix}")

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_create_m4b_uses_stream_copy_for_m4a_inputs(self, mock_which, mock_run, tmp_path):
        mock_which.return_value = "/usr/bin/ffmpeg"
        mock_run.side_effect = _successful_ffmpeg_run

        first = tmp_path / "one.m4a"
        second = tmp_path / "two.m4a"
        first.write_bytes(b"one")
        second.write_bytes(b"two")
        output = tmp_path / "book.m4b"

        packager = M4BPackager()
        metadata = AudiobookMetadata(title="Book", author="Author")

        packager.create_m4b(
            [first, second],
            ["One", "Two"],
            metadata,
            output,
            chapter_durations_ms=[1000, 1500],
        )

        final_args = mock_run.call_args_list[0].args[0]
        assert final_args[final_args.index("-c:a") + 1] == "copy"
        assert "-b:a" not in final_args
        ffmetadata_f_index = final_args.index("-f")
        ffmetadata_f_index = final_args.index("-f", ffmetadata_f_index + 1)
        assert final_args[ffmetadata_f_index + 1] == "ffmetadata"
        assert final_args[ffmetadata_f_index + 2] == "-i"
        assert final_args[final_args.index("-map_metadata") + 1] == "1"
        assert final_args[final_args.index("-map_chapters") + 1] == "1"
        temp_output = Path(final_args[-1])
        assert temp_output.parent == output.parent
        assert temp_output.name.startswith(f".{output.name}.")
        assert temp_output.name.endswith(f".tmp{output.suffix}")

    @patch("shutil.which")
    def test_create_m4b_failure_preserves_existing_output_and_cleans_temp(self, mock_which, tmp_path):
        mock_which.return_value = "/usr/bin/ffmpeg"
        audio_file = tmp_path / "one.wav"
        audio_file.write_bytes(b"one")
        output = tmp_path / "book.m4b"
        output.write_bytes(b"existing")
        temp_outputs: list[Path] = []

        def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN202
            assert kwargs["timeout"] > 0
            output_arg = Path(cmd[-1])
            temp_outputs.append(output_arg)
            output_arg.write_bytes(b"partial")
            return MagicMock(returncode=1, stderr="boom")

        packager = M4BPackager()
        metadata = AudiobookMetadata(title="Book", author="Author")

        with patch("subprocess.run", side_effect=fake_run):
            with pytest.raises(RuntimeError, match="boom"):
                packager.create_m4b(
                    [audio_file],
                    ["One"],
                    metadata,
                    output,
                    chapter_durations_ms=[1000],
                )

        assert output.read_bytes() == b"existing"
        assert temp_outputs
        assert not any(path.exists() for path in temp_outputs)

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_create_m4b_with_cover_keeps_attached_pic_mapping(self, mock_which, mock_run, tmp_path):
        mock_which.return_value = "/usr/bin/ffmpeg"
        mock_run.side_effect = _successful_ffmpeg_run

        audio_file = tmp_path / "one.wav"
        cover_file = tmp_path / "cover.jpg"
        audio_file.write_bytes(b"one")
        cover_file.write_bytes(b"cover")

        packager = M4BPackager()
        metadata = AudiobookMetadata(title="Book", author="Author", cover_path=cover_file)

        with patch.object(packager, "_generate_chapter_markers", return_value=[]), \
             patch.object(packager, "_write_ffmetadata"):
            packager.create_m4b([audio_file], ["One"], metadata, tmp_path / "book.m4b")

        assert mock_run.call_count == 1
        final_args = mock_run.call_args_list[-1].args[0]
        assert "-disposition:v" in final_args
        assert final_args[final_args.index("-disposition:v") + 1] == "attached_pic"
        assert "-map" in final_args
        assert "2:v" in [final_args[index + 1] for index, value in enumerate(final_args[:-1]) if value == "-map"]

    @patch("shutil.which")
    def test_generate_chapter_markers_uses_supplied_durations_without_probe(self, mock_which):
        mock_which.return_value = "/usr/bin/ffmpeg"
        packager = M4BPackager()

        with patch(
            "modules.audio.encoder.AudioEncoder.get_duration",
            side_effect=AssertionError("unexpected ffprobe duration call"),
        ):
            markers = packager._generate_chapter_markers(
                [Path("one.mp3"), Path("two.mp3")],
                ["One", "Two"],
                chapter_durations_ms=[1000, 2500],
            )

        assert len(markers) == 2
        assert markers[0].start_ms == 0
        assert markers[0].end_ms == 1000
        assert markers[1].start_ms == 1000
        assert markers[1].end_ms == 3500

    @patch("subprocess.run")
    @patch("shutil.which")
    @pytest.mark.skipif(os.name == "nt", reason="backslash is a path separator on Windows")
    def test_create_m4b_escapes_concat_paths_for_ffmpeg(self, mock_which, mock_run, tmp_path):
        mock_which.return_value = "/usr/bin/ffmpeg"

        audio_file = tmp_path / "chapter one's \\ intro.mp3"
        audio_file.write_bytes(b"mp3")
        packager = M4BPackager()
        metadata = AudiobookMetadata(title="Test", author="Author")
        concat_contents: dict[str, str] = {}

        def capture_concat_file(cmd, **_kwargs):  # noqa: ANN001
            if "-f" in cmd and cmd[cmd.index("-f") + 1] == "concat":
                concat_contents["text"] = Path(cmd[cmd.index("-i") + 1]).read_text(encoding="utf-8")
            output_path = Path(cmd[-1])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"audio")
            return MagicMock(returncode=0, stderr="")

        mock_run.side_effect = capture_concat_file

        with patch.object(packager, "_generate_chapter_markers", return_value=[]), \
             patch.object(packager, "_write_ffmetadata"):
            packager.create_m4b([audio_file], ["Intro"], metadata, tmp_path / "out.m4b")

        escaped_path = str(audio_file.absolute()).replace("\\", "\\\\").replace("'", "'\\''")
        assert concat_contents["text"] == f"file '{escaped_path}'\n"

    @patch("shutil.which")
    def test_ffmetadata_values_are_escaped(self, mock_which, tmp_path):
        mock_which.return_value = "/usr/bin/ffmpeg"
        packager = M4BPackager()
        metadata = AudiobookMetadata(
            title="A = title; #1",
            author="Author\\Name",
            comment="line one\nline two",
        )
        output_path = tmp_path / "chapters.txt"

        packager._write_ffmetadata(
            output_path,
            [ChapterMarker("Chapter = One # intro", 0, 1000)],
            metadata,
        )

        text = output_path.read_text(encoding="utf-8")
        assert "title=A \\= title\\; \\#1" in text
        assert "artist=Author\\\\Name" in text
        assert "comment=line one\\nline two" in text
        assert "title=Chapter \\= One \\# intro" in text
        assert "comment=line one\nline two" not in text

    def test_create_m4b_without_cover_real_ffmpeg_preserves_aac_metadata_and_chapters(self, tmp_path):
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        assert ffmpeg, "ffmpeg is required for packaging compatibility coverage"
        assert ffprobe, "ffprobe is required for packaging compatibility coverage"

        def write_wav(path: Path, frequency: float, duration_seconds: float) -> None:
            sample_rate = 24000
            t = np.arange(int(sample_rate * duration_seconds), dtype=np.float32) / sample_rate
            samples = (np.sin(2 * np.pi * frequency * t) * 0.25 * 32767).astype(np.int16)
            with wave.open(str(path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(sample_rate)
                handle.writeframes(samples.tobytes())

        first = tmp_path / "chapter_01.wav"
        second = tmp_path / "chapter_02.wav"
        write_wav(first, 440.0, 0.30)
        write_wav(second, 660.0, 0.40)

        output = tmp_path / "synthetic.m4b"
        packager = M4BPackager()
        metadata = AudiobookMetadata(
            title="Synthetic Book",
            author="Test Author",
            narrator="Test Narrator",
            year="2026",
            genre="Audiobook",
            comment="compatibility smoke",
        )

        packager.create_m4b(
            [first, second],
            ["Opening", "Second Part"],
            metadata,
            output,
            chapter_durations_ms=[300, 400],
        )

        probe = subprocess.run(
            [
                ffprobe,
                "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                "-show_chapters",
                str(output),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        data = json.loads(probe.stdout)

        assert output.exists()
        assert "mov,mp4,m4a,3gp,3g2,mj2" in data["format"]["format_name"]
        audio_streams = [stream for stream in data["streams"] if stream.get("codec_type") == "audio"]
        assert len(audio_streams) == 1
        assert audio_streams[0]["codec_name"] == "aac"
        assert audio_streams[0].get("sample_rate") == "24000"

        tags = {key.lower(): value for key, value in data["format"].get("tags", {}).items()}
        assert tags["title"] == "Synthetic Book"
        assert tags["artist"] == "Test Author"
        assert tags["album"] == "Synthetic Book"
        assert tags["genre"] == "Audiobook"
        assert tags["composer"] == "Test Narrator"
        assert tags["date"] == "2026"
        assert tags["comment"] == "compatibility smoke"

        chapters = data["chapters"]
        assert len(chapters) == 2
        assert [chapter["tags"]["title"] for chapter in chapters] == ["Opening", "Second Part"]
        assert float(chapters[0]["start_time"]) == pytest.approx(0.0, abs=0.03)
        assert float(chapters[0]["end_time"]) == pytest.approx(0.3, abs=0.05)
        assert float(chapters[1]["start_time"]) == pytest.approx(0.3, abs=0.05)
        assert float(chapters[1]["end_time"]) == pytest.approx(0.7, abs=0.08)


def test_importing_modules_audio_does_not_eagerly_load_submodules():
    code = """
import sys
import modules.audio
print("modules.audio.encoder" in sys.modules, "modules.audio.packager" in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        check=True,
        timeout=30,
    )

    assert result.stdout.strip() == "False False"


def test_importing_packager_module_does_not_import_pydub():
    code = """
import sys
from modules.audio.packager import M4BPackager
_ = M4BPackager
print("pydub" in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        check=True,
        timeout=30,
    )

    assert result.stdout.strip() == "False"
