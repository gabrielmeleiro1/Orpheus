import { invoke } from "@tauri-apps/api/core";

import type { DesktopBackendEndpoint, DesktopBackendStatus } from "../types/api";

export const BACKEND_HEALTH_CHECK_INTERVAL_MS = 2_000;

const ENDPOINT_WAIT_TIMEOUT_MS = 120_000;
const ENDPOINT_WAIT_INTERVAL_MS = 150;

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const done = () => {
      globalThis.clearTimeout(timer);
      signal?.removeEventListener("abort", done);
      resolve();
    };
    const timer = globalThis.setTimeout(done, ms);
    signal?.addEventListener("abort", done, { once: true });
  });
}

// Native IPC cannot be aborted, so retire late replies while bounding each
// awaited call by the same startup deadline (or the caller's abort). Both
// resolutions and rejections remain observed after the deadline.
function beforeDeadline<T>(request: Promise<T>, timeoutAt: number, signal?: AbortSignal): Promise<T | null> {
  return new Promise((resolve, reject) => {
    const retire = () => resolve(null);
    const timer = globalThis.setTimeout(retire, Math.max(0, timeoutAt - Date.now()));
    signal?.addEventListener("abort", retire, { once: true });
    request.then(resolve, reject).finally(() => {
      globalThis.clearTimeout(timer);
      signal?.removeEventListener("abort", retire);
    });
  });
}

export async function readDesktopBackendStatus(): Promise<DesktopBackendStatus> {
  return invoke<DesktopBackendStatus>("backend_status");
}

export async function restartDesktopBackend(): Promise<void> {
  await invoke("restart_backend");
}

export function describeDesktopBackendStatus(status: DesktopBackendStatus): string {
  const error = status.error?.trim();
  if (status.lifecycle === "failed") {
    return error || "Desktop backend failed";
  }
  if (status.lifecycle === "stopped") {
    return error || "Desktop backend stopped";
  }
  if (status.lifecycle === "starting") {
    return "Desktop backend starting";
  }
  return status.endpoint?.base_url ? `Backend ready at ${status.endpoint.base_url}` : "Desktop backend ready";
}

/** Resolves null on timeout or once `signal` aborts, without issuing further IPC. */
export async function waitForDesktopEndpoint(signal?: AbortSignal): Promise<DesktopBackendEndpoint | null> {
  const timeoutAt = Date.now() + ENDPOINT_WAIT_TIMEOUT_MS;

  while (Date.now() < timeoutAt && !signal?.aborted) {
    const status = await beforeDeadline(readDesktopBackendStatus(), timeoutAt, signal);
    if (!status || signal?.aborted) return null;
    if (status.lifecycle === "failed" || status.lifecycle === "stopped") {
      throw new Error(describeDesktopBackendStatus(status));
    }
    if (status.endpoint?.base_url) {
      const endpoint = await beforeDeadline(invoke<DesktopBackendEndpoint | null>("backend_endpoint"), timeoutAt, signal);
      if (signal?.aborted) return null;
      if (endpoint?.base_url) {
        return endpoint;
      }
    }
    const remainingMs = timeoutAt - Date.now();
    if (remainingMs <= 0) return null;
    await sleep(Math.min(ENDPOINT_WAIT_INTERVAL_MS, remainingMs), signal);
  }

  return null;
}
