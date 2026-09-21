import { describe, expect, it } from "vitest";
import type { RuntimeSetupStatusResponse } from "../types/api";
import { runtimeTtsReady } from "./runtimeReadiness";

function readyStatus(): RuntimeSetupStatusResponse {
  return {
    schema_version: 1, configured: true, state: "ready", state_error: null,
    selected_profile: "cuda", selected_model_groups: [
      { id: "tts-cpu-cuda", ready: true, runtime_ready: true, assets_ready: true, assets_reason: null },
    ],
    packages: { ready: true, missing: [], incompatible: [] },
    probe: { ready: true, checks: [] }, ready: true, restart_required: false,
    profiles: [], risk: null, backend: { instance_id: "test", status: "running" },
  };
}

describe("dashboard speech readiness", () => {
  it("keeps missing evidence unknown", () => expect(runtimeTtsReady(null)).toBeNull());
  it.each(["tts-cpu-cuda", "tts-mlx"])("requires verified packages, runtime and weights for %s", (id) => {
    const status = readyStatus();
    status.selected_model_groups[0].id = id;
    expect(runtimeTtsReady(status)).toBe(true);
    status.selected_model_groups[0].assets_ready = false;
    expect(runtimeTtsReady(status)).toBe(false);
    status.selected_model_groups[0].assets_ready = true;
    status.packages.ready = false;
    expect(runtimeTtsReady(status)).toBe(false);
    status.packages.ready = true;
    status.probe.ready = false;
    expect(runtimeTtsReady(status)).toBe(false);
  });
  it("does not call a library-only or cleaner-only setup speech-ready", () => {
    const status = readyStatus();
    status.selected_profile = "base";
    status.selected_model_groups = [];
    expect(runtimeTtsReady(status)).toBe(false);
    status.selected_model_groups = [{ id: "cleaner-cuda", ready: true, runtime_ready: true, assets_ready: true, assets_reason: null }];
    expect(runtimeTtsReady(status)).toBe(false);
  });
  it("recognizes a verified bundled runtime and rejects missing weights or a required restart", () => {
    const status = readyStatus();
    status.selected_profile = "base";
    status.selected_model_groups = [];
    status.installed_runtime = {
      profile: "cuda", ready: true, runtime_available: true,
      model_groups: [{ id: "tts-cpu-cuda", ready: true, assets_ready: true, runtime_ready: true }],
    };
    expect(runtimeTtsReady(status)).toBe(true);
    status.installed_runtime.model_groups[0].assets_ready = false;
    expect(runtimeTtsReady(status)).toBe(false);
    status.installed_runtime.model_groups[0].assets_ready = true;
    status.restart_required = true;
    expect(runtimeTtsReady(status)).toBe(false);
  });
});
