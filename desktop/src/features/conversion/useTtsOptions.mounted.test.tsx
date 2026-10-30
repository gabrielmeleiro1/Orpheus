// @vitest-environment happy-dom

import { act, useRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient, type ApiClient } from "../../api/client";
import { ApiError } from "../../api/http";
import type { ModelOptionsResponse, SystemCapabilitiesResponse } from "../../types/api";
import { useTtsOptions, type UseTtsOptionsResult } from "./useTtsOptions";
import type { PersistedSettings } from "./conversionOptionsShared";
import { CAPABILITIES_PROBE_MAX_WAIT_MS, CAPABILITIES_PROBE_REQUEST_TIMEOUT_MS } from "./runtimeCapabilityPolling";
import * as runtimeCapabilityPolling from "./runtimeCapabilityPolling";

const { getRuntimeBootstrap } = vi.hoisted(() => ({
  getRuntimeBootstrap: vi.fn(),
}));

vi.mock("../../app/runtimeBootstrapCache", () => ({
  getRuntimeBootstrap,
  updateRuntimeBootstrapCapabilities: vi.fn(),
  invalidateRuntimeBootstrap: vi.fn(),
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

function TtsProbe({ apiClient, runtimeSelectionGeneration = 0, onRender }: {
  apiClient: ApiClient | null;
  runtimeSelectionGeneration?: number;
  onRender?: (result: UseTtsOptionsResult) => void;
}): null {
  const persistedSettingsRef = useRef({} as PersistedSettings);
  const settingsHydratedRef = useRef(false);
  const result = useTtsOptions({ apiClient, persistedSettingsRef, settingsHydratedRef, runtimeSelectionGeneration });
  (globalThis as typeof globalThis & { ttsProbe?: UseTtsOptionsResult }).ttsProbe = result;
  onRender?.(result);
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

  it("recovers a stalled bootstrap through retry without replacing the client or remounting", async () => {
    const cache = await vi.importActual<typeof import("../../app/runtimeBootstrapCache")>("../../app/runtimeBootstrapCache");
    getRuntimeBootstrap.mockImplementation(cache.getRuntimeBootstrap);
    const ready = capabilities({ probeState: "fresh", refreshInProgress: false, mlxAvailable: true });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      if (String(input).endsWith("/api/options/models")) {
        return new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
        });
      }
      return Promise.resolve(new Response(JSON.stringify(ready)));
    });
    const apiClient = createApiClient({ baseUrl: "http://desktop.test" });
    await act(async () => root.render(<TtsProbe apiClient={apiClient} />));
    await act(async () => vi.advanceTimersByTimeAsync(8000));
    expect(getTtsProbe().loading).toBe(false);
    expect(getTtsProbe().error).toBe("request timed out");
    fetchMock.mockImplementation(async (input) => new Response(JSON.stringify(
      String(input).endsWith("/api/options/models") ? modelOptions
        : String(input).includes("/api/options/voices") ? { voices: [] } : ready,
    )));
    await act(async () => getTtsProbe().retryRuntimeOptions());
    expect(getTtsProbe().loading).toBe(false);
    expect(getTtsProbe().error).toBe("");
    expect(getTtsProbe().modelOptions).toEqual(modelOptions);
  });

  it("reloads options after a stalled bootstrap and a runtime selection change", async () => {
    const cache = await vi.importActual<typeof import("../../app/runtimeBootstrapCache")>("../../app/runtimeBootstrapCache");
    getRuntimeBootstrap.mockImplementation(cache.getRuntimeBootstrap);
    const ready = capabilities({ probeState: "fresh", refreshInProgress: false, mlxAvailable: true });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      if (String(input).endsWith("/api/options/models")) {
        return new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
        });
      }
      return Promise.resolve(new Response(JSON.stringify(ready)));
    });
    const apiClient = createApiClient({ baseUrl: "http://desktop.test" });
    await act(async () => root.render(<TtsProbe apiClient={apiClient} />));
    await act(async () => vi.advanceTimersByTimeAsync(8000));
    expect(getTtsProbe().loading).toBe(false);
    expect(getTtsProbe().error).toBe("request timed out");
    fetchMock.mockImplementation(async (input) => new Response(JSON.stringify(
      String(input).endsWith("/api/options/models") ? modelOptions
        : String(input).includes("/api/options/voices") ? { voices: [] } : ready,
    )));
    await act(async () => root.render(<TtsProbe apiClient={apiClient} runtimeSelectionGeneration={1} />));
    expect(fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/api/options/models"))).toHaveLength(2);
    expect(getTtsProbe().loading).toBe(false);
    expect(getTtsProbe().error).toBe("");
    expect(getTtsProbe().modelOptions).toEqual(modelOptions);
  });

  it.each(["client", "generation", "engine"])("waits for the current voice response after a %s change", async (change) => {
    const fresh = capabilities({ probeState: "fresh", refreshInProgress: false, mlxAvailable: true });
    const models = { ...modelOptions, tts_engines: [
      ...modelOptions.tts_engines, { ...modelOptions.tts_engines[0], id: "new-engine" },
    ] };
    getRuntimeBootstrap.mockResolvedValue({ models, capabilities: fresh });
    const pending = deferred<{ voices: Array<{ id: string; name: string }> }>();
    const firstClient = { getSystemCapabilities: vi.fn().mockResolvedValue(fresh),
      getVoiceOptions: vi.fn().mockResolvedValueOnce({ voices: [{ id: "old-voice", name: "Old" }] })
        .mockReturnValue(pending.promise) } as unknown as ApiClient;
    const secondClient = { getSystemCapabilities: vi.fn().mockResolvedValue(fresh),
      getVoiceOptions: vi.fn().mockReturnValue(pending.promise) } as unknown as ApiClient;
    await act(async () => root.render(<TtsProbe apiClient={firstClient} />));
    expect(getTtsProbe().loading).toBe(false);
    expect(getTtsProbe().selectedVoice).toBe("old-voice");
    await act(async () => {
      if (change === "engine") getTtsProbe().setSelectedEngine("new-engine");
      else root.render(<TtsProbe apiClient={change === "client" ? secondClient : firstClient}
        runtimeSelectionGeneration={change === "generation" ? 1 : 0} />);
    });
    expect(getTtsProbe().loading).toBe(true);
    expect(getTtsProbe().selectedVoice).toBe("");
    expect(getTtsProbe().filteredVoiceOptions).toEqual([]);
    await act(async () => pending.resolve({ voices: [{ id: "new-voice", name: "New" }] }));
    expect(getTtsProbe().loading).toBe(false);
    expect(getTtsProbe().selectedVoice).toBe("new-voice");
    expect(getTtsProbe().filteredVoiceOptions.map((voice) => voice.value)).toEqual(["new-voice"]);
  });

  it("ignores an obsolete voice response after a newer engine has loaded", async () => {
    const fresh = capabilities({ probeState: "fresh", refreshInProgress: false, mlxAvailable: true });
    getRuntimeBootstrap.mockResolvedValue({ models: { ...modelOptions, tts_engines: [
      ...modelOptions.tts_engines, { ...modelOptions.tts_engines[0], id: "new-engine" },
    ] }, capabilities: fresh });
    const staleVoices = deferred<{ voices: Array<{ id: string; name: string }> }>();
    const client = { getSystemCapabilities: vi.fn().mockResolvedValue(fresh),
      getVoiceOptions: vi.fn().mockReturnValueOnce(staleVoices.promise)
        .mockResolvedValue({ voices: [{ id: "current-voice", name: "Current" }] }) } as unknown as ApiClient;
    await act(async () => root.render(<TtsProbe apiClient={client} />));
    await act(async () => getTtsProbe().setSelectedEngine("new-engine"));
    expect(getTtsProbe().selectedVoice).toBe("current-voice");
    await act(async () => staleVoices.resolve({ voices: [{ id: "stale-voice", name: "Stale" }] }));
    expect(getTtsProbe().selectedVoice).toBe("current-voice");
    expect(getTtsProbe().loading).toBe(false);
  });

  it("ends a stalled voice read and recovers through runtime retry", async () => {
    const fresh = capabilities({ probeState: "fresh", refreshInProgress: false, mlxAvailable: true });
    getRuntimeBootstrap.mockResolvedValue({ models: modelOptions, capabilities: fresh });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((_input, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    }));
    const client = createApiClient({ baseUrl: "http://desktop.test" });
    await act(async () => root.render(<TtsProbe apiClient={client} />));
    expect(getTtsProbe().loading).toBe(true);
    await act(async () => vi.advanceTimersByTimeAsync(8000));
    expect(getTtsProbe().loading).toBe(false);
    expect(getTtsProbe().error).toBe("request timed out");
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ voices: [{ id: "recovered", name: "Recovered" }] })));
    await act(async () => getTtsProbe().retryRuntimeOptions());
    expect(getTtsProbe().loading).toBe(false);
    expect(getTtsProbe().error).toBe("");
    expect(getTtsProbe().selectedVoice).toBe("recovered");
  });

  it("shows an error when capability requests remain hung through the probe budget", async () => {
    getRuntimeBootstrap.mockResolvedValue({ models: modelOptions, capabilities: capabilities() });
    const signals: AbortSignal[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation((_input, init) => {
      if (init?.signal) signals.push(init.signal);
      return new Promise((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      });
    });
    const client = createApiClient({ baseUrl: "http://desktop.test" });
    client.getVoiceOptions = vi.fn().mockResolvedValue({ voices: [] });
    await act(async () => root.render(<TtsProbe apiClient={client} />));
    await act(async () => vi.advanceTimersByTimeAsync(8000));
    expect(signals[0]?.aborted).toBe(true);
    await act(async () => vi.advanceTimersByTimeAsync(CAPABILITIES_PROBE_MAX_WAIT_MS - 8000));
    expect(getTtsProbe().error).toContain("request timed out");
    expect(getTtsProbe().loading).toBe(false);
  });

  it("keeps conversion options ready when optional cleaner discovery expires", async () => {
    const ready = capabilities({ probeState: "fresh", refreshInProgress: false,
      mlxAvailable: true, primary_runtime: "mlx", cleaner_runtime_availability: { mlx: false } });
    getRuntimeBootstrap.mockResolvedValue({ models: modelOptions, capabilities: ready });
    const client = { getSystemCapabilities: vi.fn().mockResolvedValue(ready),
      getVoiceOptions: vi.fn().mockResolvedValue({ voices: [] }) } as unknown as ApiClient;
    await act(async () => root.render(<TtsProbe apiClient={client} />));
    await act(async () => vi.advanceTimersByTimeAsync(CAPABILITIES_PROBE_MAX_WAIT_MS + 30_000));
    expect(getTtsProbe().error).toBe("");
    expect(getTtsProbe().loading).toBe(false);
    expect(getTtsProbe().modelOptions).toEqual(modelOptions);
    expect(getTtsProbe().systemCapabilities).toEqual(ready);
    expect(getTtsProbe().selectedEngine).toBe("kokoro-mlx");
  });

  it("restores conversion readiness when bootstrap succeeds after the polling deadline", async () => {
    const bootstrap = deferred<{ models: ModelOptionsResponse; capabilities: SystemCapabilitiesResponse }>();
    getRuntimeBootstrap.mockReturnValue(bootstrap.promise);
    const client = { getSystemCapabilities: vi.fn().mockRejectedValue(new Error("unavailable")),
      getVoiceOptions: vi.fn().mockResolvedValue({ voices: [] }) } as unknown as ApiClient;
    await act(async () => root.render(<TtsProbe apiClient={client} />));
    await act(async () => vi.advanceTimersByTimeAsync(CAPABILITIES_PROBE_MAX_WAIT_MS));
    expect(getTtsProbe().error).toContain("request timed out");

    const fresh = capabilities({ probeState: "fresh", refreshInProgress: false, mlxAvailable: true });
    await act(async () => { bootstrap.resolve({ models: modelOptions, capabilities: fresh }); });

    const options = getTtsProbe();
    const runtimeOptionsReady = !options.loading && !options.error;
    expect(runtimeOptionsReady).toBe(true);
    expect(options.error).toBe("");
    expect(options.modelOptions).toEqual(modelOptions);
    expect(options.systemCapabilities).toEqual(fresh);
  });

  it("ends bootstrap loading at the snapshot deadline and accepts a later success", async () => {
    const bootstrap = deferred<{ models: ModelOptionsResponse; capabilities: SystemCapabilitiesResponse }>();
    getRuntimeBootstrap.mockReturnValue(bootstrap.promise);
    const fresh = capabilities({ probeState: "fresh", refreshInProgress: false, mlxAvailable: true });
    const client = { getSystemCapabilities: vi.fn().mockResolvedValue(fresh),
      getVoiceOptions: vi.fn().mockResolvedValue({ voices: [] }) } as unknown as ApiClient;
    await act(async () => root.render(<TtsProbe apiClient={client} />));
    await act(async () => vi.advanceTimersByTimeAsync(CAPABILITIES_PROBE_REQUEST_TIMEOUT_MS - 1));
    expect(getTtsProbe().loading).toBe(true);
    expect(getTtsProbe().error).toBe("");
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(getTtsProbe().loading).toBe(false);
    expect(getTtsProbe().error).toContain("request timed out");
    expect(getTtsProbe().modelOptions).toBeNull();

    await act(async () => { bootstrap.resolve({ models: modelOptions, capabilities: fresh }); });
    expect(getTtsProbe().loading).toBe(false);
    expect(getTtsProbe().error).toBe("");
    expect(getTtsProbe().modelOptions).toEqual(modelOptions);
  });

  it("preserves a bootstrap failure when capability polling later succeeds", async () => {
    getRuntimeBootstrap.mockRejectedValue(new Error("catalog unavailable"));
    const fresh = capabilities({ probeState: "fresh", refreshInProgress: false, mlxAvailable: true });
    const client = { getSystemCapabilities: vi.fn().mockResolvedValue(fresh),
      getVoiceOptions: vi.fn().mockResolvedValue({ voices: [] }) } as unknown as ApiClient;
    await act(async () => root.render(<TtsProbe apiClient={client} />));
    await act(async () => vi.advanceTimersByTimeAsync(CAPABILITIES_PROBE_MAX_WAIT_MS));
    expect(getTtsProbe().loading).toBe(false);
    expect(getTtsProbe().error).toBe("catalog unavailable");
    expect(getTtsProbe().modelOptions).toBeNull();
  });

  it("preserves an unrelated error when bootstrap succeeds", async () => {
    const bootstrap = deferred<{ models: ModelOptionsResponse; capabilities: SystemCapabilitiesResponse }>();
    getRuntimeBootstrap.mockReturnValue(bootstrap.promise);
    let reportError!: NonNullable<runtimeCapabilityPolling.StartRuntimeCapabilityPollingArgs["onError"]>;
    vi.spyOn(runtimeCapabilityPolling, "startRuntimeCapabilityPolling").mockImplementation(({ onError }) => {
      reportError = onError!;
      return { pollApplied: false, noteCapabilities: vi.fn(), stop: vi.fn() };
    });
    const client = { getVoiceOptions: vi.fn().mockResolvedValue({ voices: [] }) } as unknown as ApiClient;
    await act(async () => root.render(<TtsProbe apiClient={client} />));
    await act(async () => reportError(new ApiError({ message: "access denied", status: 403, code: "http",
      detail: "access denied", retriable: false })));
    await act(async () => vi.advanceTimersByTimeAsync(CAPABILITIES_PROBE_REQUEST_TIMEOUT_MS));
    expect(getTtsProbe().error).toBe("access denied");

    const fresh = capabilities({ probeState: "fresh", refreshInProgress: false, mlxAvailable: true });
    await act(async () => { bootstrap.resolve({ models: modelOptions, capabilities: fresh }); });
    expect(getTtsProbe().loading).toBe(false);
    expect(getTtsProbe().error).toBe("access denied");
    expect(getTtsProbe().modelOptions).toEqual(modelOptions);
  });

  it("waits for usable capabilities before clearing a bootstrap timeout", async () => {
    const bootstrap = deferred<{ models: ModelOptionsResponse; capabilities: SystemCapabilitiesResponse }>();
    getRuntimeBootstrap.mockReturnValue(bootstrap.promise);
    const pending = capabilities();
    const getSystemCapabilities = vi.fn().mockResolvedValue(pending);
    const client = { getSystemCapabilities,
      getVoiceOptions: vi.fn().mockResolvedValue({ voices: [] }) } as unknown as ApiClient;
    await act(async () => root.render(<TtsProbe apiClient={client} />));
    await act(async () => vi.advanceTimersByTimeAsync(CAPABILITIES_PROBE_REQUEST_TIMEOUT_MS));
    await act(async () => { bootstrap.resolve({ models: modelOptions, capabilities: pending }); });
    expect(getTtsProbe().error).toContain("request timed out");

    const fresh = capabilities({ probeState: "fresh", refreshInProgress: false, mlxAvailable: true });
    getSystemCapabilities.mockResolvedValue(fresh);
    await act(async () => vi.advanceTimersByTimeAsync(6000));
    expect(getTtsProbe().error).toBe("");
    expect(getTtsProbe().loading).toBe(false);
    expect(getTtsProbe().systemCapabilities).toEqual(fresh);
  });

  it.each(["client", "generation"])("ignores an expired bootstrap after the runtime %s changes", async (change) => {
    const bootstrap = deferred<{ models: ModelOptionsResponse; capabilities: SystemCapabilitiesResponse }>();
    getRuntimeBootstrap.mockReturnValueOnce(bootstrap.promise);
    const fresh = capabilities({ probeState: "fresh", refreshInProgress: false, mlxAvailable: true });
    const client = { getSystemCapabilities: vi.fn().mockResolvedValue(fresh),
      getVoiceOptions: vi.fn().mockResolvedValue({ voices: [] }) } as unknown as ApiClient;
    await act(async () => root.render(<TtsProbe apiClient={client} />));
    await act(async () => vi.advanceTimersByTimeAsync(CAPABILITIES_PROBE_REQUEST_TIMEOUT_MS));
    expect(getTtsProbe().error).toContain("request timed out");

    getRuntimeBootstrap.mockResolvedValueOnce({ models: modelOptions, capabilities: fresh });
    await act(async () => root.render(<TtsProbe
      apiClient={change === "client" ? { ...client } : client}
      runtimeSelectionGeneration={change === "generation" ? 1 : 0}
    />));
    await act(async () => { bootstrap.resolve({ models: { ...modelOptions, tts_engines: [] }, capabilities: capabilities() }); });
    await act(async () => vi.advanceTimersByTimeAsync(CAPABILITIES_PROBE_MAX_WAIT_MS));
    expect(getTtsProbe().error).toBe("");
    expect(getTtsProbe().loading).toBe(false);
    expect(getTtsProbe().modelOptions).toEqual(modelOptions);
    expect(getTtsProbe().systemCapabilities).toEqual(fresh);
  });

  it.each(["client", "generation"])("hides the previous catalog immediately when the runtime %s changes", async (change) => {
    const fresh = capabilities({ probeState: "fresh", refreshInProgress: false, mlxAvailable: true });
    getRuntimeBootstrap.mockResolvedValueOnce({ models: modelOptions, capabilities: fresh });
    const client = { getSystemCapabilities: vi.fn().mockResolvedValue(fresh),
      getVoiceOptions: vi.fn().mockResolvedValue({ voices: [] }) } as unknown as ApiClient;
    await act(async () => root.render(<TtsProbe apiClient={client} />));
    expect(getTtsProbe().loading).toBe(false);
    const pending = deferred<{ models: ModelOptionsResponse; capabilities: SystemCapabilitiesResponse }>();
    getRuntimeBootstrap.mockReturnValueOnce(pending.promise);
    const renders: UseTtsOptionsResult[] = [];
    await act(async () => root.render(<TtsProbe
      apiClient={change === "client" ? { ...client } : client}
      runtimeSelectionGeneration={change === "generation" ? 1 : 0}
      onRender={(result) => renders.push(result)}
    />));
    expect(renders[0].loading).toBe(true);
    expect(renders[0].modelOptions).toBeNull();
    await act(async () => { pending.resolve({ models: modelOptions, capabilities: fresh }); });
    expect(getTtsProbe().loading).toBe(false);
    expect(getTtsProbe().modelOptions).toEqual(modelOptions);
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

  it("stops polling on unmount", async () => {
    const pending = capabilities({ probeState: "pending", refreshInProgress: true, mlxAvailable: false });
    getRuntimeBootstrap.mockResolvedValue({ models: modelOptions, capabilities: pending });
    const getSystemCapabilities = vi.fn().mockResolvedValue(pending);
    const getVoiceOptions = vi.fn(async () => ({ engine: "kokoro-mlx", voices: [] }));
    const apiClient = { getSystemCapabilities, getVoiceOptions } as unknown as ApiClient;

    await act(async () => {
      root.render(<TtsProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    await act(async () => {
      root.unmount();
    });

    const callsAtUnmount = getSystemCapabilities.mock.calls.length;
    await vi.advanceTimersByTimeAsync(30_000);
    expect(getSystemCapabilities).toHaveBeenCalledTimes(callsAtUnmount);
  });
});
