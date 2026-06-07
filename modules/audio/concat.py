"""Audio-file concatenation helpers."""

from __future__ import annotations

from pathlib import Path

import numpy as np


def concatenate_audio_files(
    file_paths: list[Path | str],
    output_path: Path | str,
    use_memmap: bool = True,
) -> Path:
    """
    Concatenate multiple audio files into one.

    Uses memory-mapped files for large audio to reduce RAM usage.
    """
    import soundfile as sf

    if not file_paths:
        raise ValueError("No files to concatenate")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    total_samples = 0
    sample_rate = None

    for path in file_paths:
        info = sf.info(str(path))
        total_samples += info.frames
        if sample_rate is None:
            sample_rate = info.samplerate
        elif sample_rate != info.samplerate:
            raise ValueError(f"Sample rate mismatch in {path}")

    if use_memmap and total_samples > 10_000_000:
        temp_mmap_path = output_path.with_suffix(".tmp.npy")

        try:
            output_mmap = np.memmap(
                str(temp_mmap_path), dtype="float32", mode="w+", shape=(total_samples,)
            )

            offset = 0
            for path in file_paths:
                audio, _ = sf.read(str(path), dtype="float32")
                end_offset = offset + len(audio)
                output_mmap[offset:end_offset] = audio
                offset = end_offset

            output_mmap.flush()
            del output_mmap

            final_mmap = np.memmap(str(temp_mmap_path), dtype="float32", mode="r")
            sf.write(str(output_path), final_mmap, sample_rate, subtype="PCM_16")
            del final_mmap

        finally:
            if temp_mmap_path.exists():
                temp_mmap_path.unlink()
    else:
        all_audio = []
        for path in file_paths:
            audio, _ = sf.read(str(path), dtype="float32")
            all_audio.append(audio)

        concatenated = np.concatenate(all_audio)
        sf.write(str(output_path), concatenated, sample_rate, subtype="PCM_16")

    return output_path
