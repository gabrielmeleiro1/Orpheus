export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export class AssertionError extends Error {}

export function assert(condition, message) {
  if (!condition) {
    throw new AssertionError(message);
  }
}

/** Thrown by a scenario to voluntarily mark itself "skipped" with a reason. */
export class Skip extends Error {
  constructor(reason) {
    super(reason);
    this.isSkip = true;
  }
}

/**
 * Polls `predicate` until it returns a truthy value, or throws once
 * `timeoutMs` elapses. Returns the truthy value from the predicate.
 */
export async function waitFor(predicate, { timeoutMs = 15_000, intervalMs = 300, message = "condition not met" } = {}) {
  const deadline = Date.now() + timeoutMs;
  let lastError = null;
  while (Date.now() < deadline) {
    try {
      const result = await predicate();
      if (result) {
        return result;
      }
    } catch (err) {
      lastError = err;
    }
    await sleep(intervalMs);
  }
  const suffix = lastError ? ` (last error: ${lastError.message || lastError})` : "";
  throw new Error(`timed out after ${timeoutMs}ms waiting for: ${message}${suffix}`);
}

export function nowIso() {
  return new Date().toISOString();
}

export function formatDuration(ms) {
  if (ms < 1000) return `${ms}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  const remainderSec = Math.round(seconds - minutes * 60);
  return `${minutes}m ${remainderSec}s`;
}

export function requireSuccessfulConversion(status) {
  assert(status === "completed", `Conversion did not complete: ${status ?? "no terminal state"}`);
}

export function redactSecrets(text, secrets) {
  let safe = String(text);
  for (const secret of secrets.filter(Boolean)) {
    for (const value of [secret, encodeURIComponent(secret)]) safe = safe.split(value).join("[redacted]");
  }
  return safe;
}

export function hasRestoredPlaybackOffset({ currentTime, playingFor }) {
  return currentTime - playingFor >= 3.5;
}
