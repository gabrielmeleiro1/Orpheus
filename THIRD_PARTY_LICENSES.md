# Third-Party Licenses

The Orpheus **source code** in this repository is licensed under the MIT
License (see [LICENSE](LICENSE)). Orpheus depends on third-party components
with their own licenses, several of them copyleft. This file summarizes them;
the authoritative license text for each dependency ships with the package
itself. It is a summary, not legal advice.

## Packaged builds

The MIT License covers the Orpheus code. A packaged build (the macOS DMG or
the Windows installer) also bundles the copyleft components below, so the
build as a whole is a combined work subject to their terms, in particular
AGPL-3.0 and GPL-3.0. Anyone who distributes such a build must comply with
those licenses, including making the corresponding source available to
recipients. MIT-licensed code can be combined with these licenses, so the
Orpheus code itself places no extra restrictions on that.

For the Orpheus code, this repository at the release commit, together with the
pinned manifests in `requirements/`, `desktop/package-lock.json`, and
`desktop/src-tauri/Cargo.lock`, is the corresponding source. Bundled
third-party components come from their upstream projects at the pinned
versions.

## Copyleft components

| Component | License | Where it is used |
| --- | --- | --- |
| PyMuPDF (via `pymupdf4llm`) | AGPL-3.0 (or Artifex commercial) | PDF ingestion; every profile (`requirements/base.txt`) |
| EbookLib (`ebooklib`) | AGPL-3.0 | EPUB ingestion; every profile (`requirements/base.txt`) |
| espeak-ng library and `espeak-ng-data` (bundled by `espeakng-loader`) | GPL-3.0-or-later | Phonemization fallback; `mlx` and `cuda` profiles. The packaged macOS sidecar also carries a copy of the speech data at its top level. |
| libsndfile (bundled in the `soundfile` wheel as `_soundfile_data`) | LGPL-2.1-or-later | Audio file I/O; every profile. Shipped as a separate shared library, so it can be replaced. |
| `phonemizer-fork` | GPL-3.0-or-later | Phonemization; `mlx` profile directly, `cuda` profile through `kokoro` → `misaki[en]` |
| `num2words` | LGPL-2.1 | Number normalization; `mlx` profile directly, `cuda` profile through `misaki[en]` |
| FFmpeg (`ffmpeg`, `ffprobe`) | LGPL-2.1+, GPL-2.0+, or GPL-3.0+, depending on the build | Audio encoding. Not committed; `scripts/build_desktop_backend.py` copies the build machine's binaries into packaged builds. Homebrew and most Linux distribution builds are GPL. The gyan.dev Windows builds installed by `winget install Gyan.FFmpeg` are configured with `--enable-gpl --enable-version3`, so a Windows installer built from them ships GPL-3.0-or-later FFmpeg. Run `ffmpeg -version` on the build machine to check, and ship the matching FFmpeg source with any binary release. |
| PyInstaller | GPL-2.0-or-later with a bootloader exception | Builds the backend sidecar. Its bootloader ships in every packaged build; the exception allows distributing the bundled app under any terms. |

## NVIDIA CUDA libraries

The `cuda` profile installs the CUDA build of PyTorch (`torch==2.13.0+cu126`),
whose wheel includes NVIDIA runtime libraries: the CUDA runtime, cuBLAS,
cuFFT, cuRAND, cuSOLVER, cuSPARSE, NVRTC, nvJitLink, CUPTI, and cuDNN. The
Windows installer bundles them because it ships the `cuda` profile. They are
proprietary and redistributed under the NVIDIA CUDA Toolkit End User License
Agreement and the NVIDIA cuDNN Software License Agreement, not under an open
source license.

## Notable permissive components

- **Python:** FastAPI, beautifulsoup4, pydub, bitsandbytes, spaCy, thinc,
  `en-core-web-sm`, mlx, mlx-lm, mlx-audio, language-tags, and loguru (MIT);
  uvicorn, numpy, scipy, lxml, soundfile, and psutil (BSD); PyTorch (BSD-style,
  with some Apache-2.0 and MIT parts); transformers, accelerate,
  huggingface-hub, kokoro, misaki, multidict, and python-multipart
  (Apache-2.0); aiohttp (Apache-2.0 and MIT); tqdm (MPL-2.0 and MIT);
  certifi (MPL-2.0); typing_extensions and aiohappyeyeballs (PSF-2.0); regex
  (Apache-2.0 and CNRI-Python); packaging (Apache-2.0 or BSD-2-Clause).
- **Fonts:** Geist and Geist Mono (`@fontsource-variable/geist`,
  `@fontsource-variable/geist-mono`), Copyright 2024 The Geist Project Authors
  (https://github.com/vercel/geist-font), are licensed under the SIL Open Font
  License 1.1 (https://openfontlicense.org) and are bundled into desktop
  builds.
- **npm:** React, Vite, Vitest, `@tauri-apps/*`, TypeScript, and the rest of
  the tree are MIT, ISC, Apache-2.0, or BSD. `caniuse-lite` (CC-BY-4.0) is a
  build-time dependency only. See `desktop/package-lock.json`.
- **Rust:** Tauri, serde, regex, and most crates are MIT and/or Apache-2.0.
  The tree also includes MPL-2.0 crates (`cssparser`, `selectors`,
  `dtoa-short`, `option-ext`), Unicode-3.0 crates (the ICU4X family, such as
  `icu_normalizer` and `zerovec`), and a few BSD-3-Clause and Zlib crates. See
  `desktop/src-tauri/Cargo.lock`.

## Models downloaded at runtime

Model weights are never committed or bundled. The app downloads them on
request from Hugging Face, and each model has its own license. Check the model
card before use.

| Model | License |
| --- | --- |
| `hexgrad/Kokoro-82M`, `mlx-community/Kokoro-82M-bf16` | Apache-2.0 |
| `mlx-community/Llama-3.2-3B-Instruct-4bit`, `mlx-community/Llama-3.2-1B-Instruct-4bit` | Llama 3.2 Community License |
| `Qwen/Qwen2.5-1.5B-Instruct` | Apache-2.0 |
| `Qwen/Qwen2.5-3B-Instruct` | Qwen Research License (non-commercial use only) |

License names reflect the pinned versions in this repository's manifests.
Re-check them when upgrading dependencies or preparing a binary release.
