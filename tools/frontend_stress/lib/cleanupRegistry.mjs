/**
 * Shared registry of teardown callbacks for everything the harness spawns
 * (backend process, vite dev server process, Playwright browser).
 *
 * Each spawner calls `registerCleanup()` with its own idempotent stop
 * function as soon as the process/browser exists — before waiting on any
 * readiness check — so the thing gets torn down even if something else
 * fails first (e.g. the frontend's readiness timeout throws while the
 * backend is already up) or the harness is interrupted (Ctrl-C) mid-run.
 */

const cleanups = [];

/**
 * Registers a cleanup callback. Callbacks should be safe to call more than
 * once; `cleanupAll()` may run alongside a module's own direct call to the
 * same stop function on an error path.
 */
export function registerCleanup(fn) {
  cleanups.push(fn);
}

// Some environments hang indefinitely inside browser.close() (observed with
// a Playwright/Chromium version-mismatched fallback executable — the OS
// process is confirmed killed, but the CDP connection never reports
// "disconnected" back to Playwright's JS side). Bounding each callback and
// running them concurrently means one stuck callback can't stop the others
// — e.g. the backend/frontend — from getting torn down.
const CLEANUP_CALLBACK_TIMEOUT_MS = 10_000;

let cleanupPromise = null;

/**
 * Runs every registered cleanup callback concurrently and empties the
 * registry. Idempotent: overlapping calls — e.g. one from the top-level
 * try/finally and one from a signal handler — all resolve to the same run
 * instead of stopping processes twice.
 */
export function cleanupAll() {
  if (!cleanupPromise) {
    cleanupPromise = runCleanups();
  }
  return cleanupPromise;
}

async function runCleanups() {
  const fns = cleanups.splice(0, cleanups.length);
  await Promise.allSettled(
    fns.map((fn) =>
      withTimeout(fn(), CLEANUP_CALLBACK_TIMEOUT_MS).catch((err) => {
        console.error("cleanup callback failed:", err && err.stack ? err.stack : err);
      }),
    ),
  );
}

function withTimeout(promise, timeoutMs) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error(`cleanup callback did not settle within ${timeoutMs}ms`)),
      timeoutMs,
    );
    Promise.resolve(promise).then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (err) => {
        clearTimeout(timer);
        reject(err);
      },
    );
  });
}
