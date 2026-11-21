from __future__ import annotations

import math
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import modules.audio.encoder as audio_encoder_module
from modules.pipeline.models import PipelineStage
from modules.pipeline.stages import encoding


def _write_pcm16_wav(path: Path, samples: np.ndarray, *, sample_rate: int = 24000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(np.asarray(samples, dtype=np.int16).tobytes())


def _pipeline(
    *, target_dbfs: float = -16.0, cancel_requested=None,
) -> encoding.EncodingStageContext:
    return encoding.EncodingStageContext(
        config=SimpleNamespace(
            normalize_volume=True,
            target_dbfs=target_dbfs,
            mp3_bitrate="128k",
        ),
        sample_rate=24000,
        encoding_stage=PipelineStage.ENCODING,
        set_stage=lambda _stage: None,
        notify_progress=lambda **_kwargs: None,
        log_verbose=lambda *_args, **_kwargs: None,
        cancel_requested=cancel_requested,
    )


def test_file_backed_normalization_uses_ffmpeg_filter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    wav_path = tmp_path / "chapter.wav"
    _write_pcm16_wav(wav_path, np.full(24000, 8192, dtype=np.int16))

    captured: dict[str, object] = {}

    class FakeEncoder:
        def wav_to_mp3(
            self,
            input_path: Path,
            output_path: Path,
            *,
            bitrate: str = "128k",
            audio_filter: str | None = None,
            cancel_requested=None,
        ) -> Path:
            captured["input_path"] = Path(input_path)
            captured["bitrate"] = bitrate
            captured["audio_filter"] = audio_filter
            Path(output_path).write_bytes(b"mp3")
            return Path(output_path)

    monkeypatch.setattr(audio_encoder_module, "AudioEncoder", FakeEncoder)

    mp3_path, duration_ms = encoding.encode_chapter_audio(
        _pipeline(target_dbfs=-6.0),
        SimpleNamespace(number=1, title="Chapter 1"),
        chapter_idx=0,
        total_chapters=1,
        output_dir=tmp_path,
        chapter_wav=wav_path,
    )

    assert mp3_path.exists()
    assert duration_ms == 1000
    assert captured["input_path"] == wav_path
    assert captured["bitrate"] == "128k"

    audio_filter = captured["audio_filter"]
    assert isinstance(audio_filter, str)
    volume, limiter = audio_filter.split(",", 1)
    assert volume.startswith("volume=")
    assert volume.endswith("dB")
    assert limiter.startswith("alimiter=limit=0.7943")
    gain_db = float(volume.removeprefix("volume=").removesuffix("dB"))
    expected_gain_db = -6.0 - (20.0 * math.log10(8192 / 32768))
    assert gain_db == pytest.approx(expected_gain_db, abs=1e-4)


def test_silent_file_backed_normalization_skips_filter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    wav_path = tmp_path / "silent.wav"
    _write_pcm16_wav(wav_path, np.zeros(24000, dtype=np.int16))

    captured: dict[str, object] = {}

    class FakeEncoder:
        def wav_to_mp3(
            self,
            input_path: Path,
            output_path: Path,
            *,
            bitrate: str = "128k",
            audio_filter: str | None = None,
            cancel_requested=None,
        ) -> Path:
            captured["input_path"] = Path(input_path)
            captured["audio_filter"] = audio_filter
            Path(output_path).write_bytes(b"mp3")
            return Path(output_path)

    monkeypatch.setattr(audio_encoder_module, "AudioEncoder", FakeEncoder)

    mp3_path, duration_ms = encoding.encode_chapter_audio(
        _pipeline(),
        SimpleNamespace(number=1, title="Silent"),
        chapter_idx=0,
        total_chapters=1,
        output_dir=tmp_path,
        chapter_wav=wav_path,
    )

    assert mp3_path.exists()
    assert duration_ms == 1000
    assert captured["input_path"] == wav_path
    assert captured["audio_filter"] is None


def _successful_ffmpeg_run_for_encoding_stage(cmd, **_kwargs):  # noqa: ANN001, ANN202
    from unittest.mock import MagicMock

    output_path = Path(cmd[-1])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"audio")
    return MagicMock(returncode=0, stdout="", stderr="")


def test_encode_chapter_aac_uses_single_encode_for_short_wav(
    tmp_path: Path,
) -> None:
    from unittest.mock import patch

    wav_path = tmp_path / "chapter.wav"
    _write_pcm16_wav(wav_path, np.zeros(24_000, dtype=np.int16))

    pipeline = _pipeline()
    pipeline.config.temp_dir = tmp_path / "temp"
    pipeline.config.temp_dir.mkdir(parents=True, exist_ok=True)

    with patch("shutil.which", return_value="/usr/bin/ffmpeg"), patch(
        "subprocess.run",
        side_effect=_successful_ffmpeg_run_for_encoding_stage,
    ) as mock_run:
        chapter_aac = encoding.encode_chapter_aac(
            pipeline,
            SimpleNamespace(number=1, title="Chapter 1"),
            chapter_wav=wav_path,
        )

    assert chapter_aac.exists()
    assert mock_run.call_count == 1
    encode_args = mock_run.call_args_list[0].args[0]
    assert encode_args[encode_args.index("-c:a") + 1] == "aac"


def test_chapter_audio_names_match_the_recovery_inventory(tmp_path: Path) -> None:
    from modules.contracts.recovery_inventory import chapter_audio_path

    chapter = SimpleNamespace(number=3, title='Who? "Me": a/b|c*')
    expected = chapter_audio_path(chapter, tmp_path, ".mp3")

    class FakeEncoder:
        def wav_to_mp3(self, _input, output_path, **_kwargs) -> Path:
            Path(output_path).write_bytes(b"mp3")
            return Path(output_path)

    wav_path = tmp_path / "chapter.wav"
    _write_pcm16_wav(wav_path, np.zeros(2400, dtype=np.int16))
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(audio_encoder_module, "AudioEncoder", FakeEncoder)
        mp3_path, _ = encoding.encode_chapter_audio(
            _pipeline(), chapter, chapter_idx=0, total_chapters=1,
            output_dir=tmp_path, chapter_wav=wav_path,
        )

    assert mp3_path == expected
    assert mp3_path.name == "03_Who_ _Me__ a_b_c_.mp3"
