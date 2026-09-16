// @vitest-environment happy-dom

/**
 * Higher-bound regression for the installed setup→dashboard→Settings path.
 *
 * Hook-only tests mock `getRuntimeBootstrap`, so they never exercise the real
 * WeakMap cache that `useRuntimeIdentityLabel` pins on dashboard entry after
 * Continue. This file uses the real cache + a shared ApiClient identity.
 *
 * Installed failure (7db3bfb): after Continue, dashboard pins a stale
 * mlx=false bootstrap; the live backend becomes mlx=true during a long
 * dashboard dwell; opening Settings still paints the pinned snapshot until the
 * first delayed poll (1.5s). Computer Use inspects immediately and sees only
 * Cloud API even while curl already reports mlx=true.
 */

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import { getRuntimeBootstrap } from "../../app/runtimeBootstrapCache";
import { useRuntimeIdentityLabel } from "../../app/usePrimaryRuntime";
import type { ModelOptionsResponse, SystemCapabilitiesResponse } from "../../types/api";
import { useConversionOptions, type UseConversionOptionsResult } from "./useConversionOptions";

function capabilities(overrides: {
  probeState?: SystemCapabilitiesResponse["runtime_probe"]["state"];
  refreshInProgress?: boolean;
  mlxAvailable?: boolean;
}): SystemCapabilitiesResponse {
  const {
    probeState = "stale",
    refreshInProgress = true,
    mlxAvailable = false,
  } = overrides;
  return {
    primary_runtime: "mlx",
    default_tts_engine: "kokoro-mlx",
    default_tts_quantization: "bf16",
    cleaner_runtime_availability: { api: true, cuda: false, mlx: mlxAvailable },
    mlx_runtime_available: mlxAvailable,
    runtime_probe: {
      state: probeState,
      refresh_in_progress: refreshInProgress,
      cleaner: { available: mlxAvailable, latency_ms: null, error: null },
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
      name: "cuda",
      display_name: "Local CUDA",
      is_local: true,
      requires_api_key: false,
      models: ["Qwen/Qwen2.5-1.5B-Instruct"],
      supported_quantizations: ["4bit"],
      default_device: "cuda",
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

function IdentityProbe({ apiClient }: { apiClient: ApiClient }): null {
  useRuntimeIdentityLabel(apiClient);
  return null;
}

function SettingsProbe({ apiClient }: { apiClient: ApiClient }): null {
  const result = useConversionOptions({ apiClient, conversionMode: "audiobook" });
  (globalThis as typeof globalThis & { settingsProbe?: UseConversionOptionsResult }).settingsProbe = result;
  return null;
}

function getSettingsProbe(): UseConversionOptionsResult {
  return (globalThis as typeof globalThis & { settingsProbe: UseConversionOptionsResult }).settingsProbe;
}

describe("setup → dashboard → Settings capability propagation", () => {
  let root: Root;
  let container: HTMLDivElement;

  beforeEach(() => {
    vi.useFakeTimers();
    window.localStorage.clear();
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT =
      true;
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => {
      root.unmount();
    });
    container.remove();
    delete (globalThis as typeof globalThis & { settingsProbe?: UseConversionOptionsResult }).settingsProbe;
    window.localStorage.clear();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("eagerly refreshes a stale bootstrap when Settings opens before the dashboard poll", async () => {
    const staleUnavailable = capabilities({
      probeState: "stale",
      refreshInProgress: true,
      mlxAvailable: false,
    });
    const liveAvailable = capabilities({
      probeState: "stale",
      refreshInProgress: true,
      mlxAvailable: true,
    });

    let capabilityCalls = 0;
    const getSystemCapabilities = vi.fn(async () => {
      capabilityCalls += 1;
      // First call is the dashboard identity bootstrap (pinned in WeakMap).
      // Later calls refresh against the live backend.
      return capabilityCalls === 1 ? staleUnavailable : liveAvailable;
    });
    const getModelOptions = vi.fn(async () => modelOptions);
    const getVoiceOptions = vi.fn(async () => ({ engine: "kokoro-mlx", voices: [] }));
    const apiClient = {
      getSystemCapabilities,
      getModelOptions,
      getVoiceOptions,
    } as unknown as ApiClient;

    // Continue → dashboard: identity label pins the cold/stale bootstrap.
    await act(async () => {
      root.render(<IdentityProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    const pinned = await getRuntimeBootstrap(apiClient);
    expect(pinned.capabilities.cleaner_runtime_availability.mlx).toBe(false);
    expect(getSystemCapabilities).toHaveBeenCalledTimes(1);

    // Open Settings before the dashboard's first 1.5s capability refresh.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    expect(getSystemCapabilities).toHaveBeenCalledTimes(1);

    // Open Settings: must not wait for the 1.5s poll backoff before exposing MLX.
    await act(async () => {
      root.render(
        <>
          <IdentityProbe apiClient={apiClient} />
          <SettingsProbe apiClient={apiClient} />
        </>,
      );
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(getSystemCapabilities.mock.calls.length).toBeGreaterThan(1);
    expect(getSettingsProbe().cleanerBackendOptions.map((option) => option.label)).toEqual([
      "Cloud API",
      "Local MLX",
    ]);
    expect(getSettingsProbe().cleanerBackend).toBe("mlx");
    expect(getSettingsProbe().cleanerBackendConfigured).toBe(false);
  });

  it("preserves an explicitly configured Cloud API choice after the eager refresh", async () => {
    window.localStorage.setItem(
      "orpheus.desktop.settings.v1",
      JSON.stringify({ cleaner_backend: "api", cleaner_backend_configured: true }),
    );

    const staleUnavailable = capabilities({
      probeState: "stale",
      refreshInProgress: true,
      mlxAvailable: false,
    });
    const liveAvailable = capabilities({
      probeState: "stale",
      refreshInProgress: true,
      mlxAvailable: true,
    });

    let capabilityCalls = 0;
    const getSystemCapabilities = vi.fn(async () => {
      capabilityCalls += 1;
      return capabilityCalls === 1 ? staleUnavailable : liveAvailable;
    });
    const getModelOptions = vi.fn(async () => modelOptions);
    const getVoiceOptions = vi.fn(async () => ({ engine: "kokoro-mlx", voices: [] }));
    const apiClient = {
      getSystemCapabilities,
      getModelOptions,
      getVoiceOptions,
    } as unknown as ApiClient;

    await act(async () => {
      root.render(<IdentityProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    await act(async () => {
      root.render(
        <>
          <IdentityProbe apiClient={apiClient} />
          <SettingsProbe apiClient={apiClient} />
        </>,
      );
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(getSettingsProbe().cleanerBackend).toBe("api");
    expect(getSettingsProbe().cleanerBackendConfigured).toBe(true);
    expect(getSettingsProbe().cleanerBackendOptions.some((option) => option.value === "mlx")).toBe(
      true,
    );
  });
});
