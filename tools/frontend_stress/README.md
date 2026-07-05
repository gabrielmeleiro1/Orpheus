# Orpheus frontend stress harness

A plain Node script (no `@playwright/test`, just the `playwright` library API)
that drives the Orpheus desktop React frontend in a real Chromium browser the
way a user would: typing into fields, clicking buttons, uploading files,
driving the audio player, etc. It is meant to catch integration regressions
that unit tests miss (race conditions, stuck UI states, console errors) by
exercising the whole app end-to-end against a real (but isolated) backend.

## Install

```sh
cd tools/frontend_stress
npm install
```

This installs `playwright` only (no browsers are downloaded automatically —
see "Chromium build" below).

## Running

### Self-starting mode (default)

The harness spawns its own backend and Vite dev server, each pointed at
scratch state, and tears them down when it's done:

```sh
node run.mjs
```

- Backend: `python3 -m apps.launchers.desktop_backend --host 127.0.0.1 --port 0`,
  spawned from the repo root, with `AUDIOBOOK_DATA_DIR` / `AUDIOBOOK_OUTPUT_DIR`
  / `AUDIOBOOK_TEMP_DIR` / `AUDIOBOOK_DB_PATH` pointed at
  `tools/frontend_stress/.state/<timestamp>/` (gitignored). The harness parses
  the single-line `{"event":"ready",...}` JSON the launcher prints to stdout.
- Frontend: the desktop app's local `vite` binary
  (`desktop/node_modules/.bin/vite`), started with `--port 5173 --strictPort`
  and addressed as `http://localhost:5173` (not `127.0.0.1`). This exact
  origin is load-bearing: the desktop backend's CORS allowlist
  (`modules/web/server.py`, `DESKTOP_DEV_ORIGIN`) only accepts
  `http://localhost:5173`, so if you already have your own `npm run dev`
  bound to 5173 the harness will fail fast (`--strictPort`) instead of
  silently picking a different port that the backend would then reject via
  CORS. Stop your own dev server first, or use reuse mode against it. The
  harness polls the URL until it responds.
- Both processes are killed (SIGTERM, then SIGKILL after 5s) when the run
  finishes, unless `--keep-open` is passed.

### Reuse mode

Point the harness at services you already have running (e.g. your own
`npm run dev` plus a manually-launched desktop backend):

```sh
node run.mjs \
  --backend-url=http://127.0.0.1:54231 \
  --token=<desktop-api-token> \
  --frontend-url=http://127.0.0.1:5173
```

All three flags must be supplied together to enable reuse mode.

### Other flags

- `--only=a,b,d` — run a subset of scenarios, by id (`a`..`k`) or name
  (`connect`, `add-book-paste`, ...). Useful while iterating.
- `--headed` — show the browser window instead of running headless.
- `--keep-open` — leave the browser and any self-started backend/frontend
  processes running after the run (for manual poking / debugging). The
  process itself exits, so use this together with `--backend-url`/etc. on a
  subsequent invocation, or just inspect state on disk.
- `--frontend-port=<n>` — change the self-started Vite port (default 5173;
  changing this will break API calls due to the backend's CORS allowlist
  unless you also relax `DESKTOP_DEV_ORIGIN` server-side).
- `--state-dir=<path>` — override the isolated backend state directory.

### Chromium build

The harness uses the `chromium` browser type from the `playwright` package.
If the exact revision `playwright-core` wants isn't cached under
`~/Library/Caches/ms-playwright` (common when the npm dependency is newer
than whatever `npx playwright install` last downloaded), it automatically
falls back to the newest compatible `chromium`/`chromium_headless_shell`
build already on disk (see `lib/chromiumResolver.mjs`) instead of requiring
a network-dependent reinstall.

## What each scenario covers

Scenarios run in one shared browser tab/context, in this order (letters match
the original spec; `i`/`j` are intentionally swapped — see below):

