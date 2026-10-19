"""Encoded chapter audio stays below full scale."""

from __future__ import annotations

import re
import shutil
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from modules.audio.ffmpeg import require_ffmpeg_binary
from modules.pipeline.stages.encoding import EncodingStageContext, encode_chapter_aac

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None and not Path(require_ffmpeg_binary("ffmpeg")).exists(),
    reason="ffmpeg is required",
)
RATE = 24000


def _speech_like_wav(path: Path) -> Path:
    """Voiced bursts between pauses, with a few plosive-like transients: the
    gain to -16 dBFS drives those transients past full scale, as in real speech."""
    t = np.arange(RATE * 8) / RATE
    signal = sum(np.sin(2 * np.pi * 140 * k * t + k) / k for k in range(1, 12)) * 0.1
    signal *= (t % 1.0) < 0.5
    for second in (1, 4, 6):
        start = second * RATE + 600
        signal[start:start + 120] += 0.5 * np.hanning(120)
    pcm = (np.clip(signal, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(RATE)
        wav_file.writeframes(pcm.tobytes())
    return path


def _levels(path: Path) -> tuple[float, float, float]:
    """Return (true peak dBTP, RMS dBFS, sample peak dBFS) of the decoded file."""
    ffmpeg = require_ffmpeg_binary("ffmpeg")
    report = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-i", str(path), "-af",
         "ebur128=peak=true,astats=metadata=0", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    ).stderr
    true_peak = float(re.findall(r"Peak:\s+(-?[\d.]+|-inf) dBFS", report)[-1])
    rms = float(re.findall(r"RMS level dB:\s+(-?[\d.]+)", report)[-1])
    sample_peak = float(re.findall(r"Peak level dB:\s+(-?[\d.]+)", report)[-1])
    return true_peak, rms, sample_peak


def test_normalized_chapter_encode_keeps_true_peak_below_full_scale(tmp_path):
    source = _speech_like_wav(tmp_path / "chapter.wav")
    context = EncodingStageContext(
        config=SimpleNamespace(normalize_volume=True, target_dbfs=-16.0, mp3_bitrate="128k", temp_dir=tmp_path),
        sample_rate=RATE,
        encoding_stage=None,
        set_stage=lambda stage: None,
        notify_progress=lambda **kwargs: None,
        log_verbose=lambda message, level="info": None,
    )

    encoded = encode_chapter_aac(
        context, SimpleNamespace(number=1, title="One"), source, output_path=tmp_path / "chapter.m4a",
    )

    # Reference: the same gain without a ceiling, as encodes were made before.
    gain_only = tmp_path / "gain_only.m4a"
    subprocess.run(
        [require_ffmpeg_binary("ffmpeg"), "-y", "-v", "error", "-i", str(source),
         "-af", f"volume={-16.0 - 20 * np.log10(_pcm_rms(source)):.4f}dB",
         "-c:a", "aac", "-b:a", "128k", str(gain_only)],
        check=True,
    )
    _reference_true_peak, reference_rms, reference_sample_peak = _levels(gain_only)
    true_peak, rms, sample_peak = _levels(encoded)

    # Without a ceiling the encode is driven into full scale (decoded peaks clip).
    assert reference_sample_peak > -0.1, "the fixture must clip without a limiter"
    assert sample_peak <= -1.0, sample_peak
    assert true_peak <= -1.0, true_peak
    # The limiter only touches peaks: loudness is unchanged within half a dB.
    assert rms == pytest.approx(reference_rms, abs=0.5)


def _pcm_rms(path: Path) -> float:
    with wave.open(str(path), "rb") as wav_file:
        samples = np.frombuffer(wav_file.readframes(wav_file.getnframes()), dtype=np.int16)
    return float(np.sqrt(np.mean(np.square(samples.astype(np.float64)))) / 32768.0)
