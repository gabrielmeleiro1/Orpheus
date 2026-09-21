# Orpheus frontend stress harness

This Playwright harness drives the real React app and an isolated Python backend. It complements unit tests with actual conversion, cancellation, queue, listening, library, and reconnect checks. It does not qualify the packaged Tauri application or subjective audio quality.

## Setup and verification

Use Node 20 or newer. Follow the repository setup instructions to create the checkout `.venv`, install the desktop dependencies, and cache the local models required by your machine. Then:

```sh
npm --prefix tools/frontend_stress ci
npm --prefix tools/frontend_stress test
npm --prefix tools/frontend_stress start
```

The unit gate includes listener ownership (an unrelated service must never satisfy readiness), fail-closed conversion outcomes, and credential redaction. Run it whenever changing this harness. The browser gate needs local model assets; it runs offline and will never silently download weights.

If the pinned Playwright Chromium is absent, the harness searches the local Playwright browser cache. Install a matching browser with `npx playwright install chromium` from this directory if necessary.

## Self-starting mode

The default run launches the checkout `.venv/bin/python` (`.venv/Scripts/python.exe` on Windows) and starts `apps.launchers.desktop_backend` on a random loopback port. Backend data, output, temporary files, and SQLite state live under the gitignored `.state/<timestamp>` directory. `ORPHEUS_DESKTOP_DEV=1` enables the documented local development origin; Hugging Face and Transformers run offline.

Vite listens strictly on IPv4 port 5173. The parent waits for a listening event over the child process's IPC channel, verifies the child PID and port, then probes that IPv4 listener. Chromium resolves `localhost` to IPv4 while retaining the CORS-required `http://localhost:5173` origin. An unrelated IPv4 listener causes an immediate failure; an unrelated IPv6 listener is left untouched.

Spawned processes are registered for cleanup immediately and stopped on completion, errors, or interruption. `--keep-open` intentionally retains them for investigation.

## Options

- `--only=a,b,d`: scenario IDs or names; include prerequisite scenarios when selecting a subset. Missing prerequisites fail explicitly.
- `--headed`: visible browser.
- `--state-dir=<path>`: retain/reuse a particular isolated backend state directory.
- `--keep-open`: retain spawned processes after the run.
- `--frontend-port=<n>`: normally keep 5173 because the backend permits that exact development origin.

Reuse mode requires all three flags:

```sh
node tools/frontend_stress/run.mjs \
  --backend-url=http://127.0.0.1:54231 \
  --token=<desktop-api-token> \
  --frontend-url=http://localhost:5173
```

Credentials are omitted/redacted from harness diagnostics and reports. Avoid sharing shell history containing a reuse token. Self-start mode requires no token argument.

## Scenarios

| ID | Scenario | Checks |
| --- | --- | --- |
| a | connect | Runtime probe, asynchronous setup continuation, backend connection and model options |
| b | add-book-paste | Explicit pasted title survives content preflight; chapter information appears |
| c | add-book-files | File upload and preflight for EPUB, PDF, Markdown and TXT; malformed/empty fixture handling |
| d | options-sweep | Engine, voice, voice filter, speed and cleaner selections |
| e | convert-single | Isolated single source starts, completes and appears in the library |
| l | cuda-cleaner (opt-in) | Switch Qwen through 4bit → 8bit → fp16 → 4bit in one session; each choice reaches the API and produces successful cleanup and audio |
| f | convert-cancel | Multi-section conversion cancels, stop control clears and start becomes usable |
| g | queue-run | Two sources convert sequentially and both queue rows reach Done |
| h | rapid-fire | Duplicate start suppression, tab switching, source inspection and title-edit races |
| j | player-smoke | Playback, pause, seek, volume, reload/resume and rapid book/chapter navigation |
| i | library-crud | Title/author persistence across reload and explicit confirmed deletion |
| k | reconnect | Backend loss becomes an error, replacement endpoint reconnects, library survives |

The PDF and EPUB repository fixtures are deliberately empty/malformed, so they are preflight cases, not successful conversion fixtures. Cancellation uses generated multi-section text with enough work to exercise interruption reliably.

To check the CUDA settings through cleanup, audio generation, and playback, run
`node tools/frontend_stress/run.mjs --only=a,l,j`. A passing `l` scenario requires
an `llm_success` trace and the expected repaired text; rule-only cleanup or a
successful conversion with a cleaner fallback does not pass. Missing CUDA or
Qwen weights fail the test. The model must already be installed. Scenario `l`
runs only when explicitly selected with `--only`. It also checks that the device
displays “Cuda” without spellcheck while the API receives the canonical `cuda`.

For offline inference checks of all three precision choices in PowerShell
(including actual quantized layer classes and weight dtypes):

```powershell
$env:ORPHEUS_TEST_CUDA_CLEANER = "1"
.venv/Scripts/python.exe -m pytest --no-cov -m gpu tests/test_cleaner_cuda_real.py
Remove-Item Env:ORPHEUS_TEST_CUDA_CLEANER
```

Failed starts, failed/cancelled conversion terminals when success is required, missing prerequisites, and timeouts **fail**. None implies that a runtime is unavailable. Reconnect is explicitly skipped in reuse mode because the harness does not own those processes. Browser console/page errors fail scenarios; the deliberate backend-disconnection scenario permits a bounded number of connection errors.

Reports are written to `artifacts/report.json`; failed scenarios also capture `artifacts/<id>-<name>.png`. Any failure produces a nonzero exit status. These outputs and isolated runtime state are gitignored.
# Model setup end-to-end check

From the repository root, build the frontend with `npm --prefix desktop run build`,
then run `node tools/frontend_stress/model-setup.mjs`. Pass a packaged backend
executable as the optional first argument to test the release bundle.

This opt-in check needs a CUDA GPU and complete, receipt-verified Kokoro,
Qwen 2.5 1.5B and Qwen 2.5 3B snapshots in the default Hugging Face cache.
It creates isolated application data and an isolated model cache, using hard links
to avoid duplicating weights. It installs the selected model from cached weights,
checks saved precision after reload, switches precision through four real
conversions, deletes one isolated model, and checks that books and playback still
work. It never deletes the original cache. Reports and screenshots are written
under `.artifacts/model-setup-check/`.
