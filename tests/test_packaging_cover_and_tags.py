"""Packaged audiobooks carry the source cover and honest tags."""

from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from modules.audio.ffmpeg import require_ffmpeg_binary
from modules.pipeline.models import ChapterResult
from modules.pipeline.stages.packaging import narrator_name, package_chapters

pymupdf = pytest.importorskip("pymupdf")
RATE = 24000


def _wav(path: Path, seconds: float) -> Path:
    t = np.arange(int(RATE * seconds)) / RATE
    pcm = (0.2 * np.sin(2 * np.pi * 220 * t) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(RATE)
        wav_file.writeframes(pcm.tobytes())
    return path


def _pdf(path: Path) -> Path:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "The Lantern at Harrow Point", fontsize=24)
    doc.save(path)
    doc.close()
    return path


def _package(tmp_path: Path, *, author: str | None, source: Path | None) -> dict:
    results = [
        ChapterResult(chapter_number=index, chapter_title=f"Chapter {index}",
                      wav_path=_wav(tmp_path / f"chapter_{index}.wav", 1.0), duration_ms=1000)
        for index in (1, 2)
    ]
    runtime = SimpleNamespace(
        config=SimpleNamespace(voice="af_alloy", conversion_mode="audiobook", temp_dir=tmp_path / "temp"),
        state=SimpleNamespace(source_path=source, chapter_aac_executor=None),
        check_cancelled=lambda: False,
    )
    output = package_chapters(runtime, results, "Harrow Point", author, tmp_path / "out")
    probe = subprocess.run(
        [require_ffmpeg_binary("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(probe.stdout)


def test_cover_is_embedded_and_missing_author_is_not_written_as_unknown(tmp_path):
    media = _package(tmp_path, author=None, source=_pdf(tmp_path / "book.pdf"))

    covers = [stream for stream in media["streams"] if stream.get("disposition", {}).get("attached_pic") == 1]
    assert len(covers) == 1
    tags = {key.lower(): value for key, value in media["format"].get("tags", {}).items()}
    assert "artist" not in tags
    assert tags["composer"] == "Alloy"


def test_author_is_tagged_and_no_cover_when_the_source_has_none(tmp_path):
    media = _package(tmp_path, author="Edith M. Calloway", source=None)

    assert not [stream for stream in media["streams"] if stream.get("codec_type") == "video"]
    tags = {key.lower(): value for key, value in media["format"].get("tags", {}).items()}
    assert tags["artist"] == "Edith M. Calloway"


def test_narrator_name_uses_voice_display_names():
    assert narrator_name("af_alloy") == "Alloy"
    assert narrator_name("af_alloy,am_adam") == "Alloy & Adam"
    assert narrator_name("") is None
