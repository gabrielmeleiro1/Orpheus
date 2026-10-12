"""Local CUDA cleaning stops decoding when the conversion is cancelled."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from modules.tts import cleaner_cuda

torch = pytest.importorskip("torch")
pytest.importorskip("transformers")


class _Tokenizer:
    chat_template = None
    pad_token_id = 0
    eos_token_id = 0

    def __call__(self, prompt, return_tensors):
        return {"input_ids": torch.zeros((1, 4), dtype=torch.long)}

    def decode(self, ids, skip_special_tokens):
        return "x" * int(ids.shape[-1])


class _Model:
    def __init__(self, cancel_after_steps: int, cancel: threading.Event) -> None:
        self.cancel_after_steps = cancel_after_steps
        self.cancel = cancel
        self.steps = 0

    def parameters(self):
        yield torch.zeros(1)

    def generate(self, *, input_ids, max_new_tokens, stopping_criteria=None, **kwargs):
        ids = input_ids
        for _ in range(max_new_tokens):
            self.steps += 1
            if self.steps == self.cancel_after_steps:
                self.cancel.set()
            ids = torch.cat([ids, torch.ones((1, 1), dtype=torch.long)], dim=1)
            if stopping_criteria is not None and bool(stopping_criteria(ids, None).all()):
                break
        return ids


def _cleaner(cancel: threading.Event, cancel_after_steps: int):
    return SimpleNamespace(
        _model_lock=threading.RLock(),
        _tokenizer=_Tokenizer(),
        _model=_Model(cancel_after_steps, cancel),
        config=SimpleNamespace(max_tokens=2_000),
        generation_cancel_requested=cancel.is_set,
    )


def test_generation_stops_at_the_next_token_after_cancel(monkeypatch):
    monkeypatch.setattr(cleaner_cuda, "ensure_cuda_runtime", lambda: (torch,))
    cancel = threading.Event()
    cleaner = _cleaner(cancel, cancel_after_steps=5)

    output = cleaner_cuda.generate_cuda_response(cleaner, "prompt")

    assert cleaner._model.steps == 5
    assert len(output) == 5


def test_generation_without_cancellation_runs_to_completion(monkeypatch):
    monkeypatch.setattr(cleaner_cuda, "ensure_cuda_runtime", lambda: (torch,))
    cancel = threading.Event()
    cleaner = _cleaner(cancel, cancel_after_steps=-1)
    cleaner.generation_cancel_requested = None

    cleaner_cuda.generate_cuda_response(cleaner, "prompt")

    assert cleaner._model.steps == 2_000


def test_batch_cleaning_exposes_cancellation_only_while_running():
    from modules.tts.cleaner import TextCleaner
    from modules.tts.cleaner_config import CleanerConfig

    cleaner = TextCleaner(CleanerConfig(backend="basic"))
    seen: list[object] = []
    original = cleaner.clean_with_trace

    def recording(text):
        seen.append(cleaner.generation_cancel_requested)
        return original(text)

    cleaner.clean_with_trace = recording
    requested = threading.Event().is_set
    cleaner.clean_chunks_with_trace(["One chunk.", "Two chunk."], cancel_requested=requested)

    assert seen == [requested, requested]
    assert cleaner.generation_cancel_requested is None
