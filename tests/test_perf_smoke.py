"""Optional deterministic performance smoke tests."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from modules.app.controller import AppController


@pytest.mark.benchmark
class TestPerformanceSmoke:
    def test_runtime_validation_cache_smoke(self, tmp_path: Path) -> None:
        class _FakeStrategy:
            def __init__(self):
                self._loaded = False
                self.supported_voices = [type("V", (), {"id": "am_adam"})()]

            @property
            def is_loaded(self):
                return self._loaded

            def load(self):
                self._loaded = True
                time.sleep(0.01)

            def unload(self, keep_in_cache=True):
                _ = keep_in_cache
                self._loaded = False

            def synthesize(self, text, voice, speed=1.0):
                _ = (text, voice, speed)
                time.sleep(0.01)
                return [0.0]

        controller = AppController(config=None)

        with patch("modules.app.controller.TTSEngineFactory.create", return_value=_FakeStrategy()):
            t0 = time.perf_counter()
            controller._validate_tts_runtime("kokoro", "bf16")
            first_ms = (time.perf_counter() - t0) * 1000

            t1 = time.perf_counter()
            controller._validate_tts_runtime("kokoro", "bf16")
            second_ms = (time.perf_counter() - t1) * 1000

        assert first_ms > 5.0
        assert second_ms < first_ms * 0.5
