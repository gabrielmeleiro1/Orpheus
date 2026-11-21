"""Regression tests for the text cleaner stack and runtime-setup helpers."""

from __future__ import annotations

import asyncio
from collections import Counter
import random
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from modules.ingestion.text_layout import hyphenation_word_forms
from modules.tts import cleaner_edits
from modules.tts.cleaner_rules import rule_based_clean


# ---------------------------------------------------------------------------
# LLM edit alignment
# ---------------------------------------------------------------------------


def _reference_align_block(source, output, evidence):
    """The exhaustive alignment search, kept verbatim as the reference."""
    n, m = len(source), len(output)
    if (n + 1) * (m + 1) > cleaner_edits._MAX_ALIGNMENT_CELLS:
        raise ValueError("LLM preservation guard could not align a large changed span")
    penalty = n + m + 1
    costs = [[float("inf")] * (m + 1) for _ in range(n + 1)]
    paths = {}
    costs[0][0] = 0

    def offer(i, j, a, b, allowed, cost):
        candidate = costs[i][j] + cost
        if candidate < costs[a][b]:
            costs[a][b] = candidate
            paths[a, b] = cleaner_edits._Edit(i, a, j, b, allowed)

    for i in range(n + 1):
        for j in range(m + 1):
            if i < n and j < m:
                before, after = source[i].word, output[j].word
                if before.casefold() == after.casefold():
                    offer(i, j, i + 1, j + 1, True, 0)
                elif after.casefold() in source[i].expansions or cleaner_edits._leading_restoration(
                    source[i], output[j], evidence,
                ):
                    offer(i, j, i + 1, j + 1, True, 1)
                else:
                    offer(i, j, i + 1, j + 1, False, penalty)
                boundary = cleaner_edits._boundary_repair_end(source, output, i, j, evidence)
                if boundary:
                    offer(i, j, *boundary, True, 1)
            if i < n:
                allowed = source[i].furniture or source[i].drop_cap
                offer(i, j, i + 1, j, allowed, 1 if allowed else penalty)
            if j < m:
                offer(i, j, i, j + 1, False, penalty)

    edits = []
    i, j = n, m
    while i or j:
        edit = paths[i, j]
        edits.append(edit)
        i, j = edit.source_start, edit.output_start
    return list(reversed(edits))


def _alignment_inputs(source_text, output_text):
    source = cleaner_edits._tokens(source_text)
    output = cleaner_edits._tokens(output_text)
    evidence = cleaner_edits._Evidence(
        Counter(token.word.casefold() for token in source),
        hyphenation_word_forms(source_text),
        frozenset(token.word for token in source if token.drop_cap),
    )
    return source, output, evidence


_VOCABULARY = (
    "the a of and to in he she it was had said Dr. St. Mr. inter- esting com- puter "
    "book chapter river night light the the a a I me my 12 1914 XIV NASA well-known "
    "Thomas Hardy London morning ever- y- thing re- turn"
).split()


def _random_text(rng: random.Random) -> str:
    words = [rng.choice(_VOCABULARY) for _ in range(rng.randint(0, 60))]
    lines, line = [], []
    for word in words:
        line.append(word)
        if rng.random() < 0.08:
            lines.append(" ".join(line))
            line = []
    lines.append(" ".join(line))
    if rng.random() < 0.2:
        lines.insert(rng.randint(0, len(lines)), str(rng.randint(1, 300)))
    if rng.random() < 0.15:
        lines.insert(0, rng.choice("TAOW"))
    return "\n".join(lines)


def _random_edit(rng: random.Random, text: str) -> str:
    words = text.split()
    for _ in range(rng.randint(0, 4)):
        roll = rng.random()
        if not words:
            words.append(rng.choice(_VOCABULARY))
            continue
        index = rng.randrange(len(words))
        if roll < 0.25 and index + 1 < len(words):
            # Join a word with its neighbour (hyphenation repair or a fused pair).
            words[index:index + 2] = [words[index].rstrip("-") + words[index + 1]]
        elif roll < 0.4:
            del words[index]
        elif roll < 0.55:
            words.insert(index, rng.choice(_VOCABULARY))
        elif roll < 0.7:
            words[index] = rng.choice(_VOCABULARY)
        elif roll < 0.8:
            words[index] = {"Dr.": "Doctor", "St.": "Saint", "Mr.": "Mister"}.get(words[index], words[index])
        elif roll < 0.9 and len(words[index]) > 2:
            words[index:index + 1] = [words[index][:2], words[index][2:]]
        else:
            words[index] = words[index].upper()
    return " ".join(words)


def _differential_cases():
    rng = random.Random(20261123)
    cases = [
        ("", ""),
        ("", "one two"),
        ("one two", ""),
        ("one", "one"),
        ("inter- esting book", "interesting book"),
        ("the the the the", "the the"),
        ("12\nthe river\n13\nthe river", "the river the river"),
        ("T\nhe river ran", "The river ran"),
        ("Dr. Hardy met Mr. Hardy", "Doctor Hardy met Mister Hardy"),
    ]
    for _ in range(400):
        source = _random_text(rng)
        cases.append((source, _random_edit(rng, source)))
    for _ in range(30):
        cases.append((_random_text(rng), _random_text(rng)))
    return cases


def test_bounded_alignment_matches_exhaustive_search(monkeypatch):
    cases = _differential_cases()
    actual = [
        cleaner_edits._align(*_alignment_inputs(source, output)) for source, output in cases
    ]
    monkeypatch.setattr(cleaner_edits, "_align_block", _reference_align_block)
    expected = [
        cleaner_edits._align(*_alignment_inputs(source, output)) for source, output in cases
    ]
    assert actual == expected


def test_bounded_alignment_matches_exhaustive_search_on_unrelated_long_blocks(monkeypatch):
    rng = random.Random(7)
    source = " ".join(rng.choice(_VOCABULARY) for _ in range(150))
    output = " ".join(rng.choice(_VOCABULARY) for _ in range(150))
    actual = cleaner_edits._align(*_alignment_inputs(source, output))
    monkeypatch.setattr(cleaner_edits, "_align_block", _reference_align_block)
    assert actual == cleaner_edits._align(*_alignment_inputs(source, output))


