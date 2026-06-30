"""Tests for AAC packaging input selection."""

from __future__ import annotations

from concurrent.futures import Future
from modules.audio.packager import audio_files_support_stream_copy
from modules.pipeline.stages.packaging import resolve_background_audio_futures
from modules.pipeline.result_validation import (
    select_packaging_audio_input,
    select_packaging_audio_inputs,
)


class TestPackagingInputSelection:
    def test_select_packaging_audio_input_prefers_aac_over_wav(self, tmp_path):
        wav = tmp_path / "chapter.wav"
        aac = tmp_path / "chapter.m4a"
        wav.write_bytes(b"wav")
        aac.write_bytes(b"aac")

        result = type(
            "ChapterResult",
            (),
            {"wav_path": wav, "mp3_path": None, "aac_path": aac},
        )()

        selected = select_packaging_audio_input(result)
        assert selected == aac

    def test_select_packaging_audio_inputs_uses_aac_only_when_all_chapters_have_aac(
        self,
        tmp_path,
    ):
        first_aac = tmp_path / "one.m4a"
        first_wav = tmp_path / "one.wav"
        second_wav = tmp_path / "two.wav"
        for path in (first_aac, first_wav, second_wav):
            path.write_bytes(path.name.encode("utf-8"))

        first = type(
            "ChapterResult",
            (),
            {"wav_path": first_wav, "mp3_path": None, "aac_path": first_aac},
        )()
        second = type(
            "ChapterResult",
            (),
            {"wav_path": second_wav, "mp3_path": None, "aac_path": None},
        )()

        selected = select_packaging_audio_inputs([first, second])

        assert selected == [first_wav, second_wav]

    def test_select_packaging_audio_inputs_rejects_incompatible_mixed_inputs(
        self,
        tmp_path,
    ):
        first_aac = tmp_path / "one.m4a"
        second_wav = tmp_path / "two.wav"
        first_aac.write_bytes(b"aac")
        second_wav.write_bytes(b"wav")

        first = type(
            "ChapterResult",
            (),
            {"wav_path": None, "mp3_path": None, "aac_path": first_aac},
        )()
        second = type(
            "ChapterResult",
            (),
            {"wav_path": second_wav, "mp3_path": None, "aac_path": None},
        )()

        assert select_packaging_audio_inputs([first, second]) == []

    def test_resolve_background_audio_futures_sets_aac_path(self, tmp_path):
        aac = tmp_path / "one.m4a"
        aac.write_bytes(b"aac")
        result = type(
            "ChapterResult",
            (),
            {"wav_path": None, "mp3_path": None, "aac_path": None},
        )()
        future: Future = Future()
        future.set_result(aac)
        result.aac_future = future

        resolve_background_audio_futures([result])

        assert result.aac_path == aac
        assert select_packaging_audio_inputs([result]) == [aac]


class TestStreamCopyDetection:
    def test_audio_files_support_stream_copy_requires_m4a_inputs(self, tmp_path):
        first = tmp_path / "one.m4a"
        second = tmp_path / "two.wav"
        first.write_bytes(b"one")
        second.write_bytes(b"two")

        assert audio_files_support_stream_copy([first]) is True
        assert audio_files_support_stream_copy([first, second]) is False
