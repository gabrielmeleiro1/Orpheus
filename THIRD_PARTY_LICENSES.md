# Third-Party Licenses

The Orpheus **source code** in this repository is licensed under the MIT
License (see [LICENSE](LICENSE)). Orpheus depends on third-party components
with their own licenses, several of which are copyleft. This file summarizes
them; the authoritative license text for each dependency ships with the
package itself.

## Copyleft components

**Packaged builds of Orpheus (DMG/installer) are not MIT-licensed works.**
A build that bundles the components below is a combined work subject to their
licenses, including the obligation to make corresponding source available to
anyone who receives the binary. This repository, together with the pinned
manifests in `requirements/`, `desktop/package-lock.json`, and
`desktop/src-tauri/Cargo.lock`, constitutes that corresponding source for the
first-party code.

| Component | License | Where it is used |
| --- | --- | --- |
| PyMuPDF (via `pymupdf4llm`) | AGPL-3.0 (or commercial) | PDF ingestion, all profiles (`requirements/base.txt`) |
| EbookLib (`ebooklib`) | AGPL-3.0 | EPUB ingestion, all profiles (`requirements/base.txt`) |
| espeak-ng (bundled by `espeakng-loader`) | GPL-3.0 | Phonemization fallback, `mlx` and `cuda` profiles |
| `phonemizer-fork` | GPL-3.0 | Phonemization, `mlx` profile |
| FFmpeg (`ffmpeg`/`ffprobe`) | LGPL-2.1+ or GPL-2.0+ depending on the build | Audio encoding. Not committed to this repository; `scripts/build_desktop_backend.py` copies the build host's binaries into packaged builds. Homebrew and most distro builds enable GPL components, so assume GPL unless you pin an LGPL build. |
| `num2words` | LGPL-2.1 | Number normalization, `mlx` profile |

## Notable permissive components

- **Kokoro TTS models** (`hexgrad/Kokoro-82M`, `mlx-community/Kokoro-82M-bf16`):
  Apache-2.0. Downloaded at setup time, never committed.
- **JetBrains Mono** (`@fontsource/jetbrains-mono`): SIL Open Font License 1.1.
  Bundled into desktop builds; the OFL requires this notice to accompany
  distributions.
- Python: FastAPI, uvicorn, numpy, scipy, torch, lxml, psutil (BSD);
  soundfile, pydub, beautifulsoup4, spaCy, thinc, bitsandbytes, misaki, mlx,
  mlx-lm, mlx-audio, language-tags (MIT); transformers, accelerate,
  huggingface-hub, kokoro, aiohttp, python-multipart (Apache-2.0);
  tqdm (MPL-2.0/MIT); packaging (Apache-2.0/BSD).
- npm direct dependencies (React, Vite, Vitest, `@tauri-apps/*`, TypeScript)
  and Rust crates (Tauri, serde, regex, and friends) are MIT and/or
  Apache-2.0. See `desktop/package-lock.json` and
  `desktop/src-tauri/Cargo.lock` for the full trees.

License names above reflect the pinned versions in this repository's
manifests; re-verify when upgrading dependencies or preparing a binary
release.
