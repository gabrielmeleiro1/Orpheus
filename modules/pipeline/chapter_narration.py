"""Spoken navigation and real audio pauses at chapter boundaries."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import numpy as np

from .stages.synthesis import ChapterAudioAccumulator

# Chapter audio is: announcement, heading pause, body, end pause. Kokoro adds
# roughly 0.2-0.5 s of its own trailing silence to each clip, so a chapter
# change is heard as about 2 s of quiet (end pause + next announcement lead-in)
# and the body follows its announcement after about 2 s. That stays within the
# usual audiobook range (0.5-1 s lead-in, 1-5 s tail) without dead air.
CHAPTER_HEADING_PAUSE_SECONDS = 1.5
CHAPTER_END_PAUSE_SECONDS = 1.5
_LABEL = re.compile(r"^(chapter|section|part|book)\b\s*[:.\-–—]?\s*", re.I)
_NUMBER = re.compile(
    r"^(?:number\s+)?(\d+(?:\.\d+)*|[ivxlcdm]+|"
    r"(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)"
    r"(?:[ -]+(?:one|two|three|four|five|six|seven|eight|nine))?|"
    r"one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen)"
    r"\b[\s:.\-–—]*", re.I,
)


def _plain_title(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[#*_`]", "", str(title or ""))).strip()


def chapter_announcement(chapter: Any, conversion_mode: str = "audiobook") -> str:
    """Keep explicit source labels/numbers; otherwise use the parsed sequence."""
    from modules.tts.speech_typography import normalize_speech_typography

    title = _plain_title(chapter.title)
    kind = "Section" if conversion_mode == "document_audio" else "Chapter"
    number = str(chapter.number)
    label = _LABEL.match(title)
    if label:
        kind = label.group(1).capitalize()
        title = title[label.end():]
        numbered = _NUMBER.match(title)
        if numbered:
            number = numbered.group(1)
            title = title[numbered.end():]
            if re.fullmatch(r"[ivxlcdm]+", number, re.I):
                values = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
                digits = [values[c] for c in number.lower()]
                number = str(sum(-v if i + 1 < len(digits) and v < digits[i + 1] else v
                                 for i, v in enumerate(digits)))
    elif title == number:
        title = ""
    title = normalize_speech_typography(
        title, str(getattr(chapter, "content", "") or ""), heading=True,
    )
    suffix = f" {title.rstrip('.!?')}." if title else ""
    return f"{kind} number {number}.{suffix}"


def chapter_body(chapter: Any) -> str:
    """Remove the parser-identified heading span, preserving opening prose."""
    content = str(chapter.content or "")
    heading = getattr(chapter, "heading_text", "")
    if isinstance(heading, str) and heading and content.startswith(heading):
        return content[len(heading):].lstrip()
    return content


def add_chapter_narration(runtime: Any, chapter: Any, body: Path | np.ndarray, result: Any):
    """Wrap synthesized body before encoding, including file-backed/overlap paths."""
    if runtime.check_cancelled():
        return None
    announcement = chapter_announcement(chapter, runtime.config.conversion_mode)
    intro = np.asarray(runtime.synthesize_speech(
        text=announcement, voice=runtime.config.voice, speed=runtime.config.speed,
    ), dtype=np.float32)
    if runtime.check_cancelled():
        return None
    if intro.ndim != 1 or not intro.size or not np.isfinite(intro).all():
        raise ValueError("No valid audio generated for the chapter introduction")
    sample_rate = runtime.tts_sample_rate()
    output = runtime.config.temp_dir / f"chapter_{chapter.number:02d}_narrated.wav"
    runtime.track_temp_cleanup_path(output)
    accumulator = ChapterAudioAccumulator(
        output_path=output, sample_rate=sample_rate,
        max_in_memory_samples=1,
    )
    # Stream the composition: the original chapter may be hours long.
    accumulator.spill_to_disk()
    try:
        accumulator.add_segment(intro)
        accumulator.add_segment(
            np.zeros(int(CHAPTER_HEADING_PAUSE_SECONDS * sample_rate), dtype=np.float32)
        )
        if isinstance(body, Path):
            runtime.track_temp_cleanup_path(body)
            accumulator.add_file(body)
        else:
            accumulator.add_segment(body)
        accumulator.add_segment(
            np.zeros(int(CHAPTER_END_PAUSE_SECONDS * sample_rate), dtype=np.float32)
        )
        audio = accumulator.finalize()
        result.pcm_rms = accumulator.pcm16_rms()
        if isinstance(body, Path) and body.resolve().is_relative_to(runtime.config.temp_dir.resolve()):
            body.unlink(missing_ok=True)
        return audio
    finally:
        accumulator.close()
