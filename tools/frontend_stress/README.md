# Frontend Stress Harness

This Playwright harness drives the real React app and an isolated Python backend. It complements unit tests with actual conversion, cancellation, queue, listening, library, and reconnect checks. It does not qualify the packaged Tauri application or subjective audio quality.

## Setup and verification

Use Node 22 or newer. Follow the repository setup instructions to create the checkout `.venv`, install the desktop dependencies, and cache the local models required by your machine. Then:

```sh
npm --prefix tools/frontend_stress ci
npm --prefix tools/frontend_stress test
npm --prefix tools/frontend_stress start
```

The unit gate includes listener ownership (an unrelated service must never satisfy readiness), fail-closed conversion outcomes, and credential redaction. Run it whenever changing this harness. The browser gate needs local model assets; it runs offline and will never silently download weights.

For the conversion layout regression, run `npm --prefix tools/frontend_stress run test:conversion-layout`. It bundles the production conversion views and styles with synthetic state, so it needs no backend or models. Chromium checks both conversion modes at seven window sizes, source inspection, long titles, tab restoration, focus, voice menus, and resizing. Screenshots and the report are saved under `.artifacts/conversion-layout-regression/`.

If the pinned Playwright Chromium is absent, the harness searches the local Playwright browser cache (macOS, Windows, and Linux layouts). Install a matching browser with `npx playwright install chromium` from this directory if necessary, or set `ORPHEUS_HARNESS_CHROMIUM` to an installed Chromium-based browser such as `C:\Program Files\Google\Chrome\Application\chrome.exe`; that override takes precedence over Playwright's own browser.

## Self-starting mode

The default run launches the checkout `.venv/bin/python` (`.venv/Scripts/python.exe` on Windows) and starts `apps.launchers.desktop_backend` on a random loopback port. Backend data, output, temporary files, and SQLite state live under the gitignored `.state/<timestamp>` directory. `ORPHEUS_DESKTOP_DEV=1` enables the documented local development origin; Hugging Face and Transformers run offline.

Vite listens strictly on IPv4 port 5173. The parent waits for a listening event over the child process's IPC channel, verifies the child PID and port, then probes that IPv4 listener. Chromium resolves `localhost` to IPv4 while retaining the CORS-required `http://localhost:5173` origin. An unrelated IPv4 listener causes an immediate failure; an unrelated IPv6 listener is left untouched.

Spawned processes are registered for cleanup immediately and stopped on completion, errors, or interruption. `--keep-open` intentionally retains them for investigation.

## Options

- `--only=a,b,d`: scenario IDs or names; include prerequisite scenarios when selecting a subset. Unknown or empty selections fail before services start. Missing prerequisites fail explicitly.
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
displays “CUDA” without spellcheck while the API receives the canonical `cuda`.

To run a journey against a packaged sidecar instead of the checkout backend, set
`ORPHEUS_HARNESS_BACKEND_EXE` to `orpheus-desktop-backend.exe` (for example the
one under `desktop/src-tauri/binaries/` or an installer extracted with
`/EXTRACTONLY=1`). The sidecar starts in the isolated state folder with the same
environment overrides, so `--only=a,l,j` then proves what ships.

For offline inference checks of all three precision choices in PowerShell
(including actual quantized layer classes and weight dtypes):

```powershell
$env:ORPHEUS_TEST_CUDA_CLEANER = "1"
$env:ORPHEUS_TEST_HF_HOME = "<copied Hugging Face cache root>"
.venv/Scripts/python.exe -m pytest --no-cov -m gpu tests/test_cleaner_cuda_real.py
Remove-Item Env:ORPHEUS_TEST_CUDA_CLEANER, Env:ORPHEUS_TEST_HF_HOME
```

`ORPHEUS_TEST_HF_HOME` must point to a dedicated copy of the Hugging Face cache
(the folder that contains `hub/`) holding the Qwen weights; the test runs
offline against it and never uses your normal cache.

