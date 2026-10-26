import type { RuntimeSetupStatusResponse } from "../types/api";
import { isCheckingConfiguredRuntime } from "../features/setup/setupModel";

const readyHintKey = "orpheus.desktop.runtime-ready.v1";

/** A presentation hint only; never restore runtime readiness from storage. */
export function readReadyRuntimeHint(): boolean {
  try {
    return window.localStorage.getItem(readyHintKey) === "ready";
  } catch {
    return false;
  }
}

export function rememberReadyRuntime(status: RuntimeSetupStatusResponse): void {
  if (isCheckingConfiguredRuntime(status)) return;
  try {
    if (status.configured && status.ready && status.state === "ready" && !status.restart_required) {
      window.localStorage.setItem(readyHintKey, "ready");
    } else {
      window.localStorage.removeItem(readyHintKey);
    }
  } catch {
    // Storage is optional, just like persisted conversion preferences.
  }
}

export function showDashboardDuringRuntimeCheck(
  readyHint: boolean,
  status: RuntimeSetupStatusResponse | null,
  error: string | null,
): boolean {
  return readyHint && error === null && (status === null || isCheckingConfiguredRuntime(status));
}
