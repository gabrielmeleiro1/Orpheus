import threading
import wave
from pathlib import Path

import pytest
import numpy as np
from unittest.mock import MagicMock, patch
from types import SimpleNamespace
import modules.tts.engine as tts_engine_module
import modules.tts.cleaner as cleaner_module
from modules.tts.engine_output import synthesize_to_file as synthesize_to_file_helper
from modules.tts.engine_output import synthesize_to_output_file as synthesize_to_output_file_helper
from modules.tts.chunker import TextChunker, ChunkConfig
from modules.tts.cleaner import TextCleaner
from modules.tts.engine import TTSEngine
from modules.tts.engine_config import TTSConfig


@pytest.fixture(autouse=True)
def _stub_lazy_mlx_runtime(monkeypatch):
    monkeypatch.setattr(cleaner_module.cleaner_mlx_backend, "ensure_mlx_lm_runtime", lambda: None)
    monkeypatch.setattr(tts_engine_module, "_ensure_mlx_runtime", lambda: None)
    monkeypatch.setattr(tts_engine_module, "_ensure_tqdm_thread_lock", lambda: None)


class TestTextChunker:
    def test_chunking(self):
        config = ChunkConfig(max_tokens=10)
        chunker = TextChunker(config)
        text = "This is sentence one. This is sentence two. This is sentence three."
        chunks = chunker.chunk(text)
        assert len(chunks) > 0
        assert all(isinstance(c, str) for c in chunks)
    
    def test_empty_text(self):
        chunker = TextChunker()
        assert chunker.chunk("") == []

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
            record_activity=lambda: calls.append("activity"),
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
        assert calls == ["activity", "refresh"]
    
    def test_synthesize(self):
        engine = TTSEngine()
        expected = np.array([0.1, 0.2, 0.3], dtype=np.float32)

        with patch.object(engine, "_synthesize_once", return_value=expected):
            audio = engine.synthesize("Hello world", voice="af_bella", speed=1.0)

        assert audio is not None
        assert np.array_equal(audio, expected)

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

        with patch.object(tts_engine_module, "_get_cached_model", return_value=None), patch.object(
            tts_engine_module.mlx_compat_backend,
            "load_tts_model",
            side_effect=ValueError("shape mismatch"),
            create=True,
        ):
            with pytest.raises(ValueError, match="shape mismatch"):
                engine.load_model()

    def test_engine_output_helper_writes_wav_file(self, tmp_path):
        engine = SimpleNamespace(
            config=SimpleNamespace(sample_rate=24000),
            synthesize=MagicMock(return_value=np.array([0.1, 0.2, 0.3], dtype=np.float32)),
        )
        output_path = tmp_path / "sample.wav"

        written_path = synthesize_to_file_helper(
            engine,
            text="Hello world",
            output_path=output_path,
            voice="af_bella",
            speed=1.0,
        )

        assert written_path == output_path
        assert output_path.exists()
        engine.synthesize.assert_called_once_with("Hello world", "af_bella", 1.0)

    def test_engine_output_file_helper_does_not_read_generated_audio(self, tmp_path):
        output_path = tmp_path / "sample.wav"

        def fake_generate_audio(**kwargs):  # noqa: ANN001
            if "output_path" in kwargs:
                target = Path(kwargs["output_path"]) / f"{kwargs['file_prefix']}.wav"
            else:
                target = Path(f"{kwargs['file_prefix']}.wav")
            with wave.open(str(target), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(24000)
                wav_file.writeframes((np.array([1, 2, 3], dtype=np.int16)).tobytes())

        engine = SimpleNamespace(
            _model_lock=threading.RLock(),
            _model_path="model-path",
            _model=object(),
        )

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
        with wave.open(str(output_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(24000)
            wav_file.writeframes((np.array([9], dtype=np.int16)).tobytes())

        def fake_generate_audio(**kwargs):  # noqa: ANN001
            target = output_path.with_name(f"{output_path.stem}_000.wav")
            with wave.open(str(target), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(24000)
                wav_file.writeframes((np.array([1, 2, 3], dtype=np.int16)).tobytes())

        engine = SimpleNamespace(
            _model_lock=threading.RLock(),
            _model_path="model-path",
            _model=object(),
        )

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
