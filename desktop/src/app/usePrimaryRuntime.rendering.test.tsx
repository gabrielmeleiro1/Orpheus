// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import type { ApiClient } from "../api/client";
import type { ModelOptionsResponse, SystemCapabilitiesResponse } from "../types/api";

const bootstrap = vi.hoisted(() => ({ getRuntimeBootstrap: vi.fn() }));
vi.mock("./runtimeBootstrapCache", () => ({
  getRuntimeBootstrap: bootstrap.getRuntimeBootstrap,
  updateRuntimeBootstrapCapabilities: vi.fn(),
}));

import { useRuntimeIdentity } from "./usePrimaryRuntime";

const pending = (): SystemCapabilitiesResponse => ({
  primary_runtime: "mlx", default_tts_engine: "kokoro-mlx", default_tts_quantization: "bf16",
  cleaner_runtime_availability: { api: true, mlx: false }, mlx_runtime_available: false,
  runtime_probe: { state: "pending", refresh_in_progress: true,
    tts: { available: false, latency_ms: null, error: null }, cleaner: { available: false, latency_ms: null, error: null } },
} as SystemCapabilitiesResponse);
const modelOptions: ModelOptionsResponse = {
  tts_engines: [{ id: "kokoro-mlx", name: "MLX", voices: [], supported_quantizations: ["bf16"] }],
  cleaner_models: ["mlx-community/Llama-3.2-3B-Instruct-4bit"],
  api_cleaner_models: ["gpt-4o-mini"],
  cleaner_backend_options: [
    { name: "api", display_name: "Cloud API", is_local: false, requires_api_key: true, models: ["gpt-4o-mini"] },
    { name: "mlx", display_name: "Local MLX", is_local: true, requires_api_key: false, models: ["mlx-community/Llama-3.2-3B-Instruct-4bit"] },
  ],
};

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it("does not re-render its host when a warm-up poll repeats the same runtime identity", async () => {
  vi.useFakeTimers();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  bootstrap.getRuntimeBootstrap.mockResolvedValue({ capabilities: pending(), models: modelOptions });
  const getSystemCapabilities = vi.fn(async () => pending());
  const apiClient = { getSystemCapabilities } as unknown as ApiClient;
  let renders = 0;
  function Host() {
    renders += 1;
    useRuntimeIdentity(apiClient);
    return null;
  }
  const root = createRoot(document.createElement("div"));
  try {
    await act(async () => root.render(<Host />));
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    const settled = renders;
    const pollsBefore = getSystemCapabilities.mock.calls.length;

    // One act per poll interval: each poll lands in its own task, as in the app.
    for (let interval = 0; interval < 6; interval += 1) {
      await act(async () => vi.advanceTimersByTimeAsync(6_000));
    }
    expect(getSystemCapabilities.mock.calls.length - pollsBefore).toBeGreaterThanOrEqual(5);
    // At most one bailout render for the first identical update.
    expect(renders - settled).toBeLessThanOrEqual(1);
  } finally {
    await act(async () => root.unmount());
  }
});
