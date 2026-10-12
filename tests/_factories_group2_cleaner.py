"""Shared loaded-cleaner setup for guard and latency tests."""

import threading

from modules.tts.cleaner import TextCleaner
from modules.tts.cleaner_config import CleanerConfig

from tests.fixtures.fake_cleaner_backend import attach_cleaner_backend_strategy


def make_loaded_cleaner(backend: str = "mlx") -> TextCleaner:
    cleaner = TextCleaner.__new__(TextCleaner)
    cleaner.config = CleanerConfig(
        backend=backend,
        api_key_env="TEST_OPENAI_API_KEY",
        api_model="gpt-5-nano",
        api_base_url="https://api.openai.com/v1",
    )
    cleaner._loaded = True
    cleaner._model = object()
    cleaner._tokenizer = object()
    cleaner._manager = None
    cleaner._model_lock = threading.RLock()
    cleaner._total_chunks_cleaned = 0
    cleaner._total_time_ms = 0
    cleaner._llm_success_count = 0
    cleaner._llm_fallback_count = 0
    cleaner._rule_only_count = 0
    return attach_cleaner_backend_strategy(cleaner)
