import { describe, expect, it } from "vitest";
import type { RuntimeSetupStatusResponse } from "../types/api";
import { conversionRuntimeState, runtimeTtsReady } from "./runtimeReadiness";

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

describe("what Create may do with the runtime", () => {
  const settled = { checking: false, checkFailed: false };
  it("allows conversion only with a verified speech runtime", () => {
    expect(conversionRuntimeState(readyStatus(), settled)).toBe("ready");
  });
  it("reports a library-only setup as unavailable rather than ready", () => {
    const status = readyStatus();
    status.selected_profile = "base";
    status.selected_model_groups = [];
    expect(conversionRuntimeState(status, settled)).toBe("unavailable");
  });
  it("waits while the status is unanswered or the runtime is still being verified", () => {
    expect(conversionRuntimeState(null, settled)).toBe("checking");
    expect(conversionRuntimeState(readyStatus(), { checking: true, checkFailed: false })).toBe("checking");
  });
  it("stops waiting once the status read has failed", () => {
    expect(conversionRuntimeState(null, { checking: false, checkFailed: true })).toBe("unavailable");
  });
});
