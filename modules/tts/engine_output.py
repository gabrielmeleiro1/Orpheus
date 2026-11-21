"""Audio-output helpers for the legacy TTS engine facade."""

from __future__ import annotations

import tempfile
import time
import wave
from pathlib import Path

import numpy as np

from modules.errors import SynthesisError

from . import mlx_compat as mlx_compat_backend
from .voices import kokoro_language_code

_WAV_COPY_FRAMES = 65_536
_UPSTREAM_GENERATE_MODULE = "mlx_audio.tts.generate"


def _direct_generation_model(engine, generate_fn):  # noqa: ANN001, ANN202
    """Return the loaded model when synthesis can call it without the wrapper.

    mlx-audio's ``generate_audio`` catches every exception and only prints it,
    so installation, memory and model failures would surface as a missing
    output file. Injected generate functions keep the wrapper contract.
    """
    model = getattr(engine, "_model", None)
    if model is None or not callable(getattr(model, "generate", None)):
        return None
    active_generate_fn = generate_fn or mlx_compat_backend.generate_audio
    if (
        active_generate_fn is not None
        and getattr(active_generate_fn, "__module__", None) != _UPSTREAM_GENERATE_MODULE
    ):
        return None
    return model


def _pcm16(audio) -> np.ndarray:  # noqa: ANN001
    """Quantize one segment exactly as mlx-audio's WAV writer does."""
    data = np.array(audio)
    if data.dtype in (np.float32, np.float64):
        data = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16)
    elif data.dtype != np.int16:
        data = data.astype(np.int16)
    return data.reshape(-1)


def _generate_pcm16(engine, model, text: str, voice: str, speed: float) -> tuple[np.ndarray, int]:  # noqa: ANN001
    """Run the loaded model and return joined 16-bit samples and their rate."""
    segments: list[np.ndarray] = []
    sample_rate = None
    with engine._model_lock:  # noqa: SLF001
        with mlx_compat_backend.use_installed_voices(
            engine._model_path, engine._voice_snapshot,  # noqa: SLF001
        ):
            for result in model.generate(
                text=text,
                voice=voice,
                speed=speed,
                lang_code=kokoro_language_code(voice),
            ):
                segments.append(_pcm16(result.audio))
                if sample_rate is None:
                    sample_rate = getattr(result, "sample_rate", None)
    if not segments:
        raise SynthesisError("No audio file generated")
    pcm = segments[0] if len(segments) == 1 else np.concatenate(segments)
    return pcm, int(sample_rate or engine.config.sample_rate)


def _pcm16_to_float(pcm: np.ndarray) -> np.ndarray:
    """Match soundfile's float32 decoding of 16-bit PCM."""
    return pcm.astype(np.float32) / 32768.0


