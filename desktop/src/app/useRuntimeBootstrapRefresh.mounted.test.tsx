// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../api/client";
import type { ModelOptionsResponse, SystemCapabilitiesResponse } from "../types/api";

const { getRuntimeBootstrap, updateRuntimeBootstrapCapabilities } = vi.hoisted(() => ({
  getRuntimeBootstrap: vi.fn(),
  updateRuntimeBootstrapCapabilities: vi.fn(),
}));

vi.mock("./runtimeBootstrapCache", () => ({
  getRuntimeBootstrap,
  updateRuntimeBootstrapCapabilities,
}));

import { useRuntimeBootstrapRefresh } from "./useRuntimeBootstrapRefresh";

function capabilities(settled: boolean): SystemCapabilitiesResponse {
  return {
    primary_runtime: "mlx",
    default_tts_engine: "kokoro-mlx",
    default_tts_quantization: "bf16",
    cleaner_runtime_availability: { api: true, mlx: settled },
    mlx_runtime_available: settled,
    runtime_probe: {
      state: settled ? "fresh" : "pending",
      refresh_in_progress: !settled,
      tts: { available: settled, latency_ms: null, error: null },
      cleaner: { available: settled, latency_ms: null, error: null },
    },
  };
}

const modelOptions: ModelOptionsResponse = {
  tts_engines: [{ id: "kokoro-mlx", name: "MLX", voices: [], supported_quantizations: ["bf16"] }],
  cleaner_models: [],
  api_cleaner_models: [],
  cleaner_backend_options: [],
};

function Probe({ apiClient }: { apiClient: ApiClient }): null {
  useRuntimeBootstrapRefresh(apiClient);
  return null;
}

describe("useRuntimeBootstrapRefresh", () => {
  let root: Root;
  let container: HTMLDivElement;

  beforeEach(() => {
    vi.useFakeTimers();
    getRuntimeBootstrap.mockReset();
    updateRuntimeBootstrapCapabilities.mockReset();
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.useRealTimers();
  });

  it("refreshes the cached capabilities while the probe warms up, then stops", async () => {
    const fresh = capabilities(true);
    getRuntimeBootstrap.mockResolvedValue({ models: modelOptions, capabilities: capabilities(false) });
    const getSystemCapabilities = vi.fn().mockResolvedValue(fresh);
    const apiClient = { getSystemCapabilities } as unknown as ApiClient;

    await act(async () => root.render(<Probe apiClient={apiClient} />));
    await act(async () => vi.advanceTimersByTimeAsync(1_500));

    expect(getSystemCapabilities).toHaveBeenCalledTimes(1);
    expect(updateRuntimeBootstrapCapabilities).toHaveBeenCalledWith(apiClient, fresh);
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(getSystemCapabilities).toHaveBeenCalledTimes(1);
  });

  it("does not poll when the bootstrap snapshot is already settled", async () => {
    getRuntimeBootstrap.mockResolvedValue({ models: modelOptions, capabilities: capabilities(true) });
    const getSystemCapabilities = vi.fn();
    const apiClient = { getSystemCapabilities } as unknown as ApiClient;

    await act(async () => root.render(<Probe apiClient={apiClient} />));
    await act(async () => vi.advanceTimersByTimeAsync(60_000));

    expect(getRuntimeBootstrap).toHaveBeenCalledWith(apiClient);
    expect(getSystemCapabilities).not.toHaveBeenCalled();
  });

  it("stops pending capability polling on unmount", async () => {
    getRuntimeBootstrap.mockResolvedValue({ models: modelOptions, capabilities: capabilities(false) });
    const getSystemCapabilities = vi.fn().mockResolvedValue(capabilities(false));
    const apiClient = { getSystemCapabilities } as unknown as ApiClient;

    await act(async () => root.render(<Probe apiClient={apiClient} />));
    await act(async () => root.unmount());
    await act(async () => vi.advanceTimersByTimeAsync(60_000));

    expect(getSystemCapabilities).not.toHaveBeenCalled();
    root = createRoot(container);
  });
});
