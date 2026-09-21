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
    const status = await readDesktopBackendStatus();
    if (status.lifecycle === "failed" || status.lifecycle === "stopped") {
      throw new Error(describeDesktopBackendStatus(status));
    }
    if (status.endpoint?.base_url) {
      const endpoint = await invoke<DesktopBackendEndpoint | null>("backend_endpoint");
      if (endpoint?.base_url) {
        return endpoint;
      }
    }
    await sleep(ENDPOINT_WAIT_INTERVAL_MS);
  }

  return null;
}
