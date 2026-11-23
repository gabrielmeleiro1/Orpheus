"""Pipeline packaging-stage helpers."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

from modules.pipeline.book_workspace import sanitize_book_title
from modules.pipeline.result_validation import (
    format_failure_notes,
    select_packaging_audio_inputs,
    summarize_packaging_input_failures,
)


def narrator_name(voice: str | None) -> str | None:
    """Display name for the narrator tag ("Alloy"), or None without a voice."""
    from modules.tts.voices import KOKORO_VOICE_CATALOG

    names = {str(entry["id"]): str(entry["name"]) for entry in KOKORO_VOICE_CATALOG}
    parts = [part.strip() for part in str(voice or "").split(",") if part.strip()]
    if not parts:
        return None
    return " & ".join(names.get(part, part) for part in parts)


def _write_cover_for(
    source_path: Path | None, cancel_requested: Callable[[], bool] | None = None,
) -> Path | None:
    """Write the source's cover (EPUB cover or PDF first page) to a temp file.

    Embedded cover bytes are not validated at extraction, so a cover FFmpeg
    cannot use is dropped here rather than failing the whole book at the mux.
    """
    if source_path is None:
        return None
    from modules.audio.packager import cover_image_is_usable
    from modules.ingestion.cover_art import extract_cover

    cover = extract_cover(source_path)
    if cover is None:
        return None
    suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}
    folder = Path(tempfile.mkdtemp(prefix="orpheus-cover-"))
    path = folder / f"cover{suffix.get(cover.media_type, '.img')}"
    path.write_bytes(cover.data)
    try:
        usable = cover_image_is_usable(path, cancel_requested)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    if not usable:
        shutil.rmtree(folder, ignore_errors=True)
        return None
    return path


def _raise_if_packaging_inputs_invalid(chapter_results: list[Any]) -> None:
    failure_notes = summarize_packaging_input_failures(chapter_results)
    if not failure_notes:
        return
    summary = format_failure_notes(failure_notes)
    raise RuntimeError(f"Refusing to package incomplete chapter set. {summary}")


def _aac_path_is_durable(aac_path: Path, runtime: Any | None) -> bool:
    """True when AAC lives outside temp_dir (repair-resolvable chapter artifact)."""
    temp_dir = getattr(getattr(runtime, "config", None), "temp_dir", None)
    if temp_dir is None:
        # No runtime temp root: treat non-temp-looking durable chapter dirs as durable.
        return True
    try:
        Path(aac_path).resolve().relative_to(Path(temp_dir).resolve())
    except (TypeError, ValueError):
        return True
    return False


def resolve_background_audio_futures(chapter_results: list[Any], runtime: Any | None = None) -> None:
    """Wait for background chapter audio encodes and attach their output paths."""
    for result in chapter_results:
        future = getattr(result, "aac_future", None)
        if future is None:
            continue
        aac_path = future.result()
        result.aac_future = None
        result.aac_path = Path(aac_path)
        if getattr(result, "mp3_path", None) is None and _aac_path_is_durable(
            result.aac_path, runtime
        ):
            # AAC-only conversions reuse chapters.mp3_path for the durable .m4a.
            result.mp3_path = result.aac_path
        if runtime is not None:
            track_temp_cleanup_path = getattr(runtime, "track_temp_cleanup_path", None)
            if callable(track_temp_cleanup_path):
                track_temp_cleanup_path(result.aac_path)


def _collect_packaging_inputs(
    chapter_results: list[Any],
    runtime: Any | None = None,
) -> tuple[list[Path], list[str], Optional[list[int]]]:
    """Collect validated packaging inputs from chapter results."""
    audio_files: list[Path] = []
    chapter_names: list[str] = []
    chapter_durations_ms: list[int] = []
    has_complete_durations = True
    resolve_background_audio_futures(chapter_results, runtime=runtime)
    _raise_if_packaging_inputs_invalid(chapter_results)

    audio_files = select_packaging_audio_inputs(chapter_results)
    for result in chapter_results:
        chapter_names.append(result.chapter_title)
        if result.duration_ms > 0:
            chapter_durations_ms.append(int(result.duration_ms))
        else:
            has_complete_durations = False

    if not audio_files:
        raise RuntimeError(
            "No homogeneous chapter audio to package. Chapters must all provide AAC, "
            "all provide WAV, or all provide MP3 inputs."
        )

    return (
        audio_files,
        chapter_names,
        chapter_durations_ms if has_complete_durations else None,
    )


def package_chapters(
    runtime: Any,
    chapter_results: list[Any],
    title: str,
    author: Optional[str],
    output_dir: Path,
) -> Path:
    """
    Package chapter audio into the final M4A/M4B artifact.

    Returns:
        Path to M4B file
    """
    audio_files, chapter_names, chapter_durations_ms = _collect_packaging_inputs(
        chapter_results,
        runtime=runtime,
    )

    from modules.audio.packager import M4BPackager, AudiobookMetadata
    from modules.audio.document_packager import DocumentAudioPackager

    # No author means no artist tag: players show "Unknown" from a written tag
    # as if it were a real name. Players read the composer tag as the narrator,
    # so it gets the voice's display name rather than its internal id.
    metadata = AudiobookMetadata(
        title=title,
        author=(author or "").strip(),
        narrator=narrator_name(runtime.config.voice),
        comment="Generated by Orpheus",
    )

    # Output path: cap the sanitized title so the filename (plus the atomic
    # temp-name suffix) stays under filesystem limits, and never emit a bare
    # ".m4b"/".m4a" hidden file when the title sanitizes to nothing.
    safe_title = (
        sanitize_book_title(title)[:50] if str(title or "").strip() else "audiobook"
    )
    conversion_mode = getattr(runtime.config, "conversion_mode", "audiobook")
    bitrate = getattr(runtime.config, "mp3_bitrate", "128k")
    cancel_requested = getattr(runtime, "check_cancelled", None)

    if conversion_mode == "document_audio":
        m4a_path = output_dir / f"{safe_title}.m4a"
        packager = DocumentAudioPackager()
        packager.create_m4a(
            mp3_files=audio_files,
            metadata=metadata,
            output_path=m4a_path,
            bitrate=bitrate,
            cancel_requested=cancel_requested,
            chapter_durations_ms=chapter_durations_ms,
        )
        return m4a_path

    m4b_path = output_dir / f"{safe_title}.m4b"
    cover_file = _write_cover_for(
        getattr(getattr(runtime, "state", None), "source_path", None), cancel_requested,
    )
    if cover_file is not None:
        metadata.cover_path = cover_file
    try:
        packager = M4BPackager()
        packager.create_m4b(
            mp3_files=audio_files,
            chapter_names=chapter_names,
            metadata=metadata,
            output_path=m4b_path,
            chapter_durations_ms=chapter_durations_ms,
            cancel_requested=cancel_requested,
            bitrate=bitrate,
        )
        for result, marker in zip(chapter_results, packager.chapter_markers):
            result.packaged_start_ms = marker.start_ms
    finally:
        if cover_file is not None:
            shutil.rmtree(cover_file.parent, ignore_errors=True)
        executor = getattr(getattr(runtime, "state", None), "chapter_aac_executor", None)
        if executor is not None:
            executor.shutdown(wait=True)
            runtime.state.chapter_aac_executor = None
            runtime.state.chapter_aac_pending_future = None

    return m4b_path
