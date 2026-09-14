// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import type { ModelOptionsResponse, SystemCapabilitiesResponse } from "../../types/api";
import {
  readPersistedSettings,
  savePersistedSettings,
  type PersistedSettings,
} from "./conversionOptionsShared";
import { useConversionOptions, type UseConversionOptionsResult } from "./useConversionOptions";

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
    cudaAvailable?: boolean;
  } = {},
): SystemCapabilitiesResponse {
  const {
    probeState = "pending",
    refreshInProgress = true,
    mlxAvailable = false,
    cudaAvailable = false,
    ...rest
  } = overrides;
  return {
    default_tts_engine: "kokoro-mlx",
    default_tts_quantization: "bf16",
    cleaner_runtime_availability: { api: true, cuda: cudaAvailable, mlx: mlxAvailable },
    mlx_runtime_available: mlxAvailable,
    runtime_probe: {
      state: probeState,
      refresh_in_progress: refreshInProgress,
      cleaner: { available: mlxAvailable, latency_ms: null, error: null },
    },
    ...rest,
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

function ConversionProbe({ apiClient }: { apiClient: ApiClient }): null {
  const result = useConversionOptions({ apiClient, conversionMode: "audiobook" });
  (globalThis as typeof globalThis & { conversionProbe?: UseConversionOptionsResult }).conversionProbe =
    result;
  return null;
}

function getConversionProbe(): UseConversionOptionsResult {
  return (globalThis as typeof globalThis & { conversionProbe: UseConversionOptionsResult }).conversionProbe;
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

describe("useConversionOptions cleaner selector capability polling", () => {
  let root: Root;
  let container: HTMLDivElement;

  beforeEach(() => {
    vi.useFakeTimers();
    window.localStorage.clear();
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT =
      true;
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
    delete (globalThis as typeof globalThis & { conversionProbe?: UseConversionOptionsResult })
      .conversionProbe;
    window.localStorage.clear();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("exposes Local MLX on the cleaner selector after a stale bootstrap is refreshed by polling", async () => {
    const stale = capabilities({
      probeState: "stale",
      refreshInProgress: true,
      mlxAvailable: false,
    });
    const fresh = capabilities({
      probeState: "fresh",
      refreshInProgress: false,
      mlxAvailable: true,
    });
    const bootstrap = deferred<{ models: ModelOptionsResponse; capabilities: SystemCapabilitiesResponse }>();
    getRuntimeBootstrap.mockReturnValue(bootstrap.promise);

    const getSystemCapabilities = vi.fn().mockResolvedValue(fresh);
    const getVoiceOptions = vi.fn(async () => ({ engine: "kokoro-mlx", voices: [] }));
    const apiClient = { getSystemCapabilities, getVoiceOptions } as unknown as ApiClient;

    await act(async () => {
      root.render(<ConversionProbe apiClient={apiClient} />);
    });

    await act(async () => {
      bootstrap.resolve({ models: modelOptions, capabilities: stale });
      await bootstrap.promise;
      await Promise.resolve();
      await Promise.resolve();
    });

    // Eager refresh runs as soon as the pinned stale bootstrap resolves.
    expect(getSystemCapabilities).toHaveBeenCalled();
    expect(getConversionProbe().cleanerBackendOptions.map((option) => option.label)).toEqual([
      "Cloud API",
      "Local MLX",
    ]);
    expect(getConversionProbe().cleanerBackend).toBe("mlx");
    expect(getConversionProbe().cleanerBackendConfigured).toBe(false);
  });

  it("does not let a late stale bootstrap clobber Local MLX on the cleaner selector", async () => {
    const stale = capabilities({
      probeState: "stale",
      refreshInProgress: true,
      mlxAvailable: false,
    });
    const fresh = capabilities({
      probeState: "fresh",
      refreshInProgress: false,
      mlxAvailable: true,
    });
    const bootstrap = deferred<{ models: ModelOptionsResponse; capabilities: SystemCapabilitiesResponse }>();
    getRuntimeBootstrap.mockReturnValue(bootstrap.promise);

    const getSystemCapabilities = vi.fn().mockResolvedValue(fresh);
    const getVoiceOptions = vi.fn(async () => ({ engine: "kokoro-mlx", voices: [] }));
    const apiClient = { getSystemCapabilities, getVoiceOptions } as unknown as ApiClient;

    await act(async () => {
      root.render(<ConversionProbe apiClient={apiClient} />);
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_500);
    });
    expect(getSystemCapabilities).toHaveBeenCalled();
    expect(getConversionProbe().cleanerBackendOptions).toEqual([]);

    await act(async () => {
      bootstrap.resolve({ models: modelOptions, capabilities: stale });
      await bootstrap.promise;
    });

    expect(getConversionProbe().cleanerBackendOptions.map((option) => option.label)).toEqual([
      "Cloud API",
      "Local MLX",
    ]);
    expect(getConversionProbe().cleanerBackend).toBe("mlx");
    expect(getConversionProbe().cleanerBackendConfigured).toBe(false);
  });

  it("exposes Local MLX when a poll stays stale/refreshing but marks mlx available", async () => {
    const initial = capabilities({
      probeState: "stale",
      refreshInProgress: true,
      mlxAvailable: false,
    });
    const stillRefreshingButReady = capabilities({
      probeState: "stale",
      refreshInProgress: true,
      mlxAvailable: true,
    });
    getRuntimeBootstrap.mockResolvedValue({ models: modelOptions, capabilities: initial });

    const getSystemCapabilities = vi.fn().mockResolvedValue(stillRefreshingButReady);
    const getVoiceOptions = vi.fn(async () => ({ engine: "kokoro-mlx", voices: [] }));
    const apiClient = { getSystemCapabilities, getVoiceOptions } as unknown as ApiClient;

    await act(async () => {
      root.render(<ConversionProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(getConversionProbe().cleanerBackendOptions.map((option) => option.label)).toEqual([
      "Cloud API",
      "Local MLX",
    ]);
    expect(getConversionProbe().cleanerBackend).toBe("mlx");
    expect(getConversionProbe().cleanerBackendConfigured).toBe(false);
  });

  it("keeps polling past a fresh mlx-unavailable snapshot until Local MLX appears", async () => {
    // MLX-profile builds publish primary_runtime=mlx even while the cleaner
    // availability map is still explicitly false after a terminal probe.
    const freshUnavailable = capabilities({
      probeState: "fresh",
      refreshInProgress: false,
      mlxAvailable: false,
      primary_runtime: "mlx",
    });
    const freshAvailable = capabilities({
      probeState: "fresh",
      refreshInProgress: false,
      mlxAvailable: true,
      primary_runtime: "mlx",
    });
    getRuntimeBootstrap.mockResolvedValue({ models: modelOptions, capabilities: freshUnavailable });

    const getSystemCapabilities = vi
      .fn()
      .mockResolvedValueOnce(freshUnavailable)
      .mockResolvedValue(freshAvailable);
    const getVoiceOptions = vi.fn(async () => ({ engine: "kokoro-mlx", voices: [] }));
    const apiClient = { getSystemCapabilities, getVoiceOptions } as unknown as ApiClient;

    await act(async () => {
      root.render(<ConversionProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(getConversionProbe().cleanerBackendOptions.map((option) => option.label)).toEqual([
      "Cloud API",
    ]);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_500);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });

    expect(getConversionProbe().cleanerBackendOptions.map((option) => option.label)).toEqual([
      "Cloud API",
      "Local MLX",
    ]);
    expect(getConversionProbe().cleanerBackend).toBe("mlx");
  });

  it("keeps an explicitly configured Cloud API backend after Local MLX becomes available", async () => {
    const persisted: PersistedSettings = {
      cleaner_backend: "api",
      cleaner_backend_configured: true,
    };
    savePersistedSettings(persisted);
    expect(readPersistedSettings().cleaner_backend_configured).toBe(true);

    const stale = capabilities({
      probeState: "stale",
      refreshInProgress: true,
      mlxAvailable: false,
    });
    const fresh = capabilities({
      probeState: "fresh",
      refreshInProgress: false,
      mlxAvailable: true,
    });
    getRuntimeBootstrap.mockResolvedValue({ models: modelOptions, capabilities: stale });

    const getSystemCapabilities = vi.fn().mockResolvedValue(fresh);
    const getVoiceOptions = vi.fn(async () => ({ engine: "kokoro-mlx", voices: [] }));
    const apiClient = { getSystemCapabilities, getVoiceOptions } as unknown as ApiClient;

    await act(async () => {
      root.render(<ConversionProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(getConversionProbe().cleanerBackend).toBe("api");
    expect(getConversionProbe().cleanerBackendConfigured).toBe(true);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_500);
    });

    expect(getConversionProbe().cleanerBackend).toBe("api");
    expect(getConversionProbe().cleanerBackendConfigured).toBe(true);
    expect(getConversionProbe().cleanerBackendOptions.some((option) => option.value === "mlx")).toBe(
      true,
    );
  });

  it("preserves a legacy Local MLX preference through an unavailable startup probe", async () => {
    const legacySettings: PersistedSettings = {
      cleaner_backend: "mlx",
      cleaner_model: "mlx-community/Llama-3.2-3B-Instruct-4bit",
      cleaner_backend_configured: true,
    };
    savePersistedSettings(legacySettings);

    const unavailable = capabilities({
      probeState: "stale",
      refreshInProgress: true,
      mlxAvailable: false,
    });
    const available = capabilities({
      probeState: "fresh",
      refreshInProgress: false,
      mlxAvailable: true,
    });
    getRuntimeBootstrap.mockResolvedValue({ models: modelOptions, capabilities: unavailable });
    const availableCapabilities = deferred<SystemCapabilitiesResponse>();
    const getSystemCapabilities = vi.fn().mockReturnValue(availableCapabilities.promise);
    const getVoiceOptions = vi.fn(async () => ({ engine: "kokoro-mlx", voices: [] }));
    const apiClient = { getSystemCapabilities, getVoiceOptions } as unknown as ApiClient;

    await act(async () => {
      root.render(<ConversionProbe apiClient={apiClient} />);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(getConversionProbe().cleanerBackend).toBe("api");
    const persistedDuringFallback = readPersistedSettings();
    expect(persistedDuringFallback.preferred_cleaner_backend).toBe("mlx");
    expect(persistedDuringFallback.preferred_cleaner_model).toBe("mlx-community/Llama-3.2-3B-Instruct-4bit");

    await act(async () => {
      availableCapabilities.resolve(available);
      await availableCapabilities.promise;
      await Promise.resolve();
      await Promise.resolve();
    });

    const persisted = readPersistedSettings();
    expect(persisted.preferred_cleaner_backend).toBe("mlx");
    expect(persisted.preferred_cleaner_model).toBe("mlx-community/Llama-3.2-3B-Instruct-4bit");
    expect(getConversionProbe().cleanerBackend).toBe("mlx");
    expect(getConversionProbe().cleanerModel).toBe("mlx-community/Llama-3.2-3B-Instruct-4bit");
  });

  it("preserves CUDA tuning through an unavailable startup fallback", async () => {
    const legacySettings: PersistedSettings = {
      cleaner_backend: "cuda",
      cleaner_model: "Qwen/Qwen2.5-1.5B-Instruct",
      cleaner_backend_configured: true,
      cleaner_cuda_device: "cuda:1",
      cleaner_cuda_quantization: "4bit",
    };
    savePersistedSettings(legacySettings);

    const unavailable = capabilities({
      probeState: "stale",
      refreshInProgress: true,
      mlxAvailable: false,
      cudaAvailable: false,
    });
    const available = capabilities({
      probeState: "fresh",
      refreshInProgress: false,
      mlxAvailable: false,
      cudaAvailable: true,
    });
    getRuntimeBootstrap.mockResolvedValue({ models: modelOptions, capabilities: unavailable });
    const availableCapabilities = deferred<SystemCapabilitiesResponse>();
    const getSystemCapabilities = vi.fn().mockReturnValue(availableCapabilities.promise);
    const getVoiceOptions = vi.fn(async () => ({ engine: "kokoro-mlx", voices: [] }));
    const apiClient = { getSystemCapabilities, getVoiceOptions } as unknown as ApiClient;

    await act(async () => {
      root.render(<ConversionProbe apiClient={apiClient} />);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(getConversionProbe().cleanerBackend).toBe("api");
    expect(getConversionProbe().cleanerCudaDevice).toBe("");
    expect(getConversionProbe().cleanerCudaQuantization).toBe("");
    const persistedDuringFallback = readPersistedSettings();
    expect(persistedDuringFallback.cleaner_cuda_device).toBe("cuda:1");
    expect(persistedDuringFallback.cleaner_cuda_quantization).toBe("4bit");

    await act(async () => {
      availableCapabilities.resolve(available);
      await availableCapabilities.promise;
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(getConversionProbe().cleanerBackend).toBe("cuda");
    expect(getConversionProbe().cleanerCudaDevice).toBe("cuda:1");
    expect(getConversionProbe().cleanerCudaQuantization).toBe("4bit");
    const persisted = readPersistedSettings();
    expect(persisted.cleaner_cuda_device).toBe("cuda:1");
    expect(persisted.cleaner_cuda_quantization).toBe("4bit");
  });
});