def test_localized_edit_alignment_visits_only_a_narrow_band(monkeypatch):
    rng = random.Random(3)
    words = [rng.choice(("river", "night", "light", "book", "the")) for _ in range(400)]
    words[200:202] = ["inter-", "esting"]
    source = " ".join(words)
    output = source.replace("inter- esting", "interesting")
    calls = 0
    original = cleaner_edits._boundary_repair_end

    def counting(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(cleaner_edits, "_boundary_repair_end", counting)
    result = cleaner_edits.reconcile_llm_edits(source, output)

    assert result.text == output
    assert result.rejected_edits == []
    # The exhaustive search inspects every one of the ~160k cell pairs.
    assert calls < 20 * len(words)


# ---------------------------------------------------------------------------
# Rule-based cleaning
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("See ![Diagram](img.png) here.", "See Diagram here."),
        ("See ![](img.png) here.", "See here."),
        ("A [link](http://example.com) and ![Chart](c.png).", "A link and Chart."),
    ],
)
def test_markdown_images_keep_alt_text_without_stray_bang(text, expected):
    assert rule_based_clean(text) == expected


# ---------------------------------------------------------------------------
# API cleaner cancellation and the global limiter
# ---------------------------------------------------------------------------


class _CountingLimiter:
    """Records permits; every release after the first is slow."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.acquired = 0
        self.released = 0

    def acquire(self, block=True, timeout=None):
        with self._lock:
            self.acquired += 1
        return True

    def release(self) -> None:
        with self._lock:
            slow = self.released > 0
        if slow:
            time.sleep(0.2)
        with self._lock:
            self.released += 1


def _api_cleaner_holding_permits(limiter):
    from modules.tts.api_rate_limit import acquire_global_limiter_async
    from modules.tts.cleaner import TextCleaner
    from modules.tts.cleaner_config import CleanerConfig

    cleaner = TextCleaner(CleanerConfig(backend="api", api_key="test-key"))
    cleaner._get_api_aiohttp_module = lambda: None

    async def request(text, semaphore, *, api_session=None):
        async with semaphore:
            async with acquire_global_limiter_async(limiter):
                await asyncio.sleep(30)

    cleaner._clean_with_trace_api_async = request
    return cleaner


def test_cancelled_api_batch_returns_only_after_every_permit_is_released():
    limiter = _CountingLimiter()
    cleaner = _api_cleaner_holding_permits(limiter)
    cancelled = threading.Event()

    async def run():
        asyncio.get_running_loop().call_later(0.2, cancelled.set)
        traces = await cleaner.clean_batch_with_trace_async(
            [f"Chunk {index}." for index in range(8)], cancel_requested=cancelled.is_set,
        )
        return traces, limiter.acquired, limiter.released

    began = time.monotonic()
    traces, acquired, released = asyncio.run(run())

    assert traces == [None] * 8
    assert acquired == cleaner._get_api_batch_concurrency()
    assert released == acquired
    assert time.monotonic() - began < 5


def test_failed_api_batch_stops_and_drains_sibling_requests():
    limiter = _CountingLimiter()
    cleaner = _api_cleaner_holding_permits(limiter)
    holding = cleaner._clean_with_trace_api_async

    async def request(text, semaphore, *, api_session=None):
        if text == "fail":
            await asyncio.sleep(0.1)
            raise RuntimeError("synthetic failure")
        return await holding(text, semaphore, api_session=api_session)

    cleaner._clean_with_trace_api_async = request

    async def run():
        with pytest.raises(RuntimeError, match="synthetic failure"):
            await cleaner.clean_batch_with_trace_async(["a", "b", "fail"])
        return limiter.acquired, limiter.released

    acquired, released = asyncio.run(run())
    assert acquired == 2
    assert released == 2


def test_limiter_release_finishes_before_repeated_cancellation_propagates():
    from modules.tts.api_rate_limit import acquire_global_limiter_async

    release_started = threading.Event()
    release_finished = threading.Event()

    class _SlowRelease:
        def acquire(self, block=True, timeout=None):
            return True

        def release(self):
            release_started.set()
            time.sleep(0.3)
            release_finished.set()

    async def hold():
        async with acquire_global_limiter_async(_SlowRelease()):
            pass

    async def run():
        task = asyncio.create_task(hold())
        while not release_started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return release_finished.is_set()

    assert asyncio.run(run()) is True


# ---------------------------------------------------------------------------
# Local cleaner model cache, locking and cancellation
# ---------------------------------------------------------------------------


def _bare_cleaner(**config):
    defaults = {
        "model_name": "local-cleaner", "cache_model": True,
        "cuda_device": "cuda", "cuda_quantization": "4bit",
    }
    defaults.update(config)
    return SimpleNamespace(
        config=SimpleNamespace(**defaults),
        _loaded=False, _model=None, _tokenizer=None, _model_lock=threading.RLock(),
    )


@pytest.fixture
def mlx_backend(monkeypatch):
    from modules.tts import cleaner_mlx

    monkeypatch.setattr(cleaner_mlx, "_cleaner_model_cache", {})
    monkeypatch.setattr(cleaner_mlx, "_cleaner_model_locks", {})
    monkeypatch.setattr(cleaner_mlx, "ensure_mlx_lm_runtime", lambda: (None,) * 4)
    monkeypatch.setattr(cleaner_mlx, "installed_model_path", lambda name: name)
    monkeypatch.setattr(cleaner_mlx, "mx", None)
    return cleaner_mlx


@pytest.fixture
def cuda_backend(monkeypatch):
    from modules.tts import cleaner_cuda

    monkeypatch.setattr(cleaner_cuda, "_cleaner_model_cache", {})
    monkeypatch.setattr(cleaner_cuda, "_cleaner_model_locks", {})
    monkeypatch.setattr(cleaner_cuda, "ensure_cuda_runtime", lambda: (SimpleNamespace(),))
    monkeypatch.setattr(cleaner_cuda, "installed_model_path", lambda name: name)
    monkeypatch.setattr(cleaner_cuda, "torch", None)
    return cleaner_cuda


def _strategy(module):
    return module.CleanerCUDAStrategy() if hasattr(module, "CleanerCUDAStrategy") else module.CleanerMLXStrategy()


def _install_loader(module, monkeypatch, on_load=None):
    def make_model(name):
        if on_load is not None:
            on_load()
        return SimpleNamespace(name=name), object()

    if hasattr(module, "CleanerCUDAStrategy"):
        monkeypatch.setattr(module, "_load_cuda_model", lambda cleaner, torch_module: make_model(cleaner.config.model_name))
    else:
        monkeypatch.setattr(module, "load", lambda path, **kwargs: make_model(path))


@pytest.mark.parametrize("backend", ["mlx_backend", "cuda_backend"])
def test_cleaners_sharing_a_cached_model_share_its_use_lock(request, monkeypatch, backend):
    module = request.getfixturevalue(backend)
    _install_loader(module, monkeypatch)
    strategy = _strategy(module)
    first, second, other = _bare_cleaner(), _bare_cleaner(), _bare_cleaner(model_name="other")

    strategy.load(first)
    strategy.load(second)
    assert second._model is first._model
    assert first._model_lock is second._model_lock

    strategy.unload(other)
    strategy.load(other)
    assert other._model_lock is not first._model_lock


@pytest.mark.parametrize("backend", ["mlx_backend", "cuda_backend"])
def test_switching_cleaner_models_frees_the_cached_model_before_loading(request, monkeypatch, backend):
    module = request.getfixturevalue(backend)
    cache_sizes = []
    _install_loader(module, monkeypatch, on_load=lambda: cache_sizes.append(len(module._cleaner_model_cache)))
    strategy = _strategy(module)

    strategy.load(_bare_cleaner(model_name="first"))
    strategy.load(_bare_cleaner(model_name="second"))

    assert cache_sizes == [0, 0]
    assert len(module._cleaner_model_cache) == 1


@pytest.mark.parametrize("backend", ["mlx_backend", "cuda_backend"])
def test_unload_without_cache_keeps_another_cleaners_cached_model(request, monkeypatch, backend):
    module = request.getfixturevalue(backend)
    _install_loader(module, monkeypatch)
    strategy = _strategy(module)

    uncached = _bare_cleaner(model_name="first", cache_model=False)
    strategy.load(uncached)
    cached = _bare_cleaner(model_name="second")
    strategy.load(cached)
    strategy.unload(uncached, keep_in_cache=False)
    assert len(module._cleaner_model_cache) == 1

    strategy.unload(cached, keep_in_cache=False)
    assert module._cleaner_model_cache == {}


def _scripted_local_cleaner(monkeypatch, cancel_after: int):
    from modules.tts import cleaner_mlx
    from modules.tts.cleaner import TextCleaner
    from modules.tts.cleaner_config import CleanerConfig

    monkeypatch.setattr(cleaner_mlx, "ensure_mlx_lm_runtime", lambda: (None,) * 4)
    cleaner = TextCleaner(CleanerConfig(backend="mlx"))
    cleaned: list[str] = []
    seen_cancel: list[object] = []

    def clean_with_trace(text):
        cleaned.append(text)
        seen_cancel.append(cleaner.generation_cancel_requested)
        return SimpleNamespace(cleaned_text=text.upper(), mode="llm_success")

    cleaner.clean_with_trace = clean_with_trace
    return cleaner, cleaned, seen_cancel, (lambda: len(cleaned) >= cancel_after)


def test_local_async_batch_honours_cancellation_and_keeps_indices(monkeypatch):
    cleaner, cleaned, seen_cancel, cancel = _scripted_local_cleaner(monkeypatch, cancel_after=2)

    traces = asyncio.run(
        cleaner.clean_batch_with_trace_async(["a", "b", "c", "d"], cancel_requested=cancel)
    )

    assert cleaned == ["a", "b"]
    assert [trace and trace.cleaned_text for trace in traces] == ["A", "B", None, None]
    # Token-level decoding sees the same predicate while a chunk is generating.
    assert seen_cancel == [cancel, cancel]
    assert cleaner.generation_cancel_requested is None


def test_cancelled_sync_batch_keeps_each_trace_at_its_chunk_index(monkeypatch):
    cleaner, cleaned, _seen, cancel = _scripted_local_cleaner(monkeypatch, cancel_after=1)
    # Length sorting cleans the shortest chunk ("bb" at index 1) first.
    traces = cleaner.clean_chunks_with_trace(["cccc", "bb", "ddddd"], cancel_requested=cancel)

    assert cleaned == ["bb"]
    assert len(traces) == 3
    assert traces[0] is None and traces[2] is None
    assert traces[1].cleaned_text == "BB"


# ---------------------------------------------------------------------------
# CUDA cleaner loading details
# ---------------------------------------------------------------------------


def _capture_cuda_load(monkeypatch, *, device: str, quantization: str):
    from modules.tts import cleaner_cuda

    observed: dict[str, object] = {}

    def from_pretrained(path, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(pad_token="pad", eos_token="eos")

    loader = SimpleNamespace(from_pretrained=from_pretrained)
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(
        AutoTokenizer=loader, AutoModelForCausalLM=loader, BitsAndBytesConfig=lambda **kw: kw,
    ))
    monkeypatch.setattr(cleaner_cuda, "installed_model_path", lambda name: name)
    cleaner = SimpleNamespace(config=SimpleNamespace(
        model_name="model", cuda_device=device, cuda_quantization=quantization,
    ))
    cleaner_cuda._load_cuda_model(cleaner, SimpleNamespace(float16="float16"))
    return observed


def test_default_cuda_device_pins_the_cleaner_to_one_gpu(monkeypatch):
    assert _capture_cuda_load(monkeypatch, device="cuda", quantization="fp16")["device_map"] == {"": "cuda:0"}
    assert _capture_cuda_load(monkeypatch, device="cuda:1", quantization="fp16")["device_map"] == {"": "cuda:1"}


def test_four_bit_cuda_cleaner_computes_in_half_precision(monkeypatch):
    config = _capture_cuda_load(monkeypatch, device="cuda", quantization="4bit")["quantization_config"]
    assert config["bnb_4bit_compute_dtype"] == "float16"
    eight_bit = _capture_cuda_load(monkeypatch, device="cuda", quantization="8bit")["quantization_config"]
    assert "bnb_4bit_compute_dtype" not in eight_bit


def test_cuda_runtime_unavailable_message_is_not_repeated(monkeypatch):
    from modules.tts import cleaner_cuda
    from modules.tts.cleaner_errors import CleanerRuntimeUnavailable

    monkeypatch.setattr(cleaner_cuda, "_TORCH_CUDA_RUNTIME_CHECKED", True)
    monkeypatch.setattr(cleaner_cuda, "TORCH_CUDA_AVAILABLE", False)
    monkeypatch.setattr(cleaner_cuda, "TORCH_CUDA_IMPORT_ERROR", ImportError("No module named 'torch'"))

    with pytest.raises(CleanerRuntimeUnavailable) as raised:
        cleaner_cuda.CleanerCUDAStrategy().load(_bare_cleaner())
    assert str(raised.value).count("CUDA cleaner runtime unavailable") == 1
    assert "No module named 'torch'" in str(raised.value)


@pytest.mark.parametrize(
    ("module_name", "checked", "importer"),
    [
        ("modules.tts.cleaner_mlx", "_MLX_LM_RUNTIME_CHECKED", "_import_mlx_lm_runtime"),
        ("modules.tts.cleaner_cuda", "_TORCH_CUDA_RUNTIME_CHECKED", "_import_cuda_runtime"),
    ],
)
def test_runtime_import_runs_once_under_concurrent_first_use(monkeypatch, module_name, checked, importer):
    import importlib

    module = importlib.import_module(module_name)
    monkeypatch.setattr(module, checked, False)
    calls = []

    def slow_import():
        calls.append(threading.get_ident())
        time.sleep(0.05)
        monkeypatch.setattr(module, checked, True)

    monkeypatch.setattr(module, importer, slow_import)
    ensure = module.ensure_mlx_lm_runtime if "mlx" in module_name else module.ensure_cuda_runtime
    threads = [threading.Thread(target=lambda: _swallow(ensure)) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(calls) == 1


def _swallow(function):
    try:
        function()
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# Cleaner model resolution
# ---------------------------------------------------------------------------


def test_cuda_cleaner_model_is_kept_for_non_mlx_backends():
    from modules.tts.cleaner_config import (
        CLEANER_MLX_DEFAULT_MODEL,
        CUDA_CLEANER_MODEL_CHOICES,
        resolve_cleaner_model_name,
    )

    cuda_model = CUDA_CLEANER_MODEL_CHOICES[0]
    assert resolve_cleaner_model_name(backend="api", configured_model=cuda_model) == cuda_model
    assert resolve_cleaner_model_name(backend="mlx", configured_model=cuda_model) == CLEANER_MLX_DEFAULT_MODEL


# ---------------------------------------------------------------------------
# Setup lock
# ---------------------------------------------------------------------------


def _write_lock_owner(lock_path, *, pid, created_at):
    import json

    lock_path.mkdir(parents=True)
    (lock_path / "owner.json").write_text(
        json.dumps({"pid": pid, "created_at": created_at, "token": "0" * 32}), encoding="utf-8",
    )


def _exited_pid() -> int:
    import subprocess

    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=30)
    return process.pid


def test_dead_owner_lock_is_recovered_even_after_the_clock_stepped_back(tmp_path):
    from modules.runtime_setup import state

    state_path = tmp_path / "runtime-setup.json"
    lock_path = state_path.with_name(f"{state_path.name}.lock")
    _write_lock_owner(lock_path, pid=_exited_pid(), created_at=time.time() + 3600)

    with state.SetupLock(state_path, timeout_seconds=0.2):
        assert lock_path.is_dir()
    assert not lock_path.exists()


def test_ownerless_future_dated_lock_stays_held(tmp_path):
    from modules.runtime_setup import state

    state_path = tmp_path / "runtime-setup.json"
    lock_path = state_path.with_name(f"{state_path.name}.lock")
    lock_path.mkdir()
    future = time.time() + 3600
    import os

    os.utime(lock_path, (future, future))

    with pytest.raises(state.SetupExecutionError, match="already running"):
        with state.SetupLock(state_path, timeout_seconds=0.1):
            pytest.fail("An ambiguous lock must stay held")
    assert lock_path.is_dir()


def test_zero_timeout_lock_release_survives_brief_gate_contention(tmp_path):
    from modules.runtime_setup import state

    lock = state.SetupLock(tmp_path / "runtime-setup.json", timeout_seconds=0)
    lock.__enter__()
    gate_held = threading.Event()
    release_gate = threading.Event()

    def contend():
        with state.SetupLock(tmp_path / "runtime-setup.json", timeout_seconds=1)._metadata_gate(
            time.monotonic() + 1,
        ):
            gate_held.set()
            release_gate.wait(1)

    contender = threading.Thread(target=contend)
    contender.start()
    assert gate_held.wait(1)
    threading.Timer(0.05, release_gate.set).start()
    try:
        lock.__exit__(None, None, None)
    finally:
        contender.join()
    assert not lock.path.exists()


def test_windows_process_probe_declares_handle_sized_signatures(monkeypatch):
    import ctypes
    from ctypes import wintypes

    from modules.runtime_setup import state

    calls = []

    class _Function:
        def __init__(self, name, result):
            self.name, self.result = name, result
            self.argtypes = self.restype = None

        def __call__(self, *args):
            calls.append((self.name, args))
            if self.name == "GetExitCodeProcess":
                args[1]._obj.value = 259
            return self.result

    handle = 0x7FFF_0000_1234
    kernel32 = SimpleNamespace(
        OpenProcess=_Function("OpenProcess", handle),
        GetExitCodeProcess=_Function("GetExitCodeProcess", 1),
        CloseHandle=_Function("CloseHandle", 1),
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel32, raising=False)
    state._windows_process_api.cache_clear()
    try:
        assert state._windows_pid_is_running(4242) is True
    finally:
        state._windows_process_api.cache_clear()

    assert kernel32.OpenProcess.restype is wintypes.HANDLE
    assert kernel32.OpenProcess.argtypes == (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    assert kernel32.GetExitCodeProcess.argtypes == (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    assert kernel32.GetExitCodeProcess.restype is wintypes.BOOL
    assert kernel32.CloseHandle.argtypes == (wintypes.HANDLE,)
    assert [name for name, _args in calls] == ["OpenProcess", "GetExitCodeProcess", "CloseHandle"]
    assert calls[2][1] == (handle,)


# ---------------------------------------------------------------------------
# Download receipts over files that were already present
# ---------------------------------------------------------------------------


_KOKORO = "hexgrad/Kokoro-82M"


def _blob_link(etag):
    """A snapshot entry's relative link to its blob, as the Hub writes it.

    Windows cannot follow a relative link whose target uses forward slashes.
    """
    import os

    return os.path.join("..", "..", "blobs", etag)


def _hub_snapshot(cache_dir, files):
    """Lay out a Hub cache snapshot whose entries link to ETag-named blobs."""
    import hashlib

    from modules.runtime_setup.catalog import pinned_revision_for

    revision = pinned_revision_for(_KOKORO)
    repo = cache_dir / "models--hexgrad--Kokoro-82M"
    snapshot = repo / "snapshots" / revision
    (repo / "blobs").mkdir(parents=True)
    snapshot.mkdir(parents=True)
    for name, (published, actual) in files.items():
        etag = hashlib.sha256(published).hexdigest()
        (repo / "blobs" / etag).write_bytes(actual)
        (snapshot / name).symlink_to(_blob_link(etag))
    return snapshot, revision


def _fake_hub(monkeypatch, snapshot, published):
    import huggingface_hub

    downloads = []

    def snapshot_download(**kwargs):
        return str(snapshot)

    def hf_hub_download(**kwargs):
        name = kwargs["filename"]
        downloads.append(kwargs)
        target = snapshot / name
        if target.is_symlink() or target.exists():
            raise AssertionError("A damaged entry must be removed before it is refreshed")
        target.write_bytes(published[name])
        return str(target)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot_download)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", hf_hub_download)
    return downloads


def test_receipt_is_not_written_over_unverified_preexisting_files(monkeypatch, tmp_path):
    from modules.runtime_setup import downloads
    from modules.runtime_setup.receipts import validate_download_receipt

    published = {"weights.bin": b"published weights", "config.json": b"{}"}
    snapshot, revision = _hub_snapshot(tmp_path, {
        "weights.bin": (published["weights.bin"], b"corrupted weights"),
        "config.json": (published["config.json"], published["config.json"]),
    })
    fetched = _fake_hub(monkeypatch, snapshot, published)

    result = downloads.download_model_snapshot(_KOKORO, cache_dir=tmp_path)

    assert result == snapshot
    assert [call["filename"] for call in fetched] == ["weights.bin"]
    assert fetched[0]["force_download"] is True
    assert fetched[0]["revision"] == revision
    assert (snapshot / "weights.bin").read_bytes() == b"published weights"
    assert validate_download_receipt(snapshot, model_id=_KOKORO, revision=revision)


def test_verified_preexisting_files_are_not_downloaded_again(monkeypatch, tmp_path):
    from modules.runtime_setup import downloads

    published = {"weights.bin": b"published weights"}
    snapshot, _revision = _hub_snapshot(tmp_path, {
        "weights.bin": (published["weights.bin"], published["weights.bin"]),
    })
    fetched = _fake_hub(monkeypatch, snapshot, published)

    downloads.download_model_snapshot(_KOKORO, cache_dir=tmp_path)

    assert fetched == []
    assert (snapshot / downloads.DOWNLOAD_RECEIPT_FILENAME).is_file()


def test_git_hashed_preexisting_files_verify_against_their_blob_name(tmp_path):
    import hashlib

    from modules.runtime_setup import downloads

    content = b"small text file\n"
    path = tmp_path / "README.md"
    path.write_bytes(content)
    git_oid = hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()
    assert downloads._matches_etag(path, git_oid)
    assert not downloads._matches_etag(path, hashlib.sha1(b"other").hexdigest())
    assert not downloads._matches_etag(path, "not-a-hash")


def test_symlinked_receipt_is_never_trusted(monkeypatch, tmp_path):
    import huggingface_hub

    from modules.runtime_setup import downloads

    published = {"weights.bin": b"published weights"}
    snapshot, _revision = _hub_snapshot(tmp_path, {
        "weights.bin": (published["weights.bin"], published["weights.bin"]),
    })
    (snapshot / downloads.DOWNLOAD_RECEIPT_FILENAME).symlink_to(tmp_path / "missing-receipt.json")
    forced = []

    def snapshot_download(**kwargs):
        if kwargs.get("force_download"):
            forced.append(kwargs)
            (snapshot / "weights.bin").write_bytes(published["weights.bin"])
        return str(snapshot)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot_download)

    downloads.download_model_snapshot(_KOKORO, cache_dir=tmp_path)

    assert len(forced) == 1
    receipt = snapshot / downloads.DOWNLOAD_RECEIPT_FILENAME
    assert receipt.is_file() and not receipt.is_symlink()


# ---------------------------------------------------------------------------
# Persistent receipt verification cache
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settled_receipt_clock(monkeypatch):
    """Let freshly written snapshots count as settled without a wall-clock wait.

    On Windows a fingerprint is only cached once its files' timestamps are a
    couple of seconds old.
    """
    from modules.runtime_setup import receipts

    real_now = receipts._now_ns
    monkeypatch.setattr(receipts, "_now_ns", lambda: real_now() + 10_000_000_000)


def _receipted_snapshot(root):
    from modules.runtime_setup.catalog import pinned_revision_for
    from modules.runtime_setup.receipts import write_download_receipt

    revision = pinned_revision_for(_KOKORO)
    snapshot = root / "snapshots" / revision
    snapshot.mkdir(parents=True)
    (snapshot / "weights.bin").write_bytes(b"weights")
    write_download_receipt(snapshot, model_id=_KOKORO, revision=revision)
    return snapshot.resolve(), revision


def _count_hashes(monkeypatch):
    from modules.runtime_setup import receipts

    hashed = []
    original = receipts._validate_parsed_receipt

    def counting(parsed):
        hashed.append(parsed.snapshot)
        return original(parsed)

    monkeypatch.setattr(receipts, "_validate_parsed_receipt", counting)
    return hashed


def test_model_snapshot_checks_persist_to_the_shared_integrity_cache(monkeypatch, tmp_path):
    from modules.runtime_setup.receipts import ModelReceiptVerifier
    from modules.runtime_setup.state import default_state_path
    from modules.tts import model_snapshots

    data_dir = tmp_path / "desktop" / "data"
    data_dir.mkdir(parents=True)
    monkeypatch.setenv("AUDIOBOOK_DATA_DIR", str(data_dir))
    cache_path = default_state_path().with_name("model-integrity-cache.json")
    assert model_snapshots._integrity_cache_path() == cache_path

    snapshot, revision = _receipted_snapshot(tmp_path / "hub")
    hashed = _count_hashes(monkeypatch)
    first = ModelReceiptVerifier(cache_path_factory=model_snapshots._integrity_cache_path)
    assert first.verify(snapshot, model_id=_KOKORO, revision=revision)
    assert cache_path.is_file()

    # A new process (here: a new verifier) reuses the check without hashing.
    restarted = ModelReceiptVerifier(cache_path_factory=model_snapshots._integrity_cache_path)
    assert restarted.verify(snapshot, model_id=_KOKORO, revision=revision)
    assert len(hashed) == 1

    # Changed bytes are hashed again and fail.
    (snapshot / "weights.bin").write_bytes(b"changed")
    assert not ModelReceiptVerifier(
        cache_path_factory=model_snapshots._integrity_cache_path,
    ).verify(snapshot, model_id=_KOKORO, revision=revision)
    assert len(hashed) == 2


def test_integrity_cache_stays_in_memory_without_a_desktop_data_dir(monkeypatch):
    from modules.tts import model_snapshots

    monkeypatch.delenv("AUDIOBOOK_DATA_DIR", raising=False)
    assert model_snapshots._integrity_cache_path() is None


def test_unwritable_integrity_cache_still_verifies(monkeypatch, tmp_path):
    from modules.runtime_setup.receipts import ModelReceiptVerifier

    snapshot, revision = _receipted_snapshot(tmp_path / "hub")
    verifier = ModelReceiptVerifier(cache_path=tmp_path / "missing-dir" / "cache.json")
    assert verifier.verify(snapshot, model_id=_KOKORO, revision=revision)
    assert verifier.verify(snapshot, model_id=_KOKORO, revision=revision)


def test_integrity_cache_is_written_without_blocking_verification(monkeypatch, tmp_path):
    from modules.runtime_setup import receipts

    snapshot, revision = _receipted_snapshot(tmp_path / "hub")
    verifier = receipts.ModelReceiptVerifier(cache_path=tmp_path / "cache.json")
    writer = receipts.atomic_write_json
    lock_free_during_write = []

    def checking_write(*args, **kwargs):
        # The condition's lock is reentrant, so probe it from another thread.
        def probe():
            acquired = verifier._condition.acquire(blocking=False)
            if acquired:
                verifier._condition.release()
            lock_free_during_write.append(acquired)

        prober = threading.Thread(target=probe)
        prober.start()
        prober.join()
        return writer(*args, **kwargs)

    monkeypatch.setattr(receipts, "atomic_write_json", checking_write)
    assert verifier.verify(snapshot, model_id=_KOKORO, revision=revision)
    assert lock_free_during_write == [True]


def test_verifiers_sharing_a_cache_file_keep_each_others_checks(tmp_path):
    from modules.runtime_setup.receipts import ModelReceiptVerifier

    cache_path = tmp_path / "cache.json"
    first_snapshot, revision = _receipted_snapshot(tmp_path / "one")
    second_snapshot, _ = _receipted_snapshot(tmp_path / "two")
    first = ModelReceiptVerifier(cache_path=cache_path)
    second = ModelReceiptVerifier(cache_path=cache_path)

    assert first.verify(first_snapshot, model_id=_KOKORO, revision=revision)
    assert second.verify(second_snapshot, model_id=_KOKORO, revision=revision)

    import json

    assert len(json.loads(cache_path.read_text())["entries"]) == 2


def test_invalidate_clears_the_shared_cache_file(tmp_path):
    from modules.runtime_setup.receipts import ModelReceiptVerifier

    cache_path = tmp_path / "cache.json"
    snapshot, revision = _receipted_snapshot(tmp_path / "one")
    verifier = ModelReceiptVerifier(cache_path=cache_path)
    assert verifier.verify(snapshot, model_id=_KOKORO, revision=revision)
    verifier.invalidate()

    import json

    assert json.loads(cache_path.read_text())["entries"] == {}


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _fake_paths_info(monkeypatch, published, calls=None):
    import huggingface_hub

    class _Api:
        def get_paths_info(self, repo_id, paths, *, revision=None, repo_type=None):
            if calls is not None:
                calls.append(list(paths))
            return [
                SimpleNamespace(path=path, blob_id="0" * 40, lfs=SimpleNamespace(sha256=_sha256(published[path])))
                for path in paths if path in published
            ]

    monkeypatch.setattr(huggingface_hub, "HfApi", _Api)


def test_copied_preexisting_files_are_checked_against_hub_hashes(monkeypatch, tmp_path):
    from modules.runtime_setup import downloads
    from modules.runtime_setup.catalog import pinned_revision_for
    from modules.runtime_setup.receipts import validate_download_receipt

    revision = pinned_revision_for(_KOKORO)
    snapshot = tmp_path / "models--hexgrad--Kokoro-82M" / "snapshots" / revision
    snapshot.mkdir(parents=True)
    published = {"weights.bin": b"published weights", "voice.pt": b"published voice"}
    # Copies without symlinks, as the Hub writes them when symlinks are off.
    (snapshot / "weights.bin").write_bytes(published["weights.bin"])
    (snapshot / "voice.pt").write_bytes(b"corrupted voice")
    fetched = _fake_hub(monkeypatch, snapshot, published)
    asked = []
    _fake_paths_info(monkeypatch, published, asked)

    downloads.download_model_snapshot(_KOKORO, cache_dir=tmp_path)

    assert asked == [["voice.pt", "weights.bin"]]
    assert [call["filename"] for call in fetched] == ["voice.pt"]
    assert validate_download_receipt(snapshot, model_id=_KOKORO, revision=revision)


def test_copied_preexisting_files_are_refreshed_when_hub_hashes_are_unavailable(monkeypatch, tmp_path):
    import huggingface_hub

    from modules.runtime_setup import downloads
    from modules.runtime_setup.catalog import pinned_revision_for

    revision = pinned_revision_for(_KOKORO)
    snapshot = tmp_path / "models--hexgrad--Kokoro-82M" / "snapshots" / revision
    snapshot.mkdir(parents=True)
    published = {"weights.bin": b"published weights"}
    (snapshot / "weights.bin").write_bytes(published["weights.bin"])
    fetched = _fake_hub(monkeypatch, snapshot, published)

    class _OfflineApi:
        def get_paths_info(self, *args, **kwargs):
            raise OSError("offline")

    monkeypatch.setattr(huggingface_hub, "HfApi", _OfflineApi)

    downloads.download_model_snapshot(_KOKORO, cache_dir=tmp_path)

    assert [call["filename"] for call in fetched] == ["weights.bin"]


def _fake_linking_hub(monkeypatch, snapshot, blobs, links, published):
    """A Hub that links the snapshot to blobs, writing only ``blobs`` itself."""
    import huggingface_hub
    from huggingface_hub.errors import LocalEntryNotFoundError

    fetched = []

    def snapshot_download(**kwargs):
        if kwargs.get("local_files_only"):
            raise LocalEntryNotFoundError("not cached")
        snapshot.mkdir(parents=True, exist_ok=True)
        for etag, content in blobs.items():
            (snapshot.parents[1] / "blobs" / etag).write_bytes(content)
        for name, etag in links.items():
            (snapshot / name).symlink_to(_blob_link(etag))
        return str(snapshot)

    def hf_hub_download(**kwargs):
        name = kwargs["filename"]
        fetched.append(kwargs)
        assert not (snapshot / name).is_symlink()
        (snapshot / name).write_bytes(published[name])
        return str(snapshot / name)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot_download)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", hf_hub_download)
    return fetched


def test_blob_reused_from_another_revision_is_not_certified_unchecked(monkeypatch, tmp_path):
    from modules.runtime_setup import downloads
    from modules.runtime_setup.catalog import pinned_revision_for
    from modules.runtime_setup.receipts import validate_download_receipt

    revision = pinned_revision_for(_KOKORO)
    repo = tmp_path / "models--hexgrad--Kokoro-82M"
    published = {"weights.bin": b"published weights"}
    etag = _sha256(published["weights.bin"])
    # Another revision left a damaged blob under the same ETag; the new
    # revision's snapshot directory does not exist yet.
    (repo / "blobs").mkdir(parents=True)
    (repo / "blobs" / etag).write_bytes(b"damaged weights")
    snapshot = repo / "snapshots" / revision
    fetched = _fake_linking_hub(monkeypatch, snapshot, {}, {"weights.bin": etag}, published)

    downloads.download_model_snapshot(_KOKORO, cache_dir=tmp_path)

    assert [call["filename"] for call in fetched] == ["weights.bin"]
    assert (snapshot / "weights.bin").read_bytes() == b"published weights"
    assert validate_download_receipt(snapshot, model_id=_KOKORO, revision=revision)


def test_unpinned_model_checks_files_already_in_its_resolved_snapshot(monkeypatch, tmp_path):
    from modules.runtime_setup import downloads

    model = "example-org/custom-cleaner"
    revision = "a" * 40
    repo = tmp_path / "models--example-org--custom-cleaner"
    snapshot = repo / "snapshots" / revision
    published = {"weights.bin": b"published weights"}
    etag = _sha256(published["weights.bin"])
    (repo / "blobs").mkdir(parents=True)
    (repo / "blobs" / etag).write_bytes(b"damaged weights")
    snapshot.mkdir(parents=True)
    (snapshot / "weights.bin").symlink_to(_blob_link(etag))
    fetched = _fake_hub(monkeypatch, snapshot, published)

    downloads.download_model_snapshot(model, cache_dir=tmp_path)

    assert [call["filename"] for call in fetched] == ["weights.bin"]
    assert [call["revision"] for call in fetched] == [revision]


def test_fresh_download_is_hashed_only_for_its_receipt(monkeypatch, tmp_path):
    from modules.runtime_setup import downloads, receipts
    from modules.runtime_setup.catalog import pinned_revision_for

    revision = pinned_revision_for(_KOKORO)
    repo = tmp_path / "models--hexgrad--Kokoro-82M"
    (repo / "blobs").mkdir(parents=True)
    published = {"weights.bin": b"published weights"}
    etag = _sha256(published["weights.bin"])
    fetched = _fake_linking_hub(
        monkeypatch, repo / "snapshots" / revision, {etag: published["weights.bin"]},
        {"weights.bin": etag}, published,
    )
    hashed = []
    original = receipts._file_size_and_sha256

    def counting(path):
        hashed.append(path)
        return original(path)

    monkeypatch.setattr(receipts, "_file_size_and_sha256", counting)
    monkeypatch.setattr(downloads, "_file_size_and_sha256", counting)
    monkeypatch.setattr(downloads, "_matches_etag", lambda *a: pytest.fail("fresh file hashed twice"))

    downloads.download_model_snapshot(_KOKORO, cache_dir=tmp_path)

    assert fetched == []
    assert len(hashed) == 1


def _fake_local_dir_hub(monkeypatch, folder, published):
    import huggingface_hub
    from huggingface_hub.errors import EntryNotFoundError

    fetched = []

    def snapshot_download(**kwargs):
        for name, content in published.items():
            if not (folder / name).exists():
                (folder / name).write_bytes(content)
        return str(folder)

    def hf_hub_download(**kwargs):
        name = kwargs["filename"]
        fetched.append(name)
        if name not in published:
            raise EntryNotFoundError(f"404 {name}")
        (folder / name).write_bytes(published[name])
        return str(folder / name)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot_download)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", hf_hub_download)
    return fetched


def test_local_dir_download_never_touches_unrelated_user_files(monkeypatch, tmp_path):
    from modules.runtime_setup import downloads

    folder = tmp_path / "Downloads"
    (folder / "photos").mkdir(parents=True)
    (folder / "my_notes.txt").write_bytes(b"precious user data")
    (folder / "photos" / "cat.jpg").write_bytes(b"\xff\xd8 jpeg bytes")
    published = {"config.json": b"{}", "kokoro.pth": b"published weights"}
    # A damaged copy of a model file was left by an earlier attempt.
    (folder / "kokoro.pth").write_bytes(b"damaged weights")
    fetched = _fake_local_dir_hub(monkeypatch, folder, published)
    asked = []
    _fake_paths_info(monkeypatch, published, asked)

    downloads.download_model_snapshot(_KOKORO, local_dir=folder)

    assert (folder / "my_notes.txt").read_bytes() == b"precious user data"
    assert (folder / "photos" / "cat.jpg").read_bytes() == b"\xff\xd8 jpeg bytes"
    assert fetched == ["kokoro.pth"]
    assert (folder / "kokoro.pth").read_bytes() == b"published weights"


def test_local_dir_refuses_to_certify_files_it_cannot_check(monkeypatch, tmp_path):
    import huggingface_hub

    from modules.runtime_setup import downloads

    folder = tmp_path / "Downloads"
    folder.mkdir()
    (folder / "my_notes.txt").write_bytes(b"precious user data")
    _fake_local_dir_hub(monkeypatch, folder, {"kokoro.pth": b"published weights"})

    class _OfflineApi:
        def get_paths_info(self, *args, **kwargs):
            raise OSError("offline")

    monkeypatch.setattr(huggingface_hub, "HfApi", _OfflineApi)

    with pytest.raises(RuntimeError, match="Could not check"):
        downloads.download_model_snapshot(_KOKORO, local_dir=folder)
    assert (folder / "my_notes.txt").read_bytes() == b"precious user data"
    assert not (folder / downloads.DOWNLOAD_RECEIPT_FILENAME).exists()


def test_copied_preexisting_blob_without_symlinks_is_not_certified(monkeypatch, tmp_path):
    import shutil

    import huggingface_hub
    from huggingface_hub.errors import LocalEntryNotFoundError

    from modules.runtime_setup import downloads
    from modules.runtime_setup.catalog import pinned_revision_for
    from modules.runtime_setup.receipts import validate_download_receipt

    revision = pinned_revision_for(_KOKORO)
    repo = tmp_path / "models--hexgrad--Kokoro-82M"
    published = {"weights.bin": b"published weights"}
    etag = _sha256(published["weights.bin"])
    (repo / "blobs").mkdir(parents=True)
    (repo / "blobs" / etag).write_bytes(b"damaged weights")
    snapshot = repo / "snapshots" / revision
    fetched = []

    def snapshot_download(**kwargs):
        if kwargs.get("local_files_only"):
            raise LocalEntryNotFoundError("not cached")
        snapshot.mkdir(parents=True, exist_ok=True)
        # The Hub's fallback when it cannot symlink an existing blob.
        shutil.copyfile(repo / "blobs" / etag, snapshot / "weights.bin")
        return str(snapshot)

    def hf_hub_download(**kwargs):
        fetched.append(kwargs["filename"])
        (snapshot / kwargs["filename"]).write_bytes(published[kwargs["filename"]])
        return str(snapshot / kwargs["filename"])

    monkeypatch.setattr(huggingface_hub, "snapshot_download", snapshot_download)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", hf_hub_download)
    _fake_paths_info(monkeypatch, published)

    downloads.download_model_snapshot(_KOKORO, cache_dir=tmp_path)

    assert fetched == ["weights.bin"]
    assert validate_download_receipt(snapshot, model_id=_KOKORO, revision=revision)
    assert (snapshot / "weights.bin").read_bytes() == b"published weights"


def test_removal_never_reaches_outside_the_hub_snapshot(monkeypatch, tmp_path):
    import os

    from modules.runtime_setup import downloads

    revision = "b" * 40
    snapshot = tmp_path / "models--example-org--custom-cleaner" / "snapshots" / revision
    snapshot.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_bytes(b"keep")
    os.symlink(outside, snapshot / "linked", target_is_directory=True)

    assert not downloads._entry_inside(snapshot, snapshot / "linked" / "keep.txt")
    assert not downloads._entry_inside(snapshot, snapshot / ".." / "outside" / "keep.txt")
    assert downloads._entry_inside(snapshot, snapshot / "weights.bin")
    assert not downloads._owned_snapshot(outside, tmp_path / "models--example-org--custom-cleaner", local_dir=None)
