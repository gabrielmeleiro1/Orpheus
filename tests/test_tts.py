import threading
import wave
from pathlib import Path

import pytest
import numpy as np
from unittest.mock import patch
from types import SimpleNamespace
import modules.tts.engine as tts_engine_module
import modules.tts.cleaner as cleaner_module
from modules.tts.engine_output import synthesize_to_output_path as synthesize_to_output_path_helper
from modules.tts.engine_output import synthesize_to_output_file as synthesize_to_output_file_helper
from modules.tts.cleaner import TextCleaner
from modules.tts.engine import TTSEngine
from modules.tts.engine_config import TTSConfig
from modules.errors import SynthesisError


def _write_pcm_wav(path, samples):
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(24000)
        wav_file.writeframes(np.array(samples, dtype=np.int16).tobytes())


def _output_engine(tmp_path, **options):
    return SimpleNamespace(
        _model_lock=threading.RLock(),
        _model_path=str(tmp_path),
        _model=object(),
        **options,
    )


def _segment_generator(output_path):
    def fake_generate_audio(**kwargs):  # noqa: ANN001
        target = output_path.with_name(f"{output_path.stem}_000.wav")
        _write_pcm_wav(target, [1, 2, 3])

    return fake_generate_audio


@pytest.fixture(autouse=True)
def _stub_lazy_mlx_runtime(monkeypatch):
    monkeypatch.setattr(cleaner_module.cleaner_mlx_backend, "ensure_mlx_lm_runtime", lambda: None)
    monkeypatch.setattr(tts_engine_module, "_ensure_mlx_runtime", lambda: None)
    monkeypatch.setattr(tts_engine_module, "_ensure_tqdm_thread_lock", lambda: None)


class TestTextCleaner:
    def test_rule_based_cleaning(self):
        cleaner = TextCleaner()
        text = "Dr. Smith went to St. Louis."
        cleaned = cleaner._rule_based_clean(text)
        assert isinstance(cleaned, str)
        assert "Smith" in cleaned
        assert "Louis" in cleaned

    def test_no_cleaning_needed(self):
        cleaner = TextCleaner()
        with patch.object(cleaner, 'load'):
            text = "Simple text."
            assert cleaner.clean(text) == text

