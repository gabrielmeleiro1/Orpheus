import { invoke } from "@tauri-apps/api/core";

import type { DesktopBackendEndpoint, DesktopBackendStatus } from "../../types/api";

export const PLAYBACK_SAVE_INTERVAL_SEC = 15;
export const BACKEND_HEALTH_CHECK_INTERVAL_MS = 2_000;

const ENDPOINT_WAIT_TIMEOUT_MS = 120_000;
const ENDPOINT_WAIT_INTERVAL_MS = 150;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    globalThis.setTimeout(resolve, ms);
  });
}

// Native IPC cannot be aborted, so retire late replies while bounding each
// awaited call by the same startup deadline. Both resolutions and rejections
// remain observed after the deadline.
function beforeDeadline<T>(request: Promise<T>, timeoutAt: number): Promise<T | null> {
  return new Promise((resolve, reject) => {
    const timer = globalThis.setTimeout(() => resolve(null), Math.max(0, timeoutAt - Date.now()));
    request.then(resolve, reject).finally(() => globalThis.clearTimeout(timer));
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

export async function waitForDesktopEndpoint(): Promise<DesktopBackendEndpoint | null> {
  const timeoutAt = Date.now() + ENDPOINT_WAIT_TIMEOUT_MS;

  while (Date.now() < timeoutAt) {
    const status = await beforeDeadline(readDesktopBackendStatus(), timeoutAt);
    if (!status) return null;
    if (status.lifecycle === "failed" || status.lifecycle === "stopped") {
      throw new Error(describeDesktopBackendStatus(status));
    }
    if (status.endpoint?.base_url) {
      const endpoint = await beforeDeadline(invoke<DesktopBackendEndpoint | null>("backend_endpoint"), timeoutAt);
      if (endpoint?.base_url) {
        return endpoint;
      }
    }
    const remainingMs = timeoutAt - Date.now();
    if (remainingMs <= 0) return null;
    await sleep(Math.min(ENDPOINT_WAIT_INTERVAL_MS, remainingMs));
  }

  return null;
}
