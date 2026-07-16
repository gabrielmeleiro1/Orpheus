import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

import { isTauriRuntime } from "../../platform/tauriRuntime";
import type { SetupProgressEvent, SetupRunnerStatusSnapshot } from "./setupModel";

export interface RuntimeSetupEnvironment {
  available: boolean;
  mode: "source_checkout" | "packaged" | "browser";
  reason: string;
}

export interface RuntimeSetupRequest {
  profile: "base" | "mlx" | "cuda";
  modelGroups: string[];
  acceptPytorchRisk: boolean;
}

export interface RuntimeSetupRunnerStatus extends SetupRunnerStatusSnapshot {}

export async function startRuntimeSetup(request: RuntimeSetupRequest): Promise<RuntimeSetupRunnerStatus> {
  return invoke<RuntimeSetupRunnerStatus>("start_runtime_setup", { request });
}

export async function getRuntimeSetupRunnerStatus(): Promise<RuntimeSetupRunnerStatus> {
  return invoke<RuntimeSetupRunnerStatus>("runtime_setup_status");
}

export async function getRuntimeSetupEnvironment(): Promise<RuntimeSetupEnvironment> {
  if (!isTauriRuntime()) {
    return {
      available: false,
      mode: "browser",
      reason: "Runtime installation is available only in the desktop app from a source checkout.",
    };
  }
  return invoke<RuntimeSetupEnvironment>("runtime_setup_environment");
}

export async function cancelRuntimeSetup(): Promise<RuntimeSetupRunnerStatus> {
  return invoke<RuntimeSetupRunnerStatus>("cancel_runtime_setup");
}

export async function restartOrpheusBackend(): Promise<void> {
  await invoke("restart_backend_after_setup");
}

export async function listenRuntimeSetupProgress(
  onProgress: (event: SetupProgressEvent) => void,
): Promise<UnlistenFn> {
  return listen<SetupProgressEvent>("runtime-setup-progress", (event) => onProgress(event.payload));
}
