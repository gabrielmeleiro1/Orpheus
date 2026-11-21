# Environment Variables

This page lists the settings read by `modules/`, `apps/`, and `scripts/`, the
inputs the desktop shell passes to the backend, and the opt-in inputs used by
tests and harnesses. Defaults describe running the Python backend from source
unless a desktop or script default is stated. `.env.example` at the repository
root is a starting point for `.env.local`. Runtime provider selection is
covered in [RUNTIME_SELECTION.md](RUNTIME_SELECTION.md).

Most users never need these: the desktop app sets paths itself, and the Setup
screen and Settings choose the runtime and cleaner.

## Application configuration

When the backend runs from a source checkout (`python` from the repository, or
`npm run desktop:dev`, which starts it in the repository root), `AppConfig` loads
`.env.local` from the current working directory before resolving settings. The
packaged backend never reads `.env.local`. Existing process variables win,
including empty values. The file accepts `KEY=VALUE`, strips whitespace and
matching outer quotes, and ignores comments and lines without `=`. It does not
expand variable references or parse shell commands.

The following variables are user-facing for direct Python launches. The desktop
shell owns the four path variables when launching the backend or setup: it replaces
inherited values with paths under its resolved app-data root. Ordinary inherited
`AUDIOBOOK_*` paths do not change that desktop root.

| Variable | Default | Meaning |
|----------|---------|---------|
| `AUDIOBOOK_DATA_DIR` | `data` | Application data directory. Setting it also moves setup state (`runtime-setup.json`) to this directory's parent. |
| `AUDIOBOOK_OUTPUT_DIR` | `output` | Audiobook output directory. |
| `AUDIOBOOK_TEMP_DIR` | `temp` | Temporary conversion directory. |
| `AUDIOBOOK_DB_PATH` | `<data_dir>/audiobooks.db` | SQLite database. |
| `TTS_PROVIDER` | unset | Kokoro provider override; when unset, the platform default is chosen later ([RUNTIME_SELECTION.md](RUNTIME_SELECTION.md)). |
| `TTS_DEVICE` | unset | Runtime device override. |
| `TTS_MODEL_NAME` | unset | Kokoro model override. |
| `CLEANER_BACKEND` | `mlx` | Cleaner backend: `basic`, `mlx`, `cuda`, or `api`. Saved setup replaces it. |
| `CLEANER_MODEL_NAME` | `mlx-community/Llama-3.2-3B-Instruct-4bit` | Cleaner model, remapped per backend as described below. Saved setup replaces it. |
| `CLEANER_CUDA_DEVICE` | `cuda` | CUDA cleaner device. |
| `CLEANER_CUDA_QUANTIZATION` | `4bit` | CUDA cleaner precision: `4bit`, `8bit`, or `fp16`. Saved setup replaces it. Capability probes also read it, lowercasing and falling back to `4bit`. |
| `CLEANER_API_BASE_URL` | `https://api.openai.com/v1` | Remote cleaner endpoint. |
| `CLEANER_API_KEY_ENV` | `OPENAI_API_KEY` | Name of the environment variable holding the API key, not the key itself. |
| `CLEANER_API_MODEL` | `gpt-5-nano` | API model. |
| `CLEANER_API_GLOBAL_CONCURRENCY` | `6` | Global API request concurrency (integer, at least 1). |
| `WORKER_POOL_REUSE_ENABLED` | `true` | Reuse worker pools. |
| `SAVE_CLEANING_DEBUG` | `false` | Save cleaning debug artifacts. |
| `PDF_EXTRACTION_BACKEND` | `fitz` | `fitz`, `pymupdf4llm`, or `auto`, case-insensitively; other values are ignored. |
| `AUDIOBOOK_ENABLE_PARALLEL` | `false` | Process chapters in parallel. A chapter limit of 1 or less turns it off. |
| `AUDIOBOOK_MAX_PARALLEL_CHAPTERS` | `4` | Parallel chapter limit (integer, at least 1). |
| `AUDIOBOOK_EMIT_CHAPTER_MP3_ARTIFACTS` | `false` | Also write intermediate chapter MP3s. |
| `AUDIOBOOK_ENABLE_CHAPTER_AAC_PACKAGING` | `true` | Encode chapter AAC while synthesis continues and package from it. |

