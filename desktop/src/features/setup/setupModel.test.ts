import { describe, expect, it } from "vitest";

import type { RuntimeSetupStatusResponse } from "../../types/api";
import {
  applyProgressEvent,
  acquireSetupStart,
  canStartSetup,
  createSelection,
  diagnosticsSnapshot,
  profileChoices,
  selectProfile,
  shouldShowRuntimeSetup,
  toggleModelGroup,
} from "./setupModel";

function status(overrides: Partial<RuntimeSetupStatusResponse> = {}): RuntimeSetupStatusResponse {
  return {
    schema_version: 1,
    configured: false,
    state: "unconfigured",
    state_error: null,
    selected_profile: null,
    selected_model_groups: [],
    packages: { ready: false, missing: [], incompatible: [] },
    probe: { ready: false, checks: [] },
    ready: false,
    restart_required: false,
    profiles: [
      { id: "base", label: "Library only", description: "Documents and playback.", compatible: true, reason: null, model_groups: [], risk: null },
      { id: "mlx", label: "Apple Silicon (MLX)", description: "Local MLX.", compatible: true, reason: null, model_groups: ["tts-mlx", "cleaner-mlx"], risk: null },
      { id: "cuda", label: "NVIDIA CUDA", description: "Local CUDA.", compatible: false, reason: "Linux or Windows only.", model_groups: ["tts-cpu-cuda", "cleaner-cuda"], risk: { id: "pytorch", summary: "Use trusted models only.", accepted: false } },
    ],
    risk: null,
    backend: { instance_id: "backend-1", status: "running" },
    ...overrides,
  };
}

describe("runtime setup selection", () => {
  it("preserves compatible choices and disabled reasons", () => {
    expect(profileChoices(status()).map(({ id, disabled, reason }) => ({ id, disabled, reason }))).toEqual([
      { id: "base", disabled: false, reason: null },
      { id: "mlx", disabled: false, reason: null },
      { id: "cuda", disabled: true, reason: "Linux or Windows only." },
    ]);
  });

  it("defaults local TTS independently and clears groups when profiles change", () => {
    const initial = createSelection(status());
    const mlx = selectProfile(initial, "mlx", status());
    expect(mlx.modelGroups).toEqual(["tts-mlx"]);
    expect(toggleModelGroup(mlx, "cleaner-mlx", status()).modelGroups).toEqual(["tts-mlx", "cleaner-mlx"]);
    expect(selectProfile(toggleModelGroup(mlx, "cleaner-mlx", status()), "base", status()).modelGroups).toEqual([]);
    expect(selectProfile(mlx, "cuda", status())).toEqual(mlx);
  });

  it("allows base-only setup and requires explicit CUDA risk acceptance", () => {
    const cudaStatus = status({
      profiles: status().profiles.map((profile) => profile.id === "cuda" ? { ...profile, compatible: true, reason: null } : profile),
    });
    expect(canStartSetup(createSelection(status()), status())).toBe(true);
    const cuda = selectProfile(createSelection(cudaStatus), "cuda", cudaStatus);
    expect(canStartSetup(cuda, cudaStatus)).toBe(false);
    expect(canStartSetup({ ...cuda, acceptPytorchRisk: true }, cudaStatus)).toBe(true);
  });

  it("prevents duplicate starts until the running operation releases its lock", () => {
    const lock = { current: false };
    expect(acquireSetupStart(lock)).toBe(true);
    expect(acquireSetupStart(lock)).toBe(false);
    lock.current = false;
    expect(acquireSetupStart(lock)).toBe(true);
  });
});

describe("runtime setup gate and progress", () => {
  it("gates incomplete local setup but permits ready base and explicit unavailable recovery", () => {
    expect(shouldShowRuntimeSetup(status(), false)).toBe(true);
    expect(shouldShowRuntimeSetup(status({ configured: true, state: "ready", ready: true, selected_profile: "base", packages: { ready: true, missing: [], incompatible: [] }, probe: { ready: true, checks: [] } }), false)).toBe(false);
    expect(shouldShowRuntimeSetup(null, true)).toBe(false);
  });

  it("bounds progress, handles terminal states, and keeps safe diagnostics only", () => {
    const running = applyProgressEvent(undefined, { event: "stage_started", stage: "dependency_installation", message: "Installing packages", percent: 150 });
    expect(running.percent).toBe(100);
    expect(running.lifecycle).toBe("running");
    expect(running.stage).toBe("packages");
    const complete = applyProgressEvent(running, { event: "complete", terminal: true, restart_required: true });
    expect(complete).toMatchObject({ lifecycle: "complete", restartRequired: true });
    const failed = applyProgressEvent(running, { event: "error", terminal: true, message: "x".repeat(900) });
    expect(failed.lifecycle).toBe("failed");
    expect(failed.message.length).toBeLessThanOrEqual(512);
    expect(JSON.parse(diagnosticsSnapshot(status(), failed))).not.toHaveProperty("profiles");
  });
});