Failed starts, failed/cancelled conversion terminals when success is required, missing prerequisites, and timeouts **fail**. None implies that a runtime is unavailable. Reconnect is explicitly skipped in reuse mode because the harness does not own those processes. Browser console/page errors fail scenarios; the deliberate backend-disconnection scenario permits a bounded number of connection errors.

Reports are written to `artifacts/report.json`; failed scenarios also capture `artifacts/<id>-<name>.png`. Any failure produces a nonzero exit status. These outputs and isolated runtime state are gitignored.

## Portable frontend performance baseline

`performance.mjs` bundles the real library panel and audio-player hook with
production React and measures them in headless Chromium. It uses synthetic
libraries (5,000 books, with 100/500/1,000 chapters in the selected book) and
paused audio position events. It opens no real libraries and makes no backend
or model requests. Existing desktop dependencies and Playwright are required.

Run the same workload before and after, **serially on each machine**, with other
performance runs and builds stopped:

```sh
node tools/frontend_stress/performance.mjs --revision=<baseline-commit> --label=baseline --output-dir=.artifacts/local-performance/frontend-baseline
node tools/frontend_stress/performance.mjs --label=candidate --output-dir=.artifacts/local-performance/frontend-candidate
```

`--revision` loads application source from the local Git commit without changing
the checkout; both runs use the current synthetic fixture. Each output directory
must be unused. Reports retain the bundle, raw samples, source/fixture hashes,
browser version, and machine metadata. The harness warms up each chapter count,
then takes five library trials and six player batches after warm-up. It checks
that all chapter controls remain present, book rows stay bounded, metadata edits
apply, and the player highlights the expected chapter.

Compare median and p95 render/layout costs, plus the median cost of 500 position
updates. These measure shared frontend work; they do not measure native Tauri
WebView speed, audio decoding, network latency, inference, or end-to-end startup.
Avoid absolute timing assertions in CI; the unit regressions instead verify
bounded chapter work and correct playback/interaction behavior.

## Interaction-state audit