Blank values are ignored, except that a whitespace-only `TTS_PROVIDER`,
`TTS_DEVICE`, `TTS_MODEL_NAME`, or `CLEANER_API_MODEL` clears that setting.
Invalid integers are ignored, and valid ones are raised to at least 1. For
booleans, after trimming and lowercasing, `0`, `false`, `no`, and `off` mean
false; any other nonblank value means true. Paths expand `~`; ordinary
home-relative paths use `HOME` when nonempty, including the `~\` spelling.
Other tilde forms use host account lookup.

**Precedence for code that constructs `AppConfig`.** Environment values,
including `.env.local`, win over constructor arguments, with two exceptions. The
three directory variables apply only when the constructor path equals its
default (`Path("data")`, `Path("output")`, or `Path("temp")`), even when that
default was passed explicitly. `AUDIOBOOK_DB_PATH` applies only when the
constructor `db_path` is `None`. Constructor paths are used as supplied.

Two model precedence details are intentional:

- On the CUDA backend, a curated MLX model supplied by constructor/default maps to
  `Qwen/Qwen2.5-1.5B-Instruct`. A nonblank `CLEANER_MODEL_NAME` counts as explicit
  and prevents that CUDA remapping. A CUDA catalog model used with a non-CUDA
  backend still maps to the default MLX model.
- Saved setup is applied separately after construction. It sets cleaner backend,
  selected cleaner model, and CUDA precision. With no cleaner group, it selects
  `basic` and retains the already resolved model. It does not set TTS provider.

`modules/app/config_resolution.py` resolves supplied inputs without filesystem or
process-environment access. Provenance labels are `constructor`, `env`, `setup`,
and `default`; `.env.local` values count as `env`. Diagnostics redact every value.
Construction keeps file loading, directory creation, and setup-state application
outside resolution. No root or default directory migration is performed.

## Runtime and credentials

These variables are user-facing unless described as internal. They are read at
the point of use, outside `AppConfig`, so their precedence differs from the table
above.

| Variable | Default | Meaning and precedence |
|----------|---------|------------------------|
| `OPENAI_API_KEY`, or the name selected by `CLEANER_API_KEY_ENV` | unset | Secret API credential. Cleaner requests prefer a nonblank explicit `api_key`, then env. Voice recommendation prefers an explicit key whenever it is not `None`, then env. Missing keys fail when the API is used. |
| `CLEANER_API_CA_BUNDLE` | unset | Extra CA certificate bundle. A truthy cleaner `api_ca_bundle` takes precedence, then the chosen value is trimmed. |
| `CLEANER_API_INSECURE_SSL` | false | Disables certificate and hostname verification when `1`, `true`, `yes`, or `on`. Combined with explicit `api_allow_insecure_ssl` using OR. |
| `AUDIOBOOK_FFMPEG_PATH`, `FFMPEG_PATH` | unset | First usable executable in this order wins; then bundled binaries, `PATH`, and common system binary directories. |
| `AUDIOBOOK_FFPROBE_PATH`, `FFPROBE_PATH` | unset | Same executable precedence for ffprobe. |
| `AUDIOBOOK_FFMPEG_TIMEOUT_SECONDS` | max(120 s, 0.5 s per second of audio) when the audio length is known, otherwise 7200 s | Fixed timeout for every FFmpeg-family subprocess; replaces the length-based default, so a low value can stop packaging a long book. An explicit function argument wins over it; minimum is 0.001 seconds. Non-numeric input is ignored. |
| `AUDIOBOOK_TTS_TORCH_COMPILE` | false | Opt into compilation for package Kokoro. Explicit `torch_compile` runtime option wins in both directions. Only `1`, `true`, `yes` enable it. |
| `CUDA_VISIBLE_DEVICES` | inherited | `-1` disables the direct CUDA hardware probe. Device visibility is also consumed by the CUDA runtime. |
| `PHONEMIZER_ESPEAK_DATA_PATH`, `ESPEAK_DATA_PATH` | loader/bundled data | Source MLX compatibility checks these in order when the loader data path is too long; only usable short paths qualify. The desktop launcher sets both to its resolved speech-data path. |
| `PHONEMIZER_ESPEAK_LIBRARY` | resolved library | Internal launcher output to phonemizer; overwritten with the resolved library path. |
| `MLX_METAL_MEMORY` | computed per worker | Internal worker output: 90% of available memory divided by active worker count, in bytes; replaces an inherited value when the MLX policy supplies it. |

`AppConfig` contains the credential variable name only. Actual secret fields
include cleaner/pipeline `api_key` / `cleaner_api_key` and the desktop session
token. The desktop token is generated at launch and written in the readiness
payload; it is not configured by an environment variable. Credentials inherited
for downstream libraries, including Hub tokens, are not copied into configuration
resolution or its diagnostics.

## Cache and host paths

Cache overrides are user-facing; OS directory variables are inherited host inputs.

| Variable | Default | Meaning and precedence |
|----------|---------|------------------------|
| `HOME`, `USERPROFILE` | host account home | Host-provided home inputs. AppConfig explicitly uses `HOME` for home-relative env paths; platform path APIs handle other expansion. Rust home checks prefer `HOME`, then `USERPROFILE`. |
| `LOCALAPPDATA` | `<home>/AppData/Local` | Windows install directory (`<LOCALAPPDATA>/Orpheus`). Standalone setup only reads legacy state from `<LOCALAPPDATA>/Orpheus/runtime-setup.json` until its first write. |
| `APPDATA` | `<home>/AppData/Roaming` | Windows standalone setup root is `<APPDATA>/com.orpheus.desktop` (the desktop app data directory) unless `AUDIOBOOK_DATA_DIR` is set. |
| `XDG_DATA_HOME` | `<home>/.local/share` | Linux/other Unix standalone setup root is `<XDG_DATA_HOME>/com.orpheus.desktop` unless `AUDIOBOOK_DATA_DIR` is set. macOS uses `<home>/Library/Application Support/com.orpheus.desktop`. Legacy state under the older `orpheus` and `Orpheus` folder names is read until the first setup write. |
| `XDG_CACHE_HOME` | `<home>/.cache` | Cache base for Hub fallback and non-macOS speech data. macOS speech data uses `<home>/Library/Caches/Orpheus/espeak`. |
| `HF_HOME` | `<XDG_CACHE_HOME>/huggingface` | User-facing Hub cache root used by the Windows symlink probe. Falls back to `<home>/.cache/huggingface`. |
| `HF_HUB_CACHE`, `HUGGINGFACE_HUB_CACHE` | `<HF_HOME>/hub` | User-facing model cache. The Windows probe prefers `HF_HUB_CACHE`, then the legacy name, then the root-derived default; expands home and environment references. |
| `HF_HUB_DISABLE_SYMLINKS` | unset | User-facing Hub setting. Presence skips the Windows symlink probe; a failed probe sets it to `1`. If Hub is already imported, the setting is synchronized into its constants. |
| `HF_HUB_DISABLE_SYMLINKS_WARNING` | unset | User-facing Hub warning setting; set to `1` on a failed Windows symlink probe. Existing Hub constants are synchronized. These two Hub flags recognize `1`, `ON`, `YES`, `TRUE` case-insensitively. |
| `HF_HUB_DISABLE_PROGRESS_BARS`, `TQDM_DISABLE` | `1` when MLX progress suppression runs | Internal defaults passed to libraries using `setdefault`; existing values win. |
| `PROGRAMDATA` | `C:/ProgramData` | Windows launcher base for its short speech-data path candidates. |
| `SystemRoot`, `WINDIR` | typically `C:\Windows` | Windows system executable lookup. Packaged FFmpeg validation prefers `SystemRoot`, then `WINDIR`, then `C:\Windows`; smoke cleanup uses `SystemRoot` with that default. Profile verification checks `SystemRoot` then the Windows system-directory API. |
| `PATH` | inherited host path | Executable discovery. Explicit/bundled FFmpeg paths precede it. Build validation and reduced-path smoke runs can replace it for their subprocesses. |

## Desktop shell inputs

The shell passes the inherited environment with explicit launch values overlaid,
except `PYTHONHOME`, `PYTHONPATH`, `PYTHONPLATLIBDIR`, `PYTHONUSERBASE`, and
`PYTHONEXECUTABLE`, which it removes for the backend and the setup runner so
they cannot redirect the interpreter.
Under its resolved app-data root, data, output, and temporary directories are
`data/`, `output/`, and `temp/`; the database is `data/audiobooks.db`.

| Variable | Default / owner | Meaning and precedence |
|----------|-----------------|------------------------|
| `AUDIOBOOK_DESKTOP_READY_FILE` | internal shell: `<root>/backend-ready.json` | Readiness payload path. Blank means no readiness file; also gates the controller's desktop shutdown acknowledgement. Shell path replaces inherited value. |
| `ORPHEUS_DESKTOP_PARENT_PID` | internal shell: shell PID | Enables the Unix parent watchdog for a positive integer PID. Missing/invalid values disable it; Windows uses process containment instead. |
| `ORPHEUS_DESKTOP_DEV` | internal shell: `1` in debug builds | Enables the Vite development CORS origin for `1`, `true`, `yes`, `on`. Release builds do not set it; the Python server still reads any inherited value. |
| `ORPHEUS_TAURI_SETUP_SUPERVISED` | internal shell: `1` for setup | Exact trimmed `1` keeps setup stages within shell-owned process containment. Otherwise the CLI creates its own process group/session. |
| `AUDIOBOOK_DESKTOP_USE_SIDECAR` | developer: false | In debug builds, `1`, `true`, `yes`, `on` select the packaged sidecar instead of repository Python. Release builds always use the sidecar. |
| `AUDIOBOOK_DESKTOP_PYTHON` | test-only: repository `.venv` Python, then `python` | Nonblank override for Rust test subprocess helpers. Production backend/setup interpreter selection does not consult it or `PATH`. |
| `ORPHEUS_QUALIFICATION_RUNTIME_ROOT`, `ORPHEUS_QUALIFICATION_RELEASE_ID` | qualification-only: unset | Must be supplied together. Replace the desktop app-data root only after directory, containment, and release sentinel checks; invalid inputs fail rather than falling back to live data. |

## Build, smoke, and benchmark scripts

These are developer inputs, not ordinary desktop settings.

| Variable | Default | Meaning and precedence |
|----------|---------|------------------------|
| `ORPHEUS_RUNTIME_PROFILE` | build: `mlx` on Apple Silicon, otherwise `base`; source launcher: `development` | Build CLI `--runtime-profile` wins over env. With neither, rebuilding preserves the packaged profile marker if present before using the platform default. Accepted explicit values are `base`, `mlx`, `cuda`. Frozen launchers use the packaged marker and ignore env. |
| `TAURI_ENV_TARGET_TRIPLE`, `CARGO_BUILD_TARGET`, `TARGET` | rustc host, then platform detection | Build target lookup, in listed order, after explicit `--target-triple`; blank values are ignored. |
| `CARGO_MANIFEST_DIR` | internal Cargo build input | Compiled into the Rust shell to locate the repository relative to the Tauri manifest; changing it at launch has no effect. |
| `PYTHONPATH` | inherited | Python smoke backend prepends the repository root, retaining any existing value. |
| `ORPHEUS_SMOKE_PROCESS_GROUP` | internal qualification: unset | Set to `1` by release qualification when it runs the bundled smoke. The smoke then starts its backend in the driver's process group instead of a new session, so a qualification timeout can terminate the whole tree. The smoke always passes its own PID as `ORPHEUS_DESKTOP_PARENT_PID`. |
| `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE` | inherited | Conversion-journey smoke runs force these to `1` unless already enabled (`1`, `true`, `yes`, `on`). Passed to the model libraries. |
| `USER`, `USERNAME` | inherited | Read by release qualification to redact the local account name from reports and evidence. |

`scripts/verify_runtime_profiles.py` intentionally forwards only nonempty values
from this host-environment allowlist; defaults and interpretation belong to the
OS or downstream Python/network libraries:

| Variables | Purpose |
|-----------|---------|
| `PATH`, `HOME`, `USERPROFILE`, `LOCALAPPDATA`, `APPDATA`, `PROGRAMFILES` | Executable and user/application paths. |
| `TMPDIR`, `TEMP`, `TMP` | Temporary directory selection. |
| `SYSTEMROOT`, `WINDIR` | Windows system paths. |
| `LANG`, `LC_ALL` | Locale. |
| `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE` | TLS trust roots for subprocess libraries. |
| `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, `ALL_PROXY`, `http_proxy`, `https_proxy`, `no_proxy`, `all_proxy` | Network proxy configuration. |

That verifier then sets internal subprocess values unconditionally:
`PYTHONDONTWRITEBYTECODE=1`, `PYTHONNOUSERSITE=1`, `PIP_CONFIG_FILE=<os.devnull>`,
`PIP_NO_INPUT=1`, and `PIP_DISABLE_PIP_VERSION_CHECK=1`. Other inherited values
are excluded from those verification subprocesses.

## Test and harness inputs

These opt-in inputs are read by tests and test harnesses, never by the app.

| Variable | Default | Meaning |
|----------|---------|---------|
| `ORPHEUS_TEST_CUDA_CLEANER` | unset | `1` enables the real-GPU Qwen inference tests in `tests/test_cleaner_cuda_real.py`. |
| `ORPHEUS_TEST_HF_HOME` | unset | Required with the variable above: a dedicated, copied Hugging Face cache root (its `hub/` folder holds the models). The test runs offline against it. |
| `ORPHEUS_REGENERATE_WIRE_FIXTURES` | unset | `1` rewrites the wire-payload fixtures checked by `tests/test_wire_payload_contracts.py`. |
| `ORPHEUS_HARNESS_CHROMIUM` | unset | Path to a Chromium-based browser for `tools/frontend_stress`; takes precedence over Playwright's own browser. |