| id | name | covers |
| -- | ---- | ------ |
| a | connect | App loads with `?backend&token`, reaches "connected", capabilities/health load |
| b | add-book-paste | Paste markdown text on the Document tab, preflight resolves with chapter/word info |
| c | add-book-files | Browser file-input upload of `.epub`, `.pdf`, and `.md`; queue shows all three, preflight settles for each |
| d | options-sweep | Cycle TTS engine/voice/speed, voice search filter, and cleaner backend through the UI; no console errors, voice list stays consistent with engine |
| e | convert-single | Convert the pasted markdown end-to-end; progress panel advances through stages; book appears in library. **This is also the "is TTS actually available here" probe** — see below |
| f | convert-cancel | Start converting a multi-chapter source, cancel mid-run via the rail "Stop job" control, verify the UI returns to a startable state with no stuck job. Uses the multi-chapter markdown from scenario c rather than the epub fixture — see note below |
| g | queue-run | Enqueue 2 fresh small markdown sources, run the whole queue, both complete |
| h | rapid-fire | Spam the start control (dispatched synchronously to race the disabled-state guard) and assert exactly one `POST /api/conversions`; rapid tab switching mid-conversion; rapid queue add/reselect while preflights race; edit a queue item mid-preflight — all with zero console errors |
| j | player-smoke | Select a converted book, mute via the volume slider, play/pause/seek, hammer next/prev and (if available) chapter jumps — regression check for stale-load races |
| i | library-crud | Edit a book's title/author, verify the edit persists after a full page reload with the same query params, then delete it and verify the list updates |
| k | reconnect | Kill the backend process mid-session (self-start mode only), verify the UI surfaces a disconnected state without an unbounded console-error flood |

**Why `j` runs before `i`:** `library-crud` (i) ends by deleting a book.
Running it before `player-smoke` (j) would risk leaving no book left for the
player test if only one conversion succeeded. `player-smoke` only touches
playback state, so it's safe to run first; the report still labels each
result with its original scenario letter.

**Why `f` doesn't actually use the epub fixture:** `tests/fixtures/sample.epub`
is a 4-byte stub (just a bare zip magic number — not a real archive), and
`sample.pdf` has zero pages. Both are fine for scenario `c` (uploading and
preflighting a corrupt/empty source should — and does — surface a warning
instead of crashing), but a conversion attempt on either fails almost
instantly, leaving no realistic window to cancel "mid-run". Scenario `f`
instead cancels the multi-chapter `policy_headings.md` upload from scenario
`c`, which takes long enough to clean+synthesize to reliably catch mid-flight.

**Local cleaner backend vs. Cloud API default:** the backend lists "Cloud
API" first among `cleaner_backend_options`, and the frontend's default
cleaner-backend selection (`useCleanerOptions.ts`) just takes the first
*available* option in that server-declared order — it has no preference for
local backends. So even when a local MLX cleaner runtime is available, a
fresh session with no persisted settings can default to "Cloud API", which
requires an API key we don't have and leaves the start button disabled
indefinitely. Scenario `e` (the first scenario that needs to actually start a
conversion) switches the cleaner backend to "Local MLX" via the Settings tab
before proceeding — see `ensureLocalCleanerBackendSelected` in
`scenarios.mjs`. This is filed as a finding, not worked around silently — see
the harness's final report for details.

**TTS availability probe:** scenarios `e` through `j` need a real TTS
synthesis to succeed (kokoro's first model load can take a couple of minutes,
and depends on model weights being downloadable/cached). Scenario `e` is
where this is actually tested: if the conversion it starts doesn't reach
"running" within 20s, or doesn't reach a terminal state within 10 minutes, or
terminates as anything other than `completed`, the scenario reports
`skip: tts-unavailable` and every scenario that depends on a completed
conversion (`g`, `j`, `i`) skips too rather than hanging or failing. `f`
(cancel) and `h` (rapid-fire) only need a job to *start*, not finish, so they
still attempt to run independently and skip only if starting a job itself
fails.

## Artifacts

- `artifacts/report.json` — machine-readable results: per-scenario status
  (`pass`/`fail`/`skip`), duration, console-error count, and (for
  fails/skips) the captured error message.
- `artifacts/<id>-<name>.png` — full-page screenshot taken automatically the
  moment a scenario is marked `fail`.
- A human-readable summary table is also printed to stdout at the end of
  every run.

The harness exits non-zero if any scenario ends in `fail` (skips do not
affect the exit code).

## Design notes

- Selectors are pinned to stable `id`s and class names already present in the
  desktop app's DOM (e.g. `#start-btn`, `#tts-engine-select`,
  `.queue-row`, `#audio-player`), not text content that a refactor could
  reword, since another agent may be actively refactoring the view
  components this harness drives against.
- The custom `<TermSelect>` combobox (not a native `<select>`) is driven via
  `lib/termSelect.mjs`, which opens its listbox and clicks the rendered
  `.term-select-option` rows.
- Console errors are captured globally (`page.on('console'/'pageerror')`) and
  attributed to whichever scenario was running when they fired. Any
  unexpected console error fails that scenario, except `reconnect`, which
  intentionally triggers connection-error logging and instead asserts the
  error *rate* stays bounded.
