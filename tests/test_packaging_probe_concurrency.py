"""Bound independent chapter probes without changing ordering or cancellation."""

from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from modules.audio.packager import M4BPackager


def test_chapter_probes_are_bounded_and_results_keep_input_order(monkeypatch):
    lock = threading.Lock()
    first_pair = threading.Barrier(2)
    active = 0
    maximum = 0
    counts = [3, 0, 7, 2, 5, 1]

    def probe(command, **_kwargs):
        nonlocal active, maximum
        index = int(Path(command[-1]).stem)
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            if index < 2:
                first_pair.wait(timeout=3)
            return SimpleNamespace(stdout=f"{counts[index]}\n")
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr("modules.audio.packager.run_ffmpeg_command", probe)
    result = M4BPackager._probe_segment_packet_counts(
        "ffprobe", [Path(f"{index}.m4a") for index in range(len(counts))], None,
    )
    assert result == counts
    assert maximum == 2
    assert active == 0


@pytest.mark.parametrize("failure", ["error", "cancel"])
@pytest.mark.parametrize("failing_index", [0, 1])
def test_probe_failure_or_cancel_stops_sibling_before_return(monkeypatch, failure, failing_index):
    both_started = threading.Barrier(2)
    requested = threading.Event()
    sibling_stopped = threading.Event()
    started = []

    def probe(command, *, cancel_requested, **_kwargs):
        index = int(Path(command[-1]).stem)
        started.append(index)
        both_started.wait(timeout=3)
        if index == failing_index:
            if failure == "cancel":
                requested.set()
                assert cancel_requested()
            raise RuntimeError(failure)
        for _ in range(100):
            if cancel_requested():
                sibling_stopped.set()
                raise RuntimeError("cancelled")
            threading.Event().wait(0.01)
        raise AssertionError("sibling did not receive cancellation")

    monkeypatch.setattr("modules.audio.packager.run_ffmpeg_command", probe)
    with pytest.raises(RuntimeError, match=failure if failing_index == 0 else "cancelled"):
        M4BPackager._probe_segment_packet_counts(
            "ffprobe", [Path(f"{index}.m4a") for index in range(8)], requested.is_set,
        )
    assert sorted(started) == [0, 1]
    assert sibling_stopped.is_set()


def test_cancelled_packaging_starts_no_probes(monkeypatch):
    def unexpected(*_args, **_kwargs):
        raise AssertionError("probe started after cancellation")

    monkeypatch.setattr("modules.audio.packager.run_ffmpeg_command", unexpected)
    with pytest.raises(RuntimeError, match="cancelled"):
        M4BPackager._probe_segment_packet_counts(
            "ffprobe", [Path(f"{index}.m4a") for index in range(8)], lambda: True,
        )


def test_cancelled_segment_probe_never_falls_back_to_duration_probes(monkeypatch):
    requested = threading.Event()
    monkeypatch.setattr("modules.audio.packager.require_ffmpeg_binary", lambda _name: "ffprobe")
    packager = M4BPackager()

    def cancelled_segment_probe(*_args, **_kwargs):
        requested.set()
        raise RuntimeError("FFprobe failed: cancelled")

    def unexpected_fallback(*_args, **_kwargs):
        pytest.fail("Cancelled packaging started duration fallback")

    monkeypatch.setattr(packager, "_probe_segment_packet_counts", cancelled_segment_probe)
    monkeypatch.setattr(packager, "_generate_chapter_markers", unexpected_fallback)
    with pytest.raises(RuntimeError, match="cancelled"):
        packager._stream_copy_chapter_markers(
            Path("concat.m4a"), [Path(f"{index}.m4a") for index in range(8)],
            [str(index) for index in range(8)], cancel_requested=requested.is_set,
        )


@pytest.mark.parametrize("stream_copy", [False, True])
@pytest.mark.parametrize("probe_fails", [False, True])
def test_duration_fallback_stops_when_cancelled_between_probes(monkeypatch, stream_copy, probe_fails):
    requested = threading.Event()
    probed = []
    monkeypatch.setattr("modules.audio.packager.require_ffmpeg_binary", lambda _name: "ffprobe")
    monkeypatch.setattr("modules.audio.encoder.require_ffmpeg_binary", lambda _name: "ffprobe")
    packager = M4BPackager()

    def duration(_self, audio_file, **_kwargs):
        probed.append(audio_file)
        requested.set()
        if probe_fails:
            raise RuntimeError("failed probe")
        return 1.0

    monkeypatch.setattr("modules.audio.encoder.AudioEncoder.get_duration", duration)
    files = [Path(f"{index}.m4a") for index in range(8)]
    names = [str(index) for index in range(8)]
    with pytest.raises(RuntimeError, match="cancelled"):
        if stream_copy:
            monkeypatch.setattr(packager, "_probe_stream_copy_offsets_ms", lambda *_args, **_kwargs: None)
            packager._stream_copy_chapter_markers(
                Path("concat.m4a"), files, names, cancel_requested=requested.is_set,
            )
        else:
            packager._generate_chapter_markers(files, names, cancel_requested=requested.is_set)
    assert probed == [files[0]]


def test_duration_fallback_probe_stops_when_cancelled(tmp_path, monkeypatch):
    requested = threading.Event()
    killed = threading.Event()
    entered = threading.Event()
    returned = threading.Event()
    errors = []
    source = tmp_path / "chapter.m4a"
    source.write_bytes(b"audio")

    class Process:
        returncode = 0

        def communicate(self):
            entered.set()
            killed.wait(2)
            return '{"format":{"duration":"1.0"}}', ""

        def kill(self):
            killed.set()

        def wait(self, **kwargs):
            return 0

    def run(*args, **kwargs):
        stdout, stderr = Process().communicate()
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=0)

    monkeypatch.setattr("modules.audio.encoder.require_ffmpeg_binary", lambda _name: "ffprobe")
    monkeypatch.setattr("modules.audio.ffmpeg.subprocess.Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr("modules.audio.ffmpeg.subprocess.run", run)

    def package():
        try:
            M4BPackager()._generate_chapter_markers(
                [source], ["One"], cancel_requested=requested.is_set,
            )
        except RuntimeError as exc:
            errors.append(exc)
        finally:
            returned.set()

    worker = threading.Thread(target=package)
    worker.start()
    try:
        assert entered.wait(2)
        requested.set()
        assert returned.wait(0.5), "Duration probe kept running after cancellation"
        assert killed.is_set()
        assert len(errors) == 1 and "cancelled" in str(errors[0])
    finally:
        killed.set()
        worker.join(3)
