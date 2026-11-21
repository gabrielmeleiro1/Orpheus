
import os
import pytest
import numpy as np
import subprocess
import sys
import json
import re
import shutil
import time
import wave
from unittest.mock import MagicMock, patch
from pathlib import Path

import modules.audio as audio_module

from modules.audio.encoder import AudioEncoder
from modules.audio.ffmpeg import (
    atomic_output_path,
    resolve_ffmpeg_binary,
    run_ffmpeg_command,
    wait_for_ffmpeg_process,
)
from modules.audio.packager import (
    M4BPackager,
    AudiobookMetadata,
    ChapterMarker,
    quote_ffmpeg_concat_path,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_run_ffmpeg_command_cancellation_kills_child_and_raises(monkeypatch):
    class BlockingProcess:
        def __init__(self, *_args, **_kwargs):
            self.returncode = None
            self.killed = False

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            if self.returncode is None:
                raise subprocess.TimeoutExpired("ffmpeg", timeout)
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -9

        def communicate(self):
            return "", ""

    process = BlockingProcess()
    monkeypatch.setattr("modules.audio.ffmpeg.subprocess.Popen", lambda *_args, **_kwargs: process)

    with pytest.raises(RuntimeError, match="cancelled"):
        run_ffmpeg_command(
            ["ffmpeg", "-i", "in.wav", "out.mp3"],
            failure_message="FFmpeg encoding failed",
            cancel_requested=lambda: True,
        )

    assert process.killed is True


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


def _write_sine_wav(
    path: Path,
    frequency: float,
    duration_seconds: float,
    sample_rate: int = 24000,
    phase_samples: int = 0,
) -> int:
    """Write a mono 16-bit sine WAV; return its sample count.

    ``phase_samples`` lets callers chain sections into one phase-continuous tone,
    which mimics continuous document prose split across sections.
    """
    frame_count = int(sample_rate * duration_seconds)
    t = (np.arange(frame_count, dtype=np.float64) + phase_samples) / sample_rate
    samples = (np.sin(2 * np.pi * frequency * t) * 0.25 * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(samples.tobytes())
    return frame_count


def _decoded_duration_ms(path: Path, sample_rate: int = 24000) -> float:
    """Decoded PCM length in ms (the real, priming-inclusive audible timeline)."""
    ffmpeg = shutil.which("ffmpeg")
    pcm = subprocess.run(
        [ffmpeg, "-v", "quiet", "-i", str(path), "-f", "s16le", "-ac", "1",
         "-ar", str(sample_rate), "pipe:1"],
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout
    return len(np.frombuffer(pcm, dtype=np.int16)) / sample_rate * 1000.0


def _ffprobe_json_cli(path: Path, *args: str) -> dict:
    ffprobe = shutil.which("ffprobe")
    out = subprocess.run(
        [ffprobe, "-v", "error", "-print_format", "json", *args, str(path)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    return json.loads(out)


class TestConcatPathQuoting:
    def test_quote_ffmpeg_concat_path_plain_posix_path(self, tmp_path):
        audio = tmp_path / "chapter_01.m4a"

        assert quote_ffmpeg_concat_path(audio) == f"'{audio.absolute()}'"

    def test_quote_ffmpeg_concat_path_escapes_apostrophes_by_quote_splicing(self, tmp_path):
        audio = tmp_path / "chapter one's intro.m4a"

        quoted = quote_ffmpeg_concat_path(audio)

        expected = str(audio.absolute()).replace("'", "'\\''")
        assert quoted == f"'{expected}'"

    def test_quote_ffmpeg_concat_path_preserves_windows_drive_path_backslashes(self):
        # The concat demuxer copies bytes verbatim inside single quotes, so
        # backslashes must never be doubled or Windows paths break.
        raw = r"C:\Users\reader\Audiobooks\chapter_01.m4a"

        quoted = quote_ffmpeg_concat_path(raw)

        assert quoted == f"'{Path(raw).absolute()}'"
        assert quoted.count("\\") == raw.count("\\")

    def test_quote_ffmpeg_concat_path_preserves_unc_path_backslashes(self):
        raw = r"\\fileserver\audiobooks\book one\chapter_01.m4a"

        quoted = quote_ffmpeg_concat_path(raw)

        assert quoted == f"'{Path(raw).absolute()}'"
        assert quoted.count("\\") == raw.count("\\")
        assert "\\\\\\\\" not in quoted


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
    @patch("subprocess.run")
    @patch("shutil.which")
    @pytest.mark.skipif(sys.platform != "darwin", reason="Homebrew fallback is macOS-specific")
    def test_wav_to_mp3_uses_homebrew_ffmpeg_when_path_lookup_misses_it(
        self,
        mock_which,
        mock_run,
        tmp_path,
    ):
        mock_which.return_value = None
        mock_run.side_effect = _successful_ffmpeg_run
        input_path = tmp_path / "input.wav"
        input_path.write_bytes(b"wav")

        def fake_is_file(path):
            return str(path) == "/opt/homebrew/bin/ffmpeg" or Path(path).exists()

        def fake_access(path, mode):
            return str(path) == "/opt/homebrew/bin/ffmpeg"

        with patch("pathlib.Path.is_file", fake_is_file), patch("os.access", fake_access):
            encoder = AudioEncoder()
            encoder.wav_to_mp3(input_path, tmp_path / "out.mp3")

        args = mock_run.call_args.args[0]
        assert args[0] == "/opt/homebrew/bin/ffmpeg"

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_wav_to_mp3(self, mock_which, mock_run, tmp_path):
        mock_which.return_value = "/usr/bin/ffmpeg"
        mock_run.side_effect = _successful_ffmpeg_run

        encoder = AudioEncoder()

        # We need to mock input path exists
        with patch("pathlib.Path.exists", return_value=True):
            encoder.wav_to_mp3("input.wav", tmp_path / "output.mp3")

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "/usr/bin/ffmpeg"
        assert "-b:a" in args
        assert "128k" in args
        assert "-q:a" not in args
        assert "-af" not in args

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_wav_to_mp3_includes_optional_audio_filter(self, mock_which, mock_run, tmp_path):
        mock_which.return_value = "/usr/bin/ffmpeg"
        mock_run.side_effect = _successful_ffmpeg_run

        encoder = AudioEncoder()

        with patch("pathlib.Path.exists", return_value=True):
            encoder.wav_to_mp3(
                "input.wav", tmp_path / "output.mp3", audio_filter="volume=3.0dB"
            )

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
        assert re.fullmatch(r"\.[0-9a-f]{32}\.tmp" + re.escape(output.suffix), temp_output.name)

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_create_m4a_reencodes_m4a_inputs_for_gapless_prose(self, mock_which, mock_run, tmp_path):
        # document_audio is continuous prose. Stream-copying independently encoded
        # .m4a sections re-bakes AAC priming/padding at every seam, so the packager
        # must single-pass re-encode to keep the narration gapless.
        mock_which.return_value = "/usr/bin/ffmpeg"
        mock_run.side_effect = _successful_ffmpeg_run

        packager = audio_module.DocumentAudioPackager()
        metadata = AudiobookMetadata(title="Document", author="Author")

        first = tmp_path / "01_intro.m4a"
        second = tmp_path / "02_body.m4a"
        first.write_bytes(b"one")
        second.write_bytes(b"two")

        output = tmp_path / "Document.m4a"
        packager.create_m4a(
            mp3_files=[first, second],
            metadata=metadata,
            output_path=output,
        )

        final_args = mock_run.call_args_list[0].args[0]
        assert final_args[final_args.index("-c:a") + 1] == "aac"
        assert "-b:a" in final_args
        assert "copy" not in final_args
        assert "-metadata" in final_args
        assert "title=Document" in final_args
        temp_output = Path(final_args[-1])
        assert temp_output.parent == output.parent
        assert re.fullmatch(r"\.[0-9a-f]{32}\.tmp" + re.escape(output.suffix), temp_output.name)

    def test_create_m4a_reencode_avoids_streamcopy_accumulated_gap(self, tmp_path):
        # Real-ffmpeg: continuous prose sections encoded to .m4a, then joined.
        ffmpeg = shutil.which("ffmpeg")
        assert ffmpeg, "ffmpeg is required for document_audio gapless coverage"

        encoder = AudioEncoder()
        section_count = 6
        section_seconds = 0.30
        phase = 0
        sections: list[Path] = []
        for i in range(section_count):
            wav = tmp_path / f"section_{i:02d}.wav"
            phase += _write_sine_wav(wav, 300.0, section_seconds, phase_samples=phase)
            m4a = tmp_path / f"section_{i:02d}.m4a"
            encoder.wav_to_aac(wav, m4a, bitrate="128k")
            sections.append(m4a)

        content_ms = section_count * section_seconds * 1000.0

        # Fixed behavior: single-pass re-encode of the continuous sections.
        reencoded = tmp_path / "document.m4a"
        audio_module.DocumentAudioPackager().create_m4a(
            sections,
            AudiobookMetadata(title="Doc", author="Author"),
            reencoded,
        )
        reencode_ms = _decoded_duration_ms(reencoded)

        # Baseline: what stream-copy concat of the same sections produces.
        concat = tmp_path / "concat.txt"
        with open(concat, "w", encoding="utf-8") as handle:
            for section in sections:
                handle.write(f"file '{section}'\n")
        streamcopy = tmp_path / "streamcopy.m4a"
        subprocess.run(
            [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
             "-vn", "-c:a", "copy", str(streamcopy)],
            capture_output=True,
            check=True,
            timeout=30,
        )
        streamcopy_ms = _decoded_duration_ms(streamcopy)

        # Stream-copy re-bakes ~2 AAC frames of priming/padding per seam, which
        # accumulates; the single-pass re-encode carries a clearly smaller gap.
        assert reencode_ms < streamcopy_ms - 80.0
        assert (reencode_ms - content_ms) < (streamcopy_ms - content_ms)

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_create_m4a_reencodes_for_mp3_inputs(self, mock_which, mock_run, tmp_path):
        mock_which.return_value = "/usr/bin/ffmpeg"
        mock_run.side_effect = _successful_ffmpeg_run

        packager = audio_module.DocumentAudioPackager()
        metadata = AudiobookMetadata(title="Document", author="Author")

        first = tmp_path / "01_intro.mp3"
        second = tmp_path / "02_body.mp3"
        first.write_bytes(b"one")
        second.write_bytes(b"two")

        output = tmp_path / "Document.m4a"
        packager.create_m4a(
            mp3_files=[first, second],
            metadata=metadata,
            output_path=output,
            bitrate="96k",
        )

        final_args = mock_run.call_args_list[0].args[0]
        assert final_args[final_args.index("-c:a") + 1] == "aac"
        assert final_args[final_args.index("-b:a") + 1] == "96k"

class TestM4BPackager:
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

        temp_output = Path(final_args[-1])
        assert final_args == [
            "/usr/bin/ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            final_args[7],
            "-f",
            "ffmetadata",
            "-i",
            final_args[11],
            "-map_metadata",
            "1",
            "-map_chapters",
            "1",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(temp_output),
        ]
        assert temp_output.parent == output.parent
        assert re.fullmatch(r"\.[0-9a-f]{32}\.tmp" + re.escape(output.suffix), temp_output.name)

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_create_m4b_stream_copies_m4a_inputs_in_two_passes(self, mock_which, mock_run, tmp_path):
        # Stream-copied AAC chapters keep the fast copy path, but chapter markers
        # must come from the encoded/joined timeline. The packager concatenates
        # (pass 1) then muxes chapters/metadata onto the copy (pass 2). When the
        # timeline probe is unavailable it falls back to the supplied durations.
        mock_which.return_value = "/usr/bin/ffmpeg"

        def fake_run(cmd, **_kwargs):  # noqa: ANN001, ANN202
            if "-print_format" in cmd or "-show_entries" in cmd:
                # ffprobe timeline probe: force graceful fallback.
                return MagicMock(returncode=0, stdout="", stderr="")
            output_path = Path(cmd[-1])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"audio")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = fake_run

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

        ffmpeg_calls = [
            call.args[0]
            for call in mock_run.call_args_list
            if "-print_format" not in call.args[0] and "-show_entries" not in call.args[0]
        ]
        assert len(ffmpeg_calls) == 2
        concat_cmd, mux_cmd = ffmpeg_calls

        # Pass 1: stream-copy concat (never re-encodes AAC chapters).
        assert "concat" in concat_cmd
        assert concat_cmd[concat_cmd.index("-c:a") + 1] == "copy"

        # Pass 2: mux chapters/metadata onto the copied audio, still stream copy.
        assert mux_cmd[mux_cmd.index("-c:a") + 1] == "copy"
        assert "-b:a" not in mux_cmd
        ffmetadata_f_index = mux_cmd.index("-f")
        assert mux_cmd[ffmetadata_f_index + 1] == "ffmetadata"
        assert mux_cmd[mux_cmd.index("-map_metadata") + 1] == "1"
        assert mux_cmd[mux_cmd.index("-map_chapters") + 1] == "1"
        temp_output = Path(mux_cmd[-1])
        assert temp_output.parent == output.parent
        assert re.fullmatch(r"\.[0-9a-f]{32}\.tmp" + re.escape(output.suffix), temp_output.name)

    def test_create_m4b_stream_copy_markers_match_encoded_timeline(self, tmp_path):
        # Real-ffmpeg: stream-copied chapter markers must land on the actual
        # encoded segment offsets, not the source WAV-duration sums that drift.
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        assert ffmpeg and ffprobe, "ffmpeg/ffprobe required for chapter-marker drift coverage"

        encoder = AudioEncoder()
        specs = [(440.0, 0.30), (660.0, 0.40), (880.0, 0.55)]
        chapters_m4a: list[Path] = []
        source_durations_ms: list[int] = []
        for i, (freq, dur) in enumerate(specs, 1):
            wav = tmp_path / f"chapter_{i:02d}.wav"
            _write_sine_wav(wav, freq, dur)
            m4a = tmp_path / f"chapter_{i:02d}.m4a"
            encoder.wav_to_aac(wav, m4a, bitrate="128k")
            chapters_m4a.append(m4a)
            source_durations_ms.append(int(dur * 1000))

        output = tmp_path / "book.m4b"
        M4BPackager().create_m4b(
            chapters_m4a,
            [f"Chapter {i}" for i in range(1, len(specs) + 1)],
            AudiobookMetadata(title="Drift Book", author="Author"),
            output,
            chapter_durations_ms=source_durations_ms,
        )

        # Ground truth (independent of the written markers): where each stream-copied
        # segment actually begins on the joined presentation timeline.
        segment_packets = [
            int(
                _ffprobe_json_cli(
                    m, "-select_streams", "a", "-count_packets",
                    "-show_entries", "stream=nb_read_packets",
                )["streams"][0]["nb_read_packets"]
            )
            for m in chapters_m4a
        ]
        packet_pts = [
            float(p["pts_time"])
            for p in _ffprobe_json_cli(
                output, "-select_streams", "a", "-show_entries", "packet=pts_time"
            )["packets"]
        ]
        true_start_ms: list[float] = []
        idx = 0
        for count in segment_packets:
            true_start_ms.append(round(packet_pts[idx] * 1000))
            idx += count
        container_ms = float(_ffprobe_json_cli(output, "-show_format")["format"]["duration"]) * 1000.0

        chapters = _ffprobe_json_cli(output, "-show_chapters")["chapters"]
        marker_starts = [int(float(ch["start_time"]) * 1000) for ch in chapters]

        # Fixed markers track the real encoded offsets within a few ms.
        for real, got in zip(true_start_ms, marker_starts):
            assert abs(got - real) <= 12, f"chapter marker {got}ms drifted from real offset {real}ms"
        # Last chapter END tracks the true joined duration, not the source-duration sum.
        assert abs(int(float(chapters[-1]["end_time"]) * 1000) - container_ms) <= 15

        # Sanity: naive source-WAV-duration markers genuinely drift here, so this
        # assertion set actually distinguishes the fix from the regression.
        source_starts = [0]
        for duration in source_durations_ms[:-1]:
            source_starts.append(source_starts[-1] + duration)
        assert max(abs(s - r) for s, r in zip(source_starts, true_start_ms)) > 20

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
        temp_output = Path(final_args[-1])
        assert final_args == [
            "/usr/bin/ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            final_args[7],
            "-f",
            "ffmetadata",
            "-i",
            final_args[11],
            "-i",
            str(cover_file),
            "-map_metadata",
            "1",
            "-map_chapters",
            "1",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-map",
            "0:a",
            "-map",
            "2:v",
            "-c:v",
            "mjpeg",
            "-disposition:v",
            "attached_pic",
            "-movflags",
            "+faststart",
            str(temp_output),
        ]

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

    @patch("shutil.which")
    def test_probe_stream_copy_offsets_streams_csv_instead_of_json(self, mock_which, tmp_path):
        mock_which.side_effect = lambda name: f"/usr/bin/{name}"
        packager = M4BPackager()

        probe_cmds: list[list[str]] = []

        def fake_run_ffmpeg_command(cmd, **_kwargs):  # noqa: ANN001, ANN202
            probe_cmds.append(list(cmd))
            if "-count_packets" in cmd:
                return MagicMock(returncode=0, stdout="2\n", stderr="")
            return MagicMock(returncode=0, stdout="0.700000\n", stderr="")

        class _FakeProcess:
            def __init__(self) -> None:
                self.stdout = iter(["0.000000\n", "0.032000\n", "0.064000\n", "0.096000\n"])
                self.stderr = None

            def wait(self, timeout=None):  # noqa: ANN001, ANN201
                return 0

            def poll(self):  # noqa: ANN201
                return 0

            def kill(self) -> None:
                return None

        popen_cmds: list[list[str]] = []

        def fake_popen(cmd, **_kwargs):  # noqa: ANN001, ANN202
            popen_cmds.append(list(cmd))
            return _FakeProcess()

        with patch(
            "modules.audio.packager.run_ffmpeg_command",
            side_effect=fake_run_ffmpeg_command,
        ), patch("modules.audio.packager.subprocess.Popen", side_effect=fake_popen):
            offsets = packager._probe_stream_copy_offsets_ms(
                tmp_path / "concat.m4a",
                [tmp_path / "one.m4a", tmp_path / "two.m4a"],
            )

        assert offsets == ([0, 64], 700)
        for cmd in probe_cmds + popen_cmds:
            assert "-print_format" not in cmd
            assert "json" not in cmd
            assert "csv=p=0" in cmd

    @patch("shutil.which")
    def test_stream_copy_markers_fall_back_with_warning_on_memory_error(
        self,
        mock_which,
        tmp_path,
        caplog,
    ):
        mock_which.side_effect = lambda name: f"/usr/bin/{name}"
        packager = M4BPackager()

        with patch(
            "modules.audio.packager.run_ffmpeg_command",
            side_effect=MemoryError("ffprobe output too large"),
        ), caplog.at_level("WARNING", logger="modules.audio.packager"):
            markers = packager._stream_copy_chapter_markers(
                tmp_path / "concat.m4a",
                [tmp_path / "one.m4a", tmp_path / "two.m4a"],
                ["One", "Two"],
                fallback_durations_ms=[1000, 1500],
            )

        assert [(m.start_ms, m.end_ms) for m in markers] == [(0, 1000), (1000, 2500)]
        assert any("fall" in record.getMessage().lower() for record in caplog.records)

    @patch("shutil.which")
    def test_stream_copy_markers_truncate_when_names_outnumber_probed_offsets(
        self,
        mock_which,
        tmp_path,
    ):
        mock_which.side_effect = lambda name: f"/usr/bin/{name}"
        packager = M4BPackager()

        with patch.object(
            packager,
            "_probe_stream_copy_offsets_ms",
            return_value=([0], 900),
        ):
            markers = packager._stream_copy_chapter_markers(
                tmp_path / "concat.m4a",
                [tmp_path / "one.m4a"],
                ["One", "Two"],
            )

        assert [(m.title, m.start_ms, m.end_ms) for m in markers] == [("One", 0, 900)]

    @patch("shutil.which")
    def test_probe_stream_copy_offsets_preserves_zero_packet_chapter_boundary(
        self,
        mock_which,
        tmp_path,
    ):
        # DEFECT 4: a chapter with 0 audio packets shares its successor's boundary
        # packet index. Deduping boundaries into a set drops one entry, so the
        # returned starts list is shorter than the chapter list and the later
        # zip() truncates the TOC (wrong starts after the empty chapter, last
        # chapter lost). Every chapter must keep its own start offset.
        mock_which.side_effect = lambda name: f"/usr/bin/{name}"
        packager = M4BPackager()

        packet_counts = iter(["2\n", "0\n", "3\n"])

        def fake_run_ffmpeg_command(cmd, **_kwargs):  # noqa: ANN001, ANN202
            if "-count_packets" in cmd:
                return MagicMock(returncode=0, stdout=next(packet_counts), stderr="")
            return MagicMock(returncode=0, stdout="0.160000\n", stderr="")

        class _FakeProcess:
            def __init__(self) -> None:
                self.stdout = iter(
                    [
                        "0.000000\n",
                        "0.032000\n",
                        "0.064000\n",
                        "0.096000\n",
                        "0.128000\n",
                    ]
                )
                self.stderr = None

            def wait(self, timeout=None):  # noqa: ANN001, ANN201
                return 0

            def poll(self):  # noqa: ANN201
                return 0

            def kill(self) -> None:
                return None

        with patch(
            "modules.audio.packager.run_ffmpeg_command",
            side_effect=fake_run_ffmpeg_command,
        ), patch(
            "modules.audio.packager.subprocess.Popen",
            side_effect=lambda *_a, **_k: _FakeProcess(),
        ):
            offsets = packager._probe_stream_copy_offsets_ms(
                tmp_path / "concat.m4a",
                [tmp_path / "a.m4a", tmp_path / "b.m4a", tmp_path / "c.m4a"],
            )

        # Three chapters -> three starts. The empty middle chapter shares the
        # third chapter's start (it occupies zero time at that offset).
        assert offsets == ([0, 64, 64], 160)

    @patch("shutil.which")
    def test_stream_copy_markers_keep_empty_middle_chapter_aligned(
        self,
        mock_which,
        tmp_path,
    ):
        # DEFECT 4 end-to-end: with a zero-length chapter between two real ones,
        # all chapters survive with correct starts and the empty one is a
        # zero-span marker, not a dropped/misaligned entry.
        mock_which.side_effect = lambda name: f"/usr/bin/{name}"
        packager = M4BPackager()

        with patch.object(
            packager,
            "_probe_stream_copy_offsets_ms",
            return_value=([0, 64, 64], 160),
        ):
            markers = packager._stream_copy_chapter_markers(
                tmp_path / "concat.m4a",
                [tmp_path / "a.m4a", tmp_path / "b.m4a", tmp_path / "c.m4a"],
                ["One", "Two", "Three"],
            )

        assert [(m.title, m.start_ms, m.end_ms) for m in markers] == [
            ("One", 0, 64),
            ("Two", 64, 64),
            ("Three", 64, 160),
        ]

    @patch("shutil.which")
    def test_probe_concat_packet_starts_times_out_on_stalled_ffprobe(
        self,
        mock_which,
        tmp_path,
    ):
        # DEFECT 7: a stalled ffprobe whose stdout never ends must not hang the
        # packet-timestamp probe forever; it is killed at the module timeout.
        mock_which.side_effect = lambda name: f"/usr/bin/{name}"
        packager = M4BPackager()

        class _NeverEndingStdout:
            def __init__(self) -> None:
                self.stop = False

            def __iter__(self):  # noqa: ANN204
                return self

            def __next__(self):  # noqa: ANN204
                if self.stop:
                    raise StopIteration
                time.sleep(0.01)
                return "1.000000\n"

        class _StalledProcess:
            def __init__(self) -> None:
                self.stdout = _NeverEndingStdout()
                self.killed = False

            def poll(self):  # noqa: ANN201
                return None

            def kill(self) -> None:
                self.killed = True
                self.stdout.stop = True

            def wait(self, timeout=None):  # noqa: ANN001, ANN201
                return -9

        proc = _StalledProcess()

        with patch(
            "modules.audio.packager.subprocess.Popen", return_value=proc
        ), patch(
            "modules.audio.packager.ffmpeg_timeout_seconds", return_value=0.1
        ):
            with pytest.raises(RuntimeError, match="timed out"):
                packager._probe_concat_packet_starts(
                    "/usr/bin/ffprobe", tmp_path / "concat.m4a", {0}
                )

        assert proc.killed

    def test_create_m4b_rejects_empty_input(self, tmp_path):
        # DEFECT 14: empty input must fail with a clear precondition error rather
        # than an opaque ffmpeg concat error.
        packager = M4BPackager()
        with pytest.raises(ValueError, match="must not be empty"):
            packager.create_m4b(
                [],
                [],
                AudiobookMetadata(title="Empty", author="Author"),
                tmp_path / "out.m4b",
            )

    @patch("shutil.which")
    def test_generate_chapter_markers_degrades_when_duration_probe_fails(
        self,
        mock_which,
        caplog,
    ):
        mock_which.side_effect = lambda name: f"/usr/bin/{name}"
        packager = M4BPackager()

        durations = iter([2.0, KeyError("duration"), 3.0])

        def fake_get_duration(_self, _path):  # noqa: ANN001, ANN202
            value = next(durations)
            if isinstance(value, Exception):
                raise value
            return value

        with patch(
            "modules.audio.encoder.AudioEncoder.get_duration",
            new=fake_get_duration,
        ), caplog.at_level("WARNING", logger="modules.audio.packager"):
            markers = packager._generate_chapter_markers(
                [Path("one.mp3"), Path("two.mp3"), Path("three.mp3")],
                ["One", "Two", "Three"],
            )

        assert [(m.start_ms, m.end_ms) for m in markers] == [
            (0, 2000),
            (2000, 2000),
            (2000, 5000),
        ]
        assert any("duration" in record.getMessage().lower() for record in caplog.records)

    @pytest.mark.skipif(os.name == "nt", reason="backslash is a path separator on Windows")
    def test_create_m4b_real_ffmpeg_concats_paths_with_backslash_and_apostrophe(self, tmp_path):
        # Regression: doubling backslashes inside single-quoted concat entries makes
        # ffmpeg look for a nonexistent file, so packaging fails outright.
        ffmpeg = shutil.which("ffmpeg")
        assert ffmpeg, "ffmpeg is required for concat quoting coverage"

        audio_file = tmp_path / "chapter one's \\ intro.wav"
        _write_sine_wav(audio_file, 440.0, 0.2)

        output = tmp_path / "book.m4b"
        M4BPackager().create_m4b(
            [audio_file],
            ["Intro"],
            AudiobookMetadata(title="Quoting Book", author="Author"),
            output,
            chapter_durations_ms=[200],
        )

        assert output.exists()
        assert output.stat().st_size > 0

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
        assert "title=Chapter \\= One \\# intro" in text
        # A raw newline must not break the key/value line, and it must not be
        # escaped either: ffmpeg reads "\" as "next character is literal", so
        # "\n" round-trips through ffprobe as the letter n ("line onenline
        # two"). Verified against real ffmpeg. Collapse to a space instead.
        assert "comment=line one line two" in text
        assert "comment=line one\nline two" not in text
        assert "\\n" not in text

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