class TestTTSEngine:
    def test_init(self):
        engine = TTSEngine()
        assert engine.config.model_name is not None

    def test_init_uses_configured_batch_size_for_memory_manager(self, monkeypatch):
        configs = []

        monkeypatch.setattr(tts_engine_module, "VRAM_MONITOR_AVAILABLE", True)
        monkeypatch.setattr(tts_engine_module, "_ensure_mlx_runtime", lambda: None)
        monkeypatch.setattr(tts_engine_module, "_ensure_tqdm_thread_lock", lambda: None)
        monkeypatch.setattr(
            tts_engine_module,
            "get_memory_manager",
            lambda dynamic_config=None: configs.append(dynamic_config)
            or SimpleNamespace(),
        )

        TTSEngine(TTSConfig(batch_size=9))

        assert configs[0].base_tts_batch_size == 9

    def test_iter_synthesize_batch_refreshes_memory_pressure(self):
        calls = []
        engine = TTSEngine.__new__(TTSEngine)
        engine.config = SimpleNamespace(
            use_batching=True,
            batch_size=4,
            sample_rate=24000,
        )
        engine._memory_manager = SimpleNamespace(
            refresh_memory_pressure=lambda: calls.append("refresh"),
            get_tts_batch_size=lambda: 2,
            get_vram_pressure=lambda: SimpleNamespace(name="NORMAL"),
        )
        engine._batch_pause_total_ms = 0.0
        engine._synthesize_single = lambda item: tts_engine_module.BatchResult(
            audio=np.array([0.1], dtype=np.float32),
            index=item.index,
            error=None,
            duration_ms=1,
        )
        items = [
            tts_engine_module.BatchItem(text="a", voice="af_bella", speed=1.0, index=0),
            tts_engine_module.BatchItem(text="b", voice="af_bella", speed=1.0, index=1),
        ]

        results = list(engine.iter_synthesize_batch(items))

        assert len(results) == 2
        assert calls == ["refresh"]
    
    def test_quantization_aliases_map_to_expected_model_paths(self):
        engine = TTSEngine.__new__(TTSEngine)
        engine.config = SimpleNamespace(model_name="mlx-community/Kokoro-82M", quantization="q8")
        assert engine._get_model_path() == "mlx-community/Kokoro-82M-8bit"

        engine.config = SimpleNamespace(model_name="mlx-community/Kokoro-82M", quantization="q4")
        assert engine._get_model_path() == "mlx-community/Kokoro-82M-4bit"

    def test_load_model_propagates_backend_load_failures(self):
        engine = TTSEngine.__new__(TTSEngine)
        engine.config = SimpleNamespace(model_name="mlx-community/Kokoro-82M", quantization="8bit")
        engine._model_loaded = False
        engine._model = None
        engine._model_path = engine._get_model_path()

        with patch.object(tts_engine_module.engine_runtime, "_get_cached_model", return_value=None), patch.object(
            tts_engine_module.mlx_compat_backend,
            "load_tts_model",
            side_effect=ValueError("shape mismatch"),
            create=True,
        ):
            with pytest.raises(ValueError, match="shape mismatch"):
                engine.load_model()

    def test_engine_output_file_helper_does_not_read_generated_audio(self, tmp_path):
        output_path = tmp_path / "sample.wav"

        def fake_generate_audio(**kwargs):  # noqa: ANN001
            if "output_path" in kwargs:
                target = Path(kwargs["output_path"]) / f"{kwargs['file_prefix']}.wav"
            else:
                target = Path(f"{kwargs['file_prefix']}.wav")
            _write_pcm_wav(target, [1, 2, 3])

        engine = _output_engine(tmp_path)

        written_path = synthesize_to_output_file_helper(
            engine,
            text="Hello world",
            voice="af_bella",
            speed=1.0,
            output_path=output_path,
            generate_fn=fake_generate_audio,
        )

        assert written_path == output_path
        assert output_path.exists()
        with wave.open(str(output_path), "rb") as wav_file:
            assert wav_file.getnframes() == 3

    def test_engine_output_file_helper_replaces_stale_output(self, tmp_path):
        output_path = tmp_path / "sample.wav"
        _write_pcm_wav(output_path, [9])

        fake_generate_audio = _segment_generator(output_path)

        engine = _output_engine(tmp_path)

        written_path = synthesize_to_output_file_helper(
            engine,
            text="Hello world",
            voice="af_bella",
            speed=1.0,
            output_path=output_path,
            generate_fn=fake_generate_audio,
        )

        assert written_path == output_path
        with wave.open(str(output_path), "rb") as wav_file:
            assert wav_file.getnframes() == 3

    def test_engine_output_path_helper_replaces_stale_output_and_consumes_segments(self, tmp_path):
        output_path = tmp_path / "sample.wav"
        _write_pcm_wav(output_path, [9])

        fake_generate_audio = _segment_generator(output_path)

        engine = _output_engine(tmp_path, config=SimpleNamespace(sample_rate=24000))

        audio = synthesize_to_output_path_helper(
            engine,
            text="Hello world",
            voice="af_bella",
            speed=1.0,
            output_path=output_path,
            generate_fn=fake_generate_audio,
        )

        assert np.allclose(audio, np.array([1, 2, 3], dtype=np.float32) / 32768)
        with wave.open(str(output_path), "rb") as wav_file:
            assert np.array_equal(
                np.frombuffer(wav_file.readframes(wav_file.getnframes()), dtype=np.int16),
                np.array([1, 2, 3], dtype=np.int16),
            )
        assert not output_path.with_name("sample_000.wav").exists()

    @pytest.mark.parametrize(
        "output_helper",
        [synthesize_to_output_path_helper, synthesize_to_output_file_helper],
        ids=["samples", "file"],
    )
    def test_output_helpers_abort_when_stale_final_cannot_be_removed(
        self,
        tmp_path,
        monkeypatch,
        output_helper,
    ):
        output_path = tmp_path / "sample.wav"
        _write_pcm_wav(output_path, [9])

        generate_calls = []
        original_unlink = Path.unlink

        def locked_unlink(path, *args, **kwargs):  # noqa: ANN001
            if path == output_path:
                raise PermissionError("output is locked")
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", locked_unlink)
        engine = _output_engine(tmp_path, config=SimpleNamespace(sample_rate=24000))

        with pytest.raises(SynthesisError, match="stale generated audio"):
            output_helper(
                engine,
                text="Hello world",
                voice="af_bella",
                speed=1.0,
                output_path=output_path,
                generate_fn=lambda **kwargs: generate_calls.append(kwargs),
            )

        assert generate_calls == []

    @pytest.mark.parametrize(
        "output_helper",
        [synthesize_to_output_path_helper, synthesize_to_output_file_helper],
        ids=["samples", "file"],
    )
    def test_output_helpers_abort_when_stale_segment_cannot_be_removed(
        self,
        tmp_path,
        monkeypatch,
        output_helper,
    ):
        output_path = tmp_path / "sample.wav"
        segment_path = tmp_path / "sample_000.wav"
        _write_pcm_wav(segment_path, [9])

        generate_calls = []
        original_unlink = Path.unlink

        def locked_unlink(path, *args, **kwargs):  # noqa: ANN001
            if path == segment_path:
                raise OSError("segment is locked")
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", locked_unlink)
        engine = _output_engine(tmp_path, config=SimpleNamespace(sample_rate=24000))

        with pytest.raises(SynthesisError, match="stale generated audio"):
            output_helper(
                engine,
                text="Hello world",
                voice="af_bella",
                speed=1.0,
                output_path=output_path,
                generate_fn=lambda **kwargs: generate_calls.append(kwargs),
            )

        assert generate_calls == []


