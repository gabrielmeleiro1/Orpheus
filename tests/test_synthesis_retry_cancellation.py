"""Serial recovery must respect cancellation just like the normal TTS iterator."""

from types import SimpleNamespace

import numpy as np
import pytest

from modules.pipeline.retry import retry_batch_results_serially
from modules.tts.strategies.base import SynthesisItem, SynthesisResult


@pytest.mark.parametrize("cancel_before_retry", [True, False])
def test_serial_recovery_stops_on_cancellation(cancel_before_retry):
    cancelled = cancel_before_retry
    spoken = []
    progress = []

    def synthesize(*, text, voice, speed):
        nonlocal cancelled
        spoken.append(text)
        cancelled = True
        return np.ones(24, dtype=np.float32)

    runtime = SimpleNamespace(
        check_cancelled=lambda: cancelled,
        tts_engine=SimpleNamespace(synthesize=synthesize, sample_rate=24000),
        log_verbose=lambda *args: None,
    )
    items = [
        SynthesisItem(text=text, voice="af_bella", speed=1.0, index=index)
        for index, text in enumerate(["first", "second", "third"])
    ]
    failed = [SynthesisResult(audio=None, index=item.index, error="batch failure") for item in items]

    recovered = retry_batch_results_serially(
        runtime=runtime,
        batch_items=items,
        batch_results=failed,
        total_chunks=len(items),
        on_batch_progress=lambda *args: progress.append(args),
    )

    assert spoken == ([] if cancel_before_retry else ["first"])
    assert len(recovered) == (0 if cancel_before_retry else 1)
    assert progress == []
