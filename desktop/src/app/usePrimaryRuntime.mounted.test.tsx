// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient, type ApiClient } from "../api/client";
import type { ModelOptionsResponse, RuntimeSetupStatusResponse, SystemCapabilitiesResponse } from "../types/api";

const { getRuntimeBootstrap, updateRuntimeBootstrapCapabilities } = vi.hoisted(() => ({
  getRuntimeBootstrap: vi.fn(),
  updateRuntimeBootstrapCapabilities: vi.fn(),
}));

vi.mock("./runtimeBootstrapCache", () => ({
  getRuntimeBootstrap,
  updateRuntimeBootstrapCapabilities,
}));

import { DESKTOP_SETTINGS_CHANGED_EVENT } from "../features/conversion/conversionOptionsShared";
import { useRuntimeIdentity } from "./usePrimaryRuntime";

type RuntimeIdentityState = ReturnType<typeof useRuntimeIdentity>;

function capabilities({
  probeState,
  refreshInProgress,
  mlxAvailable,
  ttsAvailable,
}: {
  probeState: SystemCapabilitiesResponse["runtime_probe"]["state"];
  refreshInProgress: boolean;
  mlxAvailable: boolean;
  ttsAvailable: boolean;
}): SystemCapabilitiesResponse {
  return {
    primary_runtime: "mlx",
    default_tts_engine: "kokoro-mlx",
    default_tts_quantization: "bf16",
    cleaner_runtime_availability: {
      api: true,
      mlx: mlxAvailable,
    },
    mlx_runtime_available: mlxAvailable,
    runtime_probe: {
      state: probeState,
      refresh_in_progress: refreshInProgress,
      tts: {
        available: ttsAvailable,
        latency_ms: null,
        error: null,
      },
      cleaner: {
        available: mlxAvailable,
        latency_ms: null,
        error: null,
      },
    },
  };
}

const modelOptions: ModelOptionsResponse = {
  tts_engines: [{ id: "kokoro-mlx", name: "MLX", voices: [], supported_quantizations: ["bf16"] }],
  cleaner_models: ["mlx-community/Llama-3.2-3B-Instruct-4bit"],
  api_cleaner_models: ["gpt-4o-mini"],
  cleaner_backend_options: [
    {
      name: "api",
      display_name: "Cloud API",
      is_local: false,
      requires_api_key: true,
      models: ["gpt-4o-mini"],
    },
    {
      name: "mlx",
      display_name: "Local MLX",
      is_local: true,
      requires_api_key: false,
      models: ["mlx-community/Llama-3.2-3B-Instruct-4bit"],
    },
  ],
};

function IdentityProbe({ apiClient, setupStatus = null }: { apiClient: ApiClient; setupStatus?: RuntimeSetupStatusResponse | null }): null {
  const identity = useRuntimeIdentity(apiClient, setupStatus);
  (globalThis as typeof globalThis & { runtimeIdentityProbe?: RuntimeIdentityState }).runtimeIdentityProbe = identity;
  return null;
}

function getIdentityProbe(): RuntimeIdentityState {
  return (globalThis as typeof globalThis & { runtimeIdentityProbe: RuntimeIdentityState }).runtimeIdentityProbe;
}