class TestBlankChunkSynthesis:
    """A chunk that cleans to nothing must not fail the chapter.

    Chunkers emit chunks like "---" or "[1]"; the rule-based cleaner reduces
    both to "". Returning zero-length audio made _batch_result_has_audio false,
    which fails the chapter and then refuses to package the whole book.
    """

    def test_synthesize_returns_audible_silence_for_blank_text(self):
        engine = TTSEngine()

        audio = engine.synthesize("   ")

        assert audio.dtype == np.float32
        assert audio.size > 0
        assert float(np.abs(audio).max()) == 0.0

    def test_synthesize_writes_blank_silence_when_output_path_is_requested(self, tmp_path):
        engine = TTSEngine()
        output_path = tmp_path / "blank.wav"

        audio = engine.synthesize("   ", output_path=output_path)

        assert audio.size > 0
        with wave.open(str(output_path), "rb") as wav_file:
            assert wav_file.getnframes() == audio.size
            assert wav_file.getframerate() == engine.config.sample_rate

    def test_synthesize_to_output_file_writes_non_empty_wav_for_blank_text(self, tmp_path):
        engine = TTSEngine()
        output_path = tmp_path / "blank.wav"

        written_path = engine.synthesize_to_output_file("", output_path)

        assert written_path == output_path
        with wave.open(str(output_path), "rb") as wav_file:
            assert wav_file.getnframes() > 0
            assert wav_file.getframerate() == engine.config.sample_rate


class TestSharedModelLock:
    """A cached model is handed to several engines, so its lock must be shared."""

    def test_engines_sharing_a_cache_key_share_one_model_lock(self):
        first = TTSEngine()
        second = TTSEngine()
        first._model_cache_key = "shared-key"
        second._model_cache_key = "shared-key"

        assert first._model_lock is second._model_lock

    def test_engines_with_different_cache_keys_get_distinct_locks(self):
        first = TTSEngine()
        second = TTSEngine()
        first._model_cache_key = "key-a"
        second._model_cache_key = "key-b"

        assert first._model_lock is not second._model_lock


def _segment_path(kwargs: dict, index: int) -> Path:
    """Resolve the per-segment WAV path from generate_audio's kwargs.

    mlx_audio accepts either an output_path directory plus a file_prefix, or a
    file_prefix that is itself a full path stem.
    """
    prefix = kwargs["file_prefix"]
    base = Path(kwargs["output_path"]) / prefix if "output_path" in kwargs else Path(prefix)
    return base.with_name(f"{base.name}_{index:03d}.wav")


class TestMultiSegmentWavAssembly:
    """Cover the >1 segment branch of file-backed chapter synthesis.

    Existing tests only exercise the single-file and single-segment paths, so a
    regression that dropped later segments would silently truncate every
    multi-segment chapter to its first few seconds.
    """

    @staticmethod
    def _write_wav(path: Path, samples: np.ndarray, sample_rate: int = 24000) -> None:
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(samples.astype(np.int16).tobytes())

    def test_collects_every_generated_segment_in_order(self, tmp_path):
        output_path = tmp_path / "chapter.wav"
        segment_values = [11, 22, 33]
        frames_per_segment = 5

        def fake_generate_audio(**kwargs):  # noqa: ANN001
            for index, value in enumerate(segment_values):
                segment = _segment_path(kwargs, index)
                self._write_wav(
                    segment,
                    np.full(frames_per_segment, value, dtype=np.int16),
                )

        engine = _output_engine(tmp_path)

        written_path = synthesize_to_output_file_helper(
            engine,
            text="Hello world",
            voice="af_bella",
            speed=1.0,
            output_path=output_path,
            generate_fn=fake_generate_audio,
        )

        assert written_path == output_path
        with wave.open(str(output_path), "rb") as wav_file:
            assert wav_file.getnframes() == frames_per_segment * len(segment_values)
            frames = np.frombuffer(
                wav_file.readframes(wav_file.getnframes()), dtype=np.int16
            )
        # Order matters: a reordered concat would still have the right length.
        assert list(frames) == [
            value for value in segment_values for _ in range(frames_per_segment)
        ]
        # Segment files must not be left behind for the next run to pick up.
        assert sorted(output_path.parent.glob("chapter_*.wav")) == []

    def test_rejects_segments_with_mismatched_audio_parameters(self, tmp_path):
        output_path = tmp_path / "chapter.wav"

        def fake_generate_audio(**kwargs):  # noqa: ANN001
            self._write_wav(_segment_path(kwargs, 0), np.ones(4, dtype=np.int16))
            self._write_wav(
                _segment_path(kwargs, 1),
                np.ones(4, dtype=np.int16),
                sample_rate=48000,
            )

        engine = _output_engine(tmp_path)

        with pytest.raises(SynthesisError):
            synthesize_to_output_file_helper(
                engine,
                text="Hello world",
                voice="af_bella",
                speed=1.0,
                output_path=output_path,
                generate_fn=fake_generate_audio,
            )
