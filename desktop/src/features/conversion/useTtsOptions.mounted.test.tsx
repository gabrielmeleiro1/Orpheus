// @vitest-environment happy-dom

import { act, useRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import type { ModelOptionsResponse, SystemCapabilitiesResponse } from "../../types/api";
import { useTtsOptions, type UseTtsOptionsResult } from "./useTtsOptions";
import type { PersistedSettings } from "./conversionOptionsShared";

const { getRuntimeBootstrap } = vi.hoisted(() => ({
  getRuntimeBootstrap: vi.fn(),
}));

vi.mock("../../app/runtimeBootstrapCache", () => ({
  getRuntimeBootstrap,
}));

function capabilities(
  overrides: Partial<SystemCapabilitiesResponse> & {
    probeState?: SystemCapabilitiesResponse["runtime_probe"]["state"];
    refreshInProgress?: boolean;
    mlxAvailable?: boolean;
  } = {},
): SystemCapabilitiesResponse {
  const {
    probeState = "pending",
    refreshInProgress = true,
    mlxAvailable = false,
    ...rest
  } = overrides;
  return {
    default_tts_engine: "kokoro-mlx",
    default_tts_quantization: "bf16",
    cleaner_runtime_availability: { mlx: mlxAvailable, openai: true },
    mlx_runtime_available: mlxAvailable,
    runtime_probe: {
      state: probeState,
      refresh_in_progress: refreshInProgress,
    },
    ...rest,
  };
}

const modelOptions: ModelOptionsResponse = {
  tts_engines: [{ id: "kokoro-mlx", name: "MLX", voices: [], supported_quantizations: ["bf16"] }],
  cleaner_models: [],
  api_cleaner_models: [],
  cleaner_backend_options: [],
};

function TtsProbe({ apiClient }: { apiClient: ApiClient | null }): null {
  const persistedSettingsRef = useRef({} as PersistedSettings);
  const settingsHydratedRef = useRef(false);
  const result = useTtsOptions({ apiClient, persistedSettingsRef, settingsHydratedRef });
  (globalThis as typeof globalThis & { ttsProbe?: UseTtsOptionsResult }).ttsProbe = result;
  return null;
}

function getTtsProbe(): UseTtsOptionsResult {
  return (globalThis as typeof globalThis & { ttsProbe: UseTtsOptionsResult }).ttsProbe;
}

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });
  return { promise, resolve, reject };
}

describe("useTtsOptions capability probe polling", () => {
  let root: Root;
  let container: HTMLDivElement;

  beforeEach(() => {
    vi.useFakeTimers();
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
    getRuntimeBootstrap.mockReset();
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => {
      root.unmount();
    });
    container.remove();
    delete (globalThis as typeof globalThis & { ttsProbe?: UseTtsOptionsResult }).ttsProbe;
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("does not let a late bootstrap snapshot overwrite a fresher poll result", async () => {
    const pending = capabilities({ probeState: "pending", refreshInProgress: true, mlxAvailable: false });
    const fresh = capabilities({ probeState: "fresh", refreshInProgress: false, mlxAvailable: true });
    const bootstrap = deferred<{ models: ModelOptionsResponse; capabilities: SystemCapabilitiesResponse }>();
    getRuntimeBootstrap.mockReturnValue(bootstrap.promise);

    const getSystemCapabilities = vi.fn().mockResolvedValue(fresh);
    const getVoiceOptions = vi.fn(async () => ({ engine: "kokoro-mlx", voices: [] }));
    const apiClient = { getSystemCapabilities, getVoiceOptions } as unknown as ApiClient;

    await act(async () => {
      root.render(<TtsProbe apiClient={apiClient} />);
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_500);
    });
    expect(getTtsProbe().systemCapabilities?.cleaner_runtime_availability.mlx).toBe(true);

    await act(async () => {
      bootstrap.resolve({ models: modelOptions, capabilities: pending });
      await bootstrap.promise;
    });

    expect(getTtsProbe().systemCapabilities?.runtime_probe.state).toBe("fresh");
    expect(getTtsProbe().systemCapabilities?.cleaner_runtime_availability.mlx).toBe(true);
    expect(getTtsProbe().modelOptions).toEqual(modelOptions);
  });

  it("clears the poll timer on unmount", async () => {
    const pending = capabilities({ probeState: "pending", refreshInProgress: true, mlxAvailable: false });
    getRuntimeBootstrap.mockResolvedValue({ models: modelOptions, capabilities: pending });
    const getSystemCapabilities = vi.fn().mockResolvedValue(pending);
    const getVoiceOptions = vi.fn(async () => ({ engine: "kokoro-mlx", voices: [] }));
    const apiClient = { getSystemCapabilities, getVoiceOptions } as unknown as ApiClient;
    const clearTimeoutSpy = vi.spyOn(window, "clearTimeout");

    await act(async () => {
      root.render(<TtsProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    clearTimeoutSpy.mockClear();
    await act(async () => {
      root.unmount();
    });

    expect(clearTimeoutSpy).toHaveBeenCalled();
    const callsAtUnmount = getSystemCapabilities.mock.calls.length;
    await vi.advanceTimersByTimeAsync(30_000);
    expect(getSystemCapabilities).toHaveBeenCalledTimes(callsAtUnmount);
  });
});