def _write_pcm16_wav(output_path: Path, pcm: np.ndarray, sample_rate: int) -> None:
    with wave.open(str(output_path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(pcm.astype("<i2", copy=False).tobytes())


def _inject_generate_audio_model_arg(
    kwargs: dict,
    model_path: str,
    loaded_model: object | None = None,
    generate_fn=None,  # noqa: ANN001
) -> None:
    mlx_compat_backend.inject_generate_audio_model_arg(
        kwargs,
        model_path,
        loaded_model=loaded_model,
        generate_fn=generate_fn or mlx_compat_backend.generate_audio,
    )


def _inject_generate_audio_output_args(
    kwargs: dict,
    output_dir: str,
    file_prefix: str,
    generate_fn=None,  # noqa: ANN001
) -> None:
    mlx_compat_backend.inject_generate_audio_output_args(
        kwargs,
        output_dir=output_dir,
        file_prefix=file_prefix,
        generate_fn=generate_fn or mlx_compat_backend.generate_audio,
    )


def synthesize_to_output_path(
    engine,  # noqa: ANN001
    text: str,
    voice: str,
    speed: float,
    output_path: Path,
    *,
    generate_fn=None,  # noqa: ANN001
    inject_model_arg=None,  # noqa: ANN001
    inject_output_args=None,  # noqa: ANN001
) -> np.ndarray:
    """Synthesize directly to a destination path and return audio samples."""
    import soundfile as sf

    output_path.parent.mkdir(parents=True, exist_ok=True)
    _remove_stale_generated_files(output_path)

    model = _direct_generation_model(engine, generate_fn)
    if model is not None:
        pcm, _sample_rate = _generate_pcm16(engine, model, text, voice, speed)
        audio = _pcm16_to_float(pcm)
        sf.write(str(output_path), audio, engine.config.sample_rate, subtype="PCM_16")
        return audio

    with engine._model_lock:  # noqa: SLF001
        gen_kwargs = {
            "text": text,
            "voice": voice,
            "lang_code": kokoro_language_code(voice),
            "speed": speed,
            "audio_format": "wav",
            "verbose": False,
        }
        model_arg_injector = inject_model_arg or _inject_generate_audio_model_arg
        output_arg_injector = inject_output_args or _inject_generate_audio_output_args
        active_generate_fn = generate_fn or mlx_compat_backend.generate_audio
        model_arg_injector(
            gen_kwargs,
            engine._model_path,  # noqa: SLF001
            loaded_model=engine._model,  # noqa: SLF001
            generate_fn=active_generate_fn,
        )
        output_arg_injector(
            gen_kwargs,
            output_dir=str(output_path.parent),
            file_prefix=output_path.stem,
            generate_fn=active_generate_fn,
        )
        with mlx_compat_backend.use_installed_voices(
            engine._model_path, engine._voice_snapshot,  # noqa: SLF001
        ):
            active_generate_fn(**gen_kwargs)

    if output_path.exists():
        audio, _ = sf.read(str(output_path), dtype="float32")
        return audio

    generated_paths = sorted(output_path.parent.glob(f"{output_path.stem}_*.wav"))
    if not generated_paths:
        raise SynthesisError("No audio file generated")

    segments = [sf.read(str(path), dtype="float32")[0] for path in generated_paths]
    audio = np.concatenate([np.asarray(segment, dtype=np.float32) for segment in segments])
    sf.write(str(output_path), audio, engine.config.sample_rate, subtype="PCM_16")
    for generated_path in generated_paths:
        try:
            generated_path.unlink()
        except Exception:
            pass
    return audio


def _concatenate_wav_files(wav_paths: list[Path], output_path: Path) -> None:
    """Concatenate compatible PCM WAV files without materializing NumPy arrays."""
    if not wav_paths:
        raise SynthesisError("No audio file generated")

    with wave.open(str(wav_paths[0]), "rb") as first_reader:
        params = first_reader.getparams()

        with wave.open(str(output_path), "wb") as writer:
            writer.setparams(params)
            while True:
                frames = first_reader.readframes(_WAV_COPY_FRAMES)
                if not frames:
                    break
                writer.writeframes(frames)

            for wav_path in wav_paths[1:]:
                with wave.open(str(wav_path), "rb") as reader:
                    if reader.getparams()[:3] != params[:3]:
                        raise SynthesisError("Generated WAV files have incompatible audio parameters")
                    while True:
                        frames = reader.readframes(_WAV_COPY_FRAMES)
                        if not frames:
                            break
                        writer.writeframes(frames)

def _remove_stale_generated_files(output_path: Path) -> None:
    """Remove deterministic outputs before synthesis to avoid stale audio reuse."""
    candidates = [output_path, *sorted(output_path.parent.glob(f"{output_path.stem}_*.wav"))]
    for candidate in candidates:
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise SynthesisError(
                f"Unable to remove stale generated audio file: {candidate}"
            ) from exc


def synthesize_to_output_file(
    engine,  # noqa: ANN001
    text: str,
    voice: str,
    speed: float,
    output_path: Path,
    *,
    generate_fn=None,  # noqa: ANN001
    inject_model_arg=None,  # noqa: ANN001
    inject_output_args=None,  # noqa: ANN001
) -> Path:
    """Synthesize directly to a WAV file and return its path without reading samples."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _remove_stale_generated_files(output_path)

    model = _direct_generation_model(engine, generate_fn)
    if model is not None:
        pcm, sample_rate = _generate_pcm16(engine, model, text, voice, speed)
        _write_pcm16_wav(output_path, pcm, sample_rate)
        return output_path

    with engine._model_lock:  # noqa: SLF001
        gen_kwargs = {
            "text": text,
            "voice": voice,
            "lang_code": kokoro_language_code(voice),
            "speed": speed,
            "audio_format": "wav",
            "join_audio": True,
            "verbose": False,
        }
        model_arg_injector = inject_model_arg or _inject_generate_audio_model_arg
        output_arg_injector = inject_output_args or _inject_generate_audio_output_args
        active_generate_fn = generate_fn or mlx_compat_backend.generate_audio
        model_arg_injector(
            gen_kwargs,
            engine._model_path,  # noqa: SLF001
            loaded_model=engine._model,  # noqa: SLF001
            generate_fn=active_generate_fn,
        )
        output_arg_injector(
            gen_kwargs,
            output_dir=str(output_path.parent),
            file_prefix=output_path.stem,
            generate_fn=active_generate_fn,
        )
        with mlx_compat_backend.use_installed_voices(
            engine._model_path, engine._voice_snapshot,  # noqa: SLF001
        ):
            active_generate_fn(**gen_kwargs)

    if output_path.exists():
        return output_path

    generated_paths = sorted(output_path.parent.glob(f"{output_path.stem}_*.wav"))
    if not generated_paths:
        raise SynthesisError("No audio file generated")
    if len(generated_paths) == 1:
        generated_paths[0].replace(output_path)
        return output_path

    _concatenate_wav_files(generated_paths, output_path)
    for generated_path in generated_paths:
        try:
            generated_path.unlink()
        except Exception:
            pass
    return output_path


def generate_temp_wav_paths(
    engine,  # noqa: ANN001
    text: str,
    voice: str,
    speed: float,
    temp_path: Path,
    *,
    generate_fn=None,  # noqa: ANN001
    inject_model_arg=None,  # noqa: ANN001
    inject_output_args=None,  # noqa: ANN001
) -> list[Path]:
    """Generate WAV outputs in a temp dir, retrying join mode if needed."""
    wav_paths: list[Path] = []
    for join_audio in (False, True):
        for old_file in temp_path.glob("*.wav"):
            try:
                old_file.unlink()
            except Exception:
                pass

        with engine._model_lock:  # noqa: SLF001
            gen_kwargs = {
                "text": text,
                "voice": voice,
                "lang_code": kokoro_language_code(voice),
                "speed": speed,
                "audio_format": "wav",
                "join_audio": join_audio,
                "verbose": False,
            }
            model_arg_injector = inject_model_arg or _inject_generate_audio_model_arg
            output_arg_injector = inject_output_args or _inject_generate_audio_output_args
            active_generate_fn = generate_fn or mlx_compat_backend.generate_audio
            model_arg_injector(
                gen_kwargs,
                engine._model_path,  # noqa: SLF001
                loaded_model=engine._model,  # noqa: SLF001
                generate_fn=active_generate_fn,
            )
            output_arg_injector(
                gen_kwargs,
                output_dir=str(temp_path),
                file_prefix="tts_output",
                generate_fn=active_generate_fn,
            )
            with mlx_compat_backend.use_installed_voices(
            engine._model_path, engine._voice_snapshot,  # noqa: SLF001
        ):
                active_generate_fn(**gen_kwargs)

        for _ in range(20):
            wav_paths = sorted(temp_path.rglob("*.wav"))
            if wav_paths:
                break
            time.sleep(0.05)

        if wav_paths:
            break

    return wav_paths


def synthesize_to_temp_audio(
    engine,  # noqa: ANN001
    text: str,
    voice: str,
    speed: float,
    *,
    generate_fn=None,  # noqa: ANN001
    inject_model_arg=None,  # noqa: ANN001
    inject_output_args=None,  # noqa: ANN001
) -> np.ndarray:
    """Synthesize via temp WAV files and return concatenated audio."""
    import soundfile as sf

    model = _direct_generation_model(engine, generate_fn)
    if model is not None:
        pcm, _sample_rate = _generate_pcm16(engine, model, text, voice, speed)
        return _pcm16_to_float(pcm)

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        wav_paths = generate_temp_wav_paths(
            engine,
            text=text,
            voice=voice,
            speed=speed,
            temp_path=temp_path,
            generate_fn=generate_fn,
            inject_model_arg=inject_model_arg,
            inject_output_args=inject_output_args,
        )

        if not wav_paths:
            raise SynthesisError("No audio file generated")

        audio_segments: list[np.ndarray] = []
        for wav_path in wav_paths:
            segment, _ = sf.read(str(wav_path), dtype="float32")
            audio_segments.append(np.asarray(segment, dtype=np.float32))

        if not audio_segments:
            raise SynthesisError("No audio data in generated files")

        if len(audio_segments) == 1:
            return audio_segments[0]
        return np.concatenate(audio_segments)
