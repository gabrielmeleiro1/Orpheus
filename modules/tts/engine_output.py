"""Audio-output helpers for the legacy TTS engine facade."""

from __future__ import annotations

import tempfile
import time
import wave
from pathlib import Path

import numpy as np

from modules.errors import SynthesisError

from . import mlx_compat as mlx_compat_backend

_WAV_COPY_FRAMES = 65_536


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

    with engine._model_lock:  # noqa: SLF001
        gen_kwargs = {
            "text": text,
            "voice": voice,
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
        except Exception:
            pass


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

    with engine._model_lock:  # noqa: SLF001
        gen_kwargs = {
            "text": text,
            "voice": voice,
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


def synthesize_to_file(
    engine,  # noqa: ANN001
    text: str,
    output_path: Path | str,
    voice: str | None = None,
    speed: float = 1.0,
) -> Path:
    """Synthesize speech and save to a WAV file."""
    import soundfile as sf

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    audio = engine.synthesize(text, voice, speed)
    sf.write(str(output_path), audio, engine.config.sample_rate, subtype="PCM_16")
    return output_path