async function flushBootstrap(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("useRuntimeIdentity capability polling", () => {
  let root: Root;
  let container: HTMLDivElement;

  beforeEach(() => {
    vi.useFakeTimers();
    getRuntimeBootstrap.mockReset();
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => {
      root.unmount();
    });
    container.remove();
    delete (globalThis as typeof globalThis & { runtimeIdentityProbe?: RuntimeIdentityState }).runtimeIdentityProbe;
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("retries a stalled bootstrap on the next settings change", async () => {
    const cache = await vi.importActual<typeof import("./runtimeBootstrapCache")>("./runtimeBootstrapCache");
    getRuntimeBootstrap.mockImplementation(cache.getRuntimeBootstrap);
    const snapshot = capabilities({ probeState: "fresh", refreshInProgress: false, mlxAvailable: true, ttsAvailable: true });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      if (String(input).endsWith("/api/options/models")) {
        return new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
        });
      }
      return Promise.resolve(new Response(JSON.stringify(snapshot)));
    });
    const apiClient = createApiClient({ baseUrl: "http://desktop.test" });
    await act(async () => root.render(<IdentityProbe apiClient={apiClient} />));
    await act(async () => vi.advanceTimersByTimeAsync(8000));
    expect(getIdentityProbe().ttsLabel).toBe("—");
    fetchMock.mockImplementation(async (input) => new Response(JSON.stringify(
      String(input).endsWith("/api/options/models") ? modelOptions : snapshot,
    )));
    await act(async () => { window.dispatchEvent(new Event(DESKTOP_SETTINGS_CHANGED_EVENT)); });
    expect(getIdentityProbe().ttsLabel).toBe("MLX");
    expect(getRuntimeBootstrap).toHaveBeenCalledTimes(2);
  });

  it.each([
    [["kokoro"], null],
    [[], null],
    [undefined, null],
  ] as const)("does not infer model readiness from the CUDA engine catalog: %j", async (engines, expected) => {
    const snapshot = {
      ...capabilities({ probeState: "fresh", refreshInProgress: false, mlxAvailable: false, ttsAvailable: false }),
      primary_runtime: "cuda",
      default_tts_engine: "kokoro",
      available_tts_engines: engines ? [...engines] : undefined,
    };
    getRuntimeBootstrap.mockResolvedValue({ models: modelOptions, capabilities: snapshot });
    const apiClient = { getSystemCapabilities: vi.fn().mockResolvedValue(snapshot) } as unknown as ApiClient;
    await act(async () => root.render(<IdentityProbe apiClient={apiClient} />));
    await flushBootstrap();
    expect(getIdentityProbe()).toMatchObject({ ttsLabel: "CUDA", ttsReady: expected });
  });

  it("updates pending bootstrap identity from a fresh live snapshot and stops polling", async () => {
    const pending = capabilities({
      probeState: "pending",
      refreshInProgress: true,
      mlxAvailable: false,
      ttsAvailable: false,
    });
    const fresh = capabilities({
      probeState: "fresh",
      refreshInProgress: false,
      mlxAvailable: true,
      ttsAvailable: true,
    });
    getRuntimeBootstrap.mockResolvedValue({ models: modelOptions, capabilities: pending });
    const getSystemCapabilities = vi.fn().mockResolvedValue(fresh);
    const apiClient = { getSystemCapabilities } as unknown as ApiClient;

    await act(async () => {
      root.render(<IdentityProbe apiClient={apiClient} />);
    });
    await flushBootstrap();

    expect(getIdentityProbe()).toMatchObject({
      ttsLabel: "MLX",
      cleanerLabel: "Cloud API",
      ttsReady: null,
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_500);
    });

    expect(getSystemCapabilities).toHaveBeenCalledTimes(1);
    expect(getIdentityProbe()).toMatchObject({
      ttsLabel: "MLX",
      cleanerLabel: "Local MLX",
      ttsReady: null,
    });

    const callsAfterReady = getSystemCapabilities.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(getSystemCapabilities).toHaveBeenCalledTimes(callsAfterReady);
  });

  it("stops pending capability polling when the hook unmounts", async () => {
    const pending = capabilities({
      probeState: "pending",
      refreshInProgress: true,
      mlxAvailable: false,
      ttsAvailable: false,
    });
    getRuntimeBootstrap.mockResolvedValue({ models: modelOptions, capabilities: pending });
    const getSystemCapabilities = vi.fn().mockResolvedValue(pending);
    const apiClient = { getSystemCapabilities } as unknown as ApiClient;

    await act(async () => {
      root.render(<IdentityProbe apiClient={apiClient} />);
    });
    await flushBootstrap();

    await act(async () => {
      root.unmount();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });

    expect(getSystemCapabilities).not.toHaveBeenCalled();
  });

  it("updates readiness immediately when setup evidence changes without repeating the bootstrap", async () => {
    const snapshot = capabilities({ probeState: "fresh", refreshInProgress: false, mlxAvailable: true, ttsAvailable: true });
    getRuntimeBootstrap.mockResolvedValue({ models: modelOptions, capabilities: snapshot });
    const apiClient = { getSystemCapabilities: vi.fn().mockResolvedValue(snapshot) } as unknown as ApiClient;
    const status: RuntimeSetupStatusResponse = {
      schema_version: 1, configured: true, state: "ready", state_error: null,
      selected_profile: "mlx", selected_model_groups: [{ id: "tts-mlx", ready: true, runtime_ready: true, assets_ready: true, assets_reason: null }],
      packages: { ready: true, missing: [], incompatible: [] }, probe: { ready: true, checks: [] },
      ready: true, restart_required: false, profiles: [], risk: null,
      backend: { instance_id: "test", status: "running" },
    };
    await act(async () => root.render(<IdentityProbe apiClient={apiClient} setupStatus={status} />));
    await flushBootstrap();
    expect(getIdentityProbe().ttsReady).toBe(true);
    await act(async () => root.render(<IdentityProbe apiClient={apiClient} setupStatus={{ ...status, restart_required: true }} />));
    expect(getIdentityProbe().ttsReady).toBe(false);
    await act(async () => root.render(<IdentityProbe apiClient={apiClient} setupStatus={null} />));
    expect(getIdentityProbe().ttsReady).toBeNull();
    expect(getRuntimeBootstrap).toHaveBeenCalledTimes(1);
  });
});