`interaction-audit.mjs` checks what static screenshots miss: tooltips, menus,
focus, keyboard activation, high contrast and long text in the states users
actually reach. It starts an isolated backend and Vite (like the default run,
so it needs the cached voice model and port 5173), seeds real data through the
backend API, and visits each of its 41 states at four window sizes: 960×640
(the app's minimum), 1200×860, 1440×900 and 1920×1080. `--list-states` and the
report name the states:

```sh
npm --prefix tools/frontend_stress run audit:interaction
node tools/frontend_stress/interaction-audit.mjs --state=library-*,settings --viewport=960x640
node tools/frontend_stress/interaction-audit.mjs --quick
node tools/frontend_stress/interaction-audit.mjs --list-states
```

`--state` takes ids or `prefix-*` patterns; `--viewport` takes a listed size, a
width, or any other `WIDTHxHEIGHT` (to probe a smaller window); `--out` changes
the output folder (default `.artifacts/interaction-audit/`); `--headed` shows
the browser. `--quick` is for iterating: the smallest and largest window size
only (unless `--viewport` is given) and no activation or forced-colours pass.
The full run is the gate. A malformed argument or an unknown state or window
size stops the run before anything starts.

Interactive elements are discovered from the DOM, so new UI is covered without
editing the script. At every window size the audit hovers each one with the
real mouse, walks the page with real Tab presses, and opens every disclosure,
popup and dialog it reaches. After each action it asserts:

- floating layers (tooltips, menus, dialogs, fixed bars) are inside the window, not cut by a clipping ancestor, and a tooltip's own text is not cut; a tooltip does not cover the control it explains;
- controls, and text, are not clipped by an ancestor unless a scroll container can still reveal them, and no opaque layer covers them (fixed layers such as the player bar may only cover what can be scrolled out from under them);
- a scroll area whose top or bottom edge lies under a fixed bar reserves at least that overlap as padding, so its last item can always be scrolled into view (`scroll-area-under-fixed-layer`);
- overflowing text shows an ellipsis or line clamp and its full value is readable through a `data-tip` or the shared tooltip layer, which the audit proves by hovering the text and by focusing its control with Tab;
- keyboard focus is visible, measured in pixels once per kind of control and window size: a focus ring must be at least 2px thick (`focus-indicator-thin`), and 85% of its band must change with 3:1 contrast against the unfocused screenshot; an indicator that is not a ring must change as many pixels as a 2px band around the control (`focus-indicator-faint`). The focused control is on screen, not clipped or covered; focus moves into opened dialogs and stays inside modal ones;
- every way out of an overlay (Escape, its Close or Cancel button, a click on its backdrop) closes it and returns focus to the trigger;
- focus never falls to `<body>`: the app's focus rescue reports each catch, and a catch where the control was still there is a failure;
- every focusable control is reachable with Tab (a dialog that its toggle closes and reopens is rebuilt from new nodes; controls already walked are matched to their copies and the walk continues from the toggle), the page never scrolls sideways, and hovering or opening an overlay moves nothing else (a move counts only if undoing the action undoes it and, for hover, a second hover repeats it, so live progress text is not blamed on the mouse);
- hovering, focusing and opening never send a request that changes data, and the page raises no errors.

Once per state, at 1440×900:

- the activation pass presses ordinary buttons with Enter, and again with Space if they are still there (each button once per run, at most 30 per state), and checks that focus then lands on something visible, on screen and inside any open modal, also when the button disabled itself, unmounted or changed the view. Requests that would change data or open Explorer are answered with faithful mocked successes, so the transition happens without touching the backend;
- the forced-colours pass (Windows High Contrast, dark and light) checks that every labelled control's text is drawn against what is behind it, and that selected or current controls look different from their siblings.

States cover first-run setup and its Customize view, startup checks, an
empty, one-book, long-text
(200-character unbroken, CJK, RTL, missing author) and 200-book library (both
pages), the book view, a 120-chapter album and its Now Playing list, player
and Now Playing, Create when empty, with a source added and with pasted text,
Settings, each inline confirmation (delete book, restore backup, clean
up, delete model, remove runtime data), conversion running (seen from Create
and from the library), details drawer,
cancelling and failing, the completion notice, model download running and
failed, runtime not ready, the runtime-check notice (checking and given up)
over a player session, a lost backend, and transitions: a job ending while its
sidebar button (and its tooltip) or the drawer's Cancel has focus, and the
completion notice dismissing itself while its Dismiss has focus. Most are
live; `--list-states` names the mocked ones and how. Mocked payloads come from
`lib/interactionMocks.mjs`, built from the real responses, and
`tests/interactionMocks.test.mjs` checks each against the desktop app's wire
fixtures and response types so they cannot drift from the API.

The run exits non-zero on any failure. `report.json` lists every distinct
failure with the state, window size, action and a screenshot under
`failures/` (the offending box is outlined), plus per-state counts and the
Tab order; `states/` holds one screenshot of each state as entered. The
pass/fail rules live in `lib/interactionChecks.mjs` and are unit tested; each
narrow exception is documented next to the rule it relaxes.

## Screen capture

`screens.mjs` captures the main screens from the real app for visual review.
Like the audit, it starts an isolated backend and Vite on port 5173 and seeds
a small library with real conversions, so the voice model must be cached:

```sh
node tools/frontend_stress/screens.mjs
node tools/frontend_stress/screens.mjs --out=.artifacts/screens-candidate --sizes=1280x800,960x640
```

`--sizes` takes `WIDTHxHEIGHT` pairs (default `1600x1000,1280x800`) and `--out`
the output folder (by default a `screens` folder under `.artifacts/`); `--scale=2`
captures at twice the pixel density. It writes one PNG per screen
and size: setup, the empty and filled library, a book, Now Playing, Create
(empty, inspected, pasted, running), the conversion details and event log, and
Settings. It fails if a seeded conversion does not complete; it makes no
layout checks.

## Model setup end-to-end check

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
