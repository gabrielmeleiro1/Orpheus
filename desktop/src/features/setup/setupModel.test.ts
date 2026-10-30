import { describe, expect, it } from "vitest";

import type { RuntimeModelDownloadStatus, RuntimeSetupStatusResponse } from "../../types/api";
import {
  applyProgressEvent,
  applyRunnerStatus,
  acquireSetupStart,
  backendInstallProfileId,
  canStartSetup,
  createSelection,
  diagnosticsSnapshot,
  modelDownloadProgressEvent,
  profileChoices,
  runtimeCheckResult,
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
  it.each(["cpu", "cuda"] as const)("defaults to narration on %s without selecting unavailable CUDA cleaners", (device) => {
    const cpuStatus = status({ recommended_profile: "cuda", profiles: status().profiles.map((profile) => profile.id === "cuda" ? {
      ...profile, compatible: true, reason: null, risk: null, device,
      group_availability: { "cleaner-cuda": { available: false, unavailable_reason: "Needs an NVIDIA GPU with a CUDA 12.6 driver" } },
    } : profile) });
    const selection = createSelection(cpuStatus);
    expect(selection.profile).toBe("cuda");
    expect(selection.modelGroups).toEqual(["tts-cpu-cuda"]);
    expect(canStartSetup(selection, cpuStatus)).toBe(true);
    expect(toggleModelGroup(selection, "cleaner-cuda", cpuStatus)).toEqual(selection);
    expect(canStartSetup({ ...selection, modelGroups: ["tts-cpu-cuda", "cleaner-cuda"] }, cpuStatus)).toBe(false);
  });

  it("preserves compatible choices and disabled reasons", () => {
    expect(profileChoices(status()).map(({ id, disabled, reason }) => ({ id, disabled, reason }))).toEqual([
      { id: "base", disabled: false, reason: null },
      { id: "mlx", disabled: false, reason: null },
      { id: "cuda", disabled: true, reason: "Linux or Windows only." },
    ]);
  });

  it("defaults all local capabilities and clears groups when profiles change", () => {
    const initial = createSelection(status());
    const mlx = selectProfile(initial, "mlx", status());
    expect(mlx.modelGroups).toEqual(["tts-mlx", "cleaner-mlx"]);
    expect(toggleModelGroup(mlx, "cleaner-mlx", status()).modelGroups).toEqual(["tts-mlx"]);
    expect(selectProfile(mlx, "base", status()).modelGroups).toEqual([]);
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

  it("blocks a TTS profile with every model group unchecked", () => {
    const mlx = selectProfile(createSelection(status()), "mlx", status());
    expect(canStartSetup(mlx, status())).toBe(true);
    const nothingChecked = { ...mlx, modelGroups: [] };
    expect(canStartSetup(nothingChecked, status())).toBe(false);
  });

  it("preselects the recommended profile only when no profile was persisted", () => {
    const recommended = status({ recommended_profile: "mlx" });
    expect(createSelection(recommended).profile).toBe("mlx");
    expect(createSelection(recommended).modelGroups).toEqual(["tts-mlx", "cleaner-mlx"]);
  });

  it("keeps defaults stable while installed-runtime probes finish", () => {
    const initial = status({ recommended_profile: "mlx" });
    for (const ready of [false, true]) {
      expect(createSelection({ ...initial, installed_runtime: {
        profile: "mlx", ready, runtime_available: ready,
        model_groups: [{ id: "tts-mlx", runtime_ready: ready, assets_ready: true, ready }],
      } })).toEqual(createSelection(initial));
    }
  });

  it("ignores an incompatible recommended profile and keeps the base fallback", () => {
    expect(createSelection(status({ recommended_profile: "cuda" })).profile).toBe("base");
  });

  it("lets an explicit persisted profile win over the recommendation", () => {
    expect(createSelection(status({ selected_profile: "base", recommended_profile: "mlx" })).profile).toBe("base");
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
  it("clears consumed restart authorization from an authoritative runner snapshot", () => {
    const before = applyProgressEvent(undefined, { event: "complete", terminal: true, restart_required: true });
    const after = applyRunnerStatus(before, {
      lifecycle: "complete", profile: "cuda", modelGroups: [], stage: "verify",
      message: "Setup state saved.", percent: 100, restartRequired: false, error: null,
    });
    expect(after.restartRequired).toBe(false);
  });

  it("hydrates progress immediately from an authoritative runner snapshot", () => {
    const running = applyRunnerStatus(undefined, {
      lifecycle: "running",
      profile: "mlx",
      modelGroups: ["tts-mlx"],
      stage: "model_download",
      message: "Downloading models",
      percent: 42,
      restartRequired: false,
      error: null,
    });
    expect(running).toMatchObject({ lifecycle: "running", stage: "models", message: "Downloading models", percent: 42 });
    expect(running.completedStages).toEqual(["packages"]);

    const complete = applyRunnerStatus(running, {
      lifecycle: "complete",
      profile: "mlx",
      modelGroups: ["tts-mlx"],
      stage: "verify",
      message: "Setup state saved.",
      percent: 99,
      restartRequired: true,
      error: null,
    });
    expect(complete).toMatchObject({ lifecycle: "complete", percent: 100, restartRequired: true });
  });

  it("gates incomplete local setup but permits ready base and explicit unavailable recovery", () => {
    expect(shouldShowRuntimeSetup(status(), false)).toBe(true);
    expect(shouldShowRuntimeSetup(status({ configured: true, state: "ready", ready: true, selected_profile: "base", packages: { ready: true, missing: [], incompatible: [] }, probe: { ready: true, checks: [] } }), false)).toBe(false);
    expect(shouldShowRuntimeSetup(null, true)).toBe(false);
  });

  it("bounds progress, handles terminal states, and keeps safe diagnostics only", () => {
    const running = applyProgressEvent(undefined, { event: "stage_started", stage: "dependency_installation", message: "Installing packages", percent: 150 });
    expect(running.percent).toBe(99);
    expect(running.lifecycle).toBe("running");
    expect(running.stage).toBe("packages");
    const almostComplete = applyProgressEvent(undefined, { event: "stage_started", stage: "verify", percent: 90 });
    const complete = applyProgressEvent(almostComplete, { event: "complete", terminal: true, restart_required: true });
    expect(complete).toMatchObject({ lifecycle: "complete", restartRequired: true, percent: 100 });
    const failed = applyProgressEvent(running, { event: "error", terminal: true, message: "x".repeat(900) });
    expect(failed.lifecycle).toBe("failed");
    expect(failed.message.length).toBeLessThanOrEqual(512);
    expect(JSON.parse(diagnosticsSnapshot(status(), failed))).not.toHaveProperty("profiles");
  });

  it("activates and completes the verification stage from the runtime stage label", () => {
    const running = applyProgressEvent(undefined, {
      event: "stage_started",
      stage: "Dependency installation",
      percent: 50,
    });
    const verification = applyProgressEvent(running, {
      event: "stage_started",
      stage: "Verification",
      message: "Verifying setup",
      percent: 90,
    });
    expect(verification).toMatchObject({ lifecycle: "running", stage: "verify", percent: 90 });
    expect(verification.completedStages).toEqual(["packages", "models"]);

    const complete = applyProgressEvent(verification, {
      event: "stage_complete",
      stage: "Verification",
      message: "Setup state saved.",
      percent: 100,
    });
    expect(complete).toMatchObject({ lifecycle: "running", stage: "verify", percent: 99 });
    expect(complete.completedStages).toEqual(["packages", "models", "verify"]);
  });
});

describe("packaged backend install flow", () => {
  const bakedRuntime = {
    profile: "mlx" as const,
    ready: false,
    runtime_available: true,
    model_groups: [
      { id: "tts-mlx", runtime_ready: true, assets_ready: false, ready: false },
    ],
  };

  it("offers model provisioning for installed capabilities, including optional models after initial setup", () => {
    expect(backendInstallProfileId(false, bakedRuntime)).toBe("mlx");
    expect(backendInstallProfileId(true, bakedRuntime)).toBeNull();
    expect(backendInstallProfileId(false, null)).toBeNull();
    expect(backendInstallProfileId(false, { ...bakedRuntime, runtime_available: false, model_groups: [] })).toBeNull();
    expect(backendInstallProfileId(false, { ...bakedRuntime, runtime_available: false, model_groups: [{ id: "cleaner-mlx", runtime_ready: true, assets_ready: false, ready: false }] })).toBe("mlx");
    expect(backendInstallProfileId(false, { ...bakedRuntime, ready: true })).toBe("mlx");
  });

  it("maps download snapshots onto the staged progress model", () => {
    const running: RuntimeModelDownloadStatus = {
      lifecycle: "running",
      profile: "mlx",
      model_groups: ["tts-mlx", "cleaner-mlx"],
      total_models: 2,
      completed_models: 1,
      current_model_group: "cleaner-mlx",
      error: null,
    };
    const packages = applyProgressEvent(undefined, { event: "status", stage: "packages", percent: 30 });
    const state = applyProgressEvent(packages, modelDownloadProgressEvent(running));
    expect(state).toMatchObject({ lifecycle: "running", stage: "models", percent: null });
    expect(state.completedStages).toEqual(["packages"]);
    expect(state.message).toBe("Downloading model 2 of 2: Llama 3.2 · 3B");

    const stopping = modelDownloadProgressEvent({ ...running, lifecycle: "stopping" });
    expect(stopping.message).toContain("Stopping download");
    const cancelled = modelDownloadProgressEvent({ ...running, lifecycle: "cancelled" });
    expect(cancelled.message).toBe("Model download cancelled.");

    const complete = applyProgressEvent(state, modelDownloadProgressEvent({
      ...running,
      completed_models: 2,
      current_model_group: null,
      lifecycle: "complete",
    }));
    expect(complete.completedStages).toContain("models");
    expect(complete.lifecycle).toBe("running");

    const failed = applyProgressEvent(state, modelDownloadProgressEvent({
      ...running,
      lifecycle: "failed",
      error: "disk full",
    }));
    expect(failed).toMatchObject({ lifecycle: "failed", message: "disk full" });
  });
});

describe("runtime check result", () => {
  const group = { id: "tts-mlx", runtime_ready: true, assets_ready: true, assets_reason: null, ready: true };
  const configured = (overrides: Partial<RuntimeSetupStatusResponse> = {}) =>
    status({ configured: true, state: "ready", ready: true, selected_profile: "mlx", selected_model_groups: [group], ...overrides });

  it("reports ready, pending and restart states in plain words", () => {
    expect(runtimeCheckResult(configured())).toEqual({ tone: "ready", text: "Runtime ready." });
    expect(runtimeCheckResult(configured({ selected_profile: "base", selected_model_groups: [] })))
      .toEqual({ tone: "ready", text: "Library only is ready. Creating audiobooks needs a voice runtime." });
    expect(runtimeCheckResult(configured({ state: "checking", ready: false }))).toEqual({ tone: "pending", text: "Still checking this device." });
    expect(runtimeCheckResult(configured({ restart_required: true })).text).toBe("Restart Orpheus to finish applying the runtime.");
    expect(runtimeCheckResult(status())).toEqual({ tone: "problem", text: "No runtime is set up yet." });
  });

  it("names the first thing that is wrong", () => {
    const broken = (overrides: Partial<RuntimeSetupStatusResponse>) => runtimeCheckResult(configured({ state: "error", ready: false, ...overrides }));
    expect(broken({ selected_model_groups: [{ ...group, assets_ready: false, assets_reason: "missing", ready: false }] }))
      .toEqual({ tone: "problem", text: "Kokoro TTS is not downloaded." });
    expect(broken({
      selected_model_groups: [{ ...group, runtime_ready: false, ready: false }],
      installed_runtime: { profile: "mlx", ready: false, runtime_available: true, model_groups: [
        { id: "tts-mlx", runtime_ready: false, runtime_reason: "MLX could not load.", assets_ready: true, ready: false },
      ] },
    }).text).toBe("Kokoro TTS cannot run: MLX could not load.");
    expect(broken({ packages: { ready: false, missing: [{ name: "torch", expected: "2.6", installed: null }], incompatible: [] } }).text)
      .toBe("1 runtime package is missing or out of date.");
    expect(broken({}).text).toBe("The runtime is not ready.");
  });
});
