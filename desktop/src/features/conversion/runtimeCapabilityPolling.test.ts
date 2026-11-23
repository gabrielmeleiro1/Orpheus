/** @vitest-environment happy-dom */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "../../api/client";
import type { SystemCapabilitiesResponse } from "../../types/api";
import {
  CAPABILITIES_PROBE_MAX_WAIT_MS,
  shouldPollRuntimeCapabilities,
  startRuntimeCapabilityPolling,
} from "./runtimeCapabilityPolling";

function capabilities(
  overrides: Partial<SystemCapabilitiesResponse> & {
    probeState?: SystemCapabilitiesResponse["runtime_probe"]["state"];
    refreshInProgress?: boolean;
    mlxAvailable?: boolean;
  } = {},
): SystemCapabilitiesResponse {
  const {
    probeState = "pending",
    refreshInProgress = false,
    mlxAvailable = false,
    ...rest
  } = overrides;
  return {
    default_tts_engine: "kokoro-mlx",
    default_tts_quantization: "bf16",
    cleaner_runtime_availability: { mlx: mlxAvailable },
    mlx_runtime_available: mlxAvailable,
    runtime_probe: {
      state: probeState,
      refresh_in_progress: refreshInProgress,
    },
    ...rest,
  };
}

describe("shouldPollRuntimeCapabilities", () => {
  it("polls while the probe is pending, stale, or refresh_in_progress", () => {
    expect(shouldPollRuntimeCapabilities({ state: "pending", refresh_in_progress: false })).toBe(true);
    expect(shouldPollRuntimeCapabilities({ state: "stale", refresh_in_progress: false })).toBe(true);
    expect(shouldPollRuntimeCapabilities({ state: "fresh", refresh_in_progress: true })).toBe(true);
    expect(shouldPollRuntimeCapabilities(null)).toBe(true);
    expect(shouldPollRuntimeCapabilities(undefined)).toBe(true);
  });

  it("stops once the probe is fresh and idle with MLX cleaner resolved", () => {
    expect(shouldPollRuntimeCapabilities({ state: "fresh", refresh_in_progress: false })).toBe(false);
    expect(
      shouldPollRuntimeCapabilities(
        { state: "fresh", refresh_in_progress: false },
        capabilities({ probeState: "fresh", mlxAvailable: true, primary_runtime: "mlx" }),
      ),
    ).toBe(false);
  });

  it("keeps polling a fresh probe while MLX primary still has cleaner mlx=false", () => {
    expect(
      shouldPollRuntimeCapabilities(
        { state: "fresh", refresh_in_progress: false },
        capabilities({ probeState: "fresh", mlxAvailable: false, primary_runtime: "mlx" }),
      ),
    ).toBe(true);
  });

  it("treats runtime identity case-insensitively", () => {
    expect(
      shouldPollRuntimeCapabilities(
        { state: "fresh", refresh_in_progress: false },
        capabilities({ probeState: "fresh", mlxAvailable: false, primary_runtime: "MLX" }),
      ),
    ).toBe(true);
  });
});

describe("startRuntimeCapabilityPolling", () => {
  it("stops optional cleaner discovery quietly after repeated usable TTS snapshots", async () => {
    const ready = capabilities({ probeState: "fresh", mlxAvailable: true,
      primary_runtime: "mlx", cleaner_runtime_availability: { mlx: false } });
    const getSystemCapabilities = vi.fn().mockResolvedValue(ready);
    const onError = vi.fn();
    const onCapabilities = vi.fn();
    const poller = startRuntimeCapabilityPolling({ getSystemCapabilities, onCapabilities, onError });
    await vi.advanceTimersByTimeAsync(CAPABILITIES_PROBE_MAX_WAIT_MS + 30_000);
    expect(onCapabilities).toHaveBeenLastCalledWith(ready);
    expect(onError).not.toHaveBeenCalled();
    const calls = getSystemCapabilities.mock.calls.length;
    await vi.advanceTimersByTimeAsync(30_000);
    expect(getSystemCapabilities).toHaveBeenCalledTimes(calls);
    expect(vi.getTimerCount()).toBe(0);
    poller.stop();
  });

  it("allows sequential frozen probes to settle after their lock waits", async () => {
    const startedAt = Date.now();
    const pending = capabilities();
    const ready = capabilities({ probeState: "fresh", mlxAvailable: true });
    const onCapabilities = vi.fn();
    const onError = vi.fn();
    const poller = startRuntimeCapabilityPolling({
      getSystemCapabilities: async () => Date.now() - startedAt < 720_000 ? pending : ready,
      onCapabilities, onError,
    });
    await vi.advanceTimersByTimeAsync(735_000);
    expect(onCapabilities).toHaveBeenLastCalledWith(ready);
    expect(onError).not.toHaveBeenCalled();
    poller.stop();
  });

  it("reports exhaustion when successful snapshots never expose usable TTS", async () => {
    const onError = vi.fn();
    const poller = startRuntimeCapabilityPolling({
      getSystemCapabilities: async () => capabilities(),
      onCapabilities: vi.fn(), onError, baseMs: 100, maxWaitMs: 500,
    });
    await vi.advanceTimersByTimeAsync(500);
    expect(onError).toHaveBeenCalledWith(expect.objectContaining({ code: "timeout" }));
    poller.stop();
  });

  it.each(["mlx", "cpu", "cuda"])("preserves usable %s bootstrap options when optional polling fails", async (primaryRuntime) => {
    const onError = vi.fn();
    const poller = startRuntimeCapabilityPolling({
      getSystemCapabilities: async () => { throw new Error("offline"); },
      onCapabilities: vi.fn(), onError, baseMs: 100, maxWaitMs: 500,
    });
    poller.noteCapabilities(capabilities({ primary_runtime: primaryRuntime,
      default_tts_engine: primaryRuntime === "mlx" ? "kokoro-mlx" : "kokoro",
      mlx_runtime_available: primaryRuntime === "mlx" }));
    await vi.advanceTimersByTimeAsync(500);
    expect(onError).not.toHaveBeenCalled();
    poller.stop();
  });

  it("keeps checking a cold CUDA probe beyond the former sixty-second cutoff", async () => {
    const startedAt = Date.now();
    const pending = capabilities({ primary_runtime: "cpu", cleaner_runtime_availability: { cuda: false } });
    const ready = capabilities({ probeState: "fresh", primary_runtime: "cpu", cleaner_runtime_availability: { cuda: true } });
    const onCapabilities = vi.fn();
    const poller = startRuntimeCapabilityPolling({
      getSystemCapabilities: async () => Date.now() - startedAt < 96_000 ? pending : ready,
      onCapabilities,
    });
    poller.noteCapabilities(pending);
    await vi.advanceTimersByTimeAsync(105_000);
    expect(onCapabilities).toHaveBeenLastCalledWith(ready);
    const calls = onCapabilities.mock.calls.length;
    await vi.advanceTimersByTimeAsync(30_000);
    expect(onCapabilities).toHaveBeenCalledTimes(calls);
    poller.stop();
  });
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("aborts a hung request at the overall budget and reports a timeout", async () => {
    let requestSignal: AbortSignal | undefined;
    vi.spyOn(globalThis, "fetch").mockImplementation((_input, init) => {
      requestSignal = init?.signal ?? undefined;
      return new Promise((_resolve, reject) => {
        requestSignal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      });
    });
    const client = createApiClient({ baseUrl: "http://desktop.test" });
    const failures: unknown[] = [];
    const onError = vi.fn();
    const onCapabilities = vi.fn();
    const poller = startRuntimeCapabilityPolling({
      getSystemCapabilities: (signal) => client.getSystemCapabilities({ signal }).catch((error: unknown) => {
        failures.push(error);
        throw error;
      }),
      onCapabilities,
      onError,
      baseMs: 100,
      maxWaitMs: 500,
    });
    await vi.advanceTimersByTimeAsync(499);
    expect(requestSignal?.aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(requestSignal?.aborted).toBe(true);
    expect(failures).toEqual([expect.objectContaining({ code: "timeout", retriable: true })]);
    expect(onError).toHaveBeenCalledWith(expect.objectContaining({ code: "timeout" }));
    expect(onCapabilities).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
    poller.stop();
  });

  it("retries a hung capability read before the overall budget expires", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementationOnce((_input, init) =>
      new Promise((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      }),
    ).mockResolvedValue(new Response(JSON.stringify(capabilities({ probeState: "fresh", mlxAvailable: true }))));
    const client = createApiClient({ baseUrl: "http://desktop.test" });
    const onCapabilities = vi.fn();
    const poller = startRuntimeCapabilityPolling({
      getSystemCapabilities: (signal) => client.getSystemCapabilities({ signal }),
      onCapabilities,
    });
    await vi.advanceTimersByTimeAsync(12_500);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(onCapabilities).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
    poller.stop();
  });

  it("does not start a poll whose delay extends beyond the remaining budget", async () => {
    const getSystemCapabilities = vi.fn();
    const poller = startRuntimeCapabilityPolling({
      getSystemCapabilities,
      onCapabilities: vi.fn(),
      baseMs: 1500,
      maxWaitMs: 500,
    });
    await vi.advanceTimersByTimeAsync(2000);
    expect(getSystemCapabilities).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
    poller.stop();
  });

  it("re-fetches while pending and stops once fresh", async () => {
    const pending = capabilities({ probeState: "pending", mlxAvailable: false });
    const fresh = capabilities({ probeState: "fresh", mlxAvailable: true });
    const getSystemCapabilities = vi
      .fn()
      .mockResolvedValueOnce(pending)
      .mockResolvedValue(fresh);
    const onCapabilities = vi.fn();

    const poller = startRuntimeCapabilityPolling({
      getSystemCapabilities: (signal) => getSystemCapabilities({ signal }),
      onCapabilities,
      baseMs: 100,
      maxMs: 100,
      maxWaitMs: 10_000,
    });
    poller.noteCapabilities(pending);

    await vi.advanceTimersByTimeAsync(100);
    expect(getSystemCapabilities).toHaveBeenCalledTimes(1);
    expect(onCapabilities).toHaveBeenLastCalledWith(pending);

    await vi.advanceTimersByTimeAsync(100);
    expect(getSystemCapabilities).toHaveBeenCalledTimes(2);
    expect(onCapabilities).toHaveBeenLastCalledWith(fresh);

    const callsAfterFresh = getSystemCapabilities.mock.calls.length;
    await vi.advanceTimersByTimeAsync(5_000);
    expect(getSystemCapabilities).toHaveBeenCalledTimes(callsAfterFresh);

    poller.stop();
  });

  it("keeps polling while refresh_in_progress even when state is fresh", async () => {
    const refreshing = capabilities({
      probeState: "fresh",
      refreshInProgress: true,
      mlxAvailable: false,
    });
    const idle = capabilities({ probeState: "fresh", refreshInProgress: false, mlxAvailable: true });
    const getSystemCapabilities = vi.fn().mockResolvedValue(idle);
    const onCapabilities = vi.fn();

    const poller = startRuntimeCapabilityPolling({
      getSystemCapabilities: (signal) => getSystemCapabilities({ signal }),
      onCapabilities,
      baseMs: 100,
      maxMs: 100,
    });
    poller.noteCapabilities(refreshing);

    await vi.advanceTimersByTimeAsync(100);
    expect(getSystemCapabilities).toHaveBeenCalledTimes(1);
    expect(onCapabilities).toHaveBeenCalledWith(idle);

    const callsAfterIdle = getSystemCapabilities.mock.calls.length;
    await vi.advanceTimersByTimeAsync(5_000);
    expect(getSystemCapabilities).toHaveBeenCalledTimes(callsAfterIdle);

    poller.stop();
  });

  it("clears the timer on stop so later ticks do not fetch", async () => {
    const pending = capabilities({ probeState: "pending" });
    const getSystemCapabilities = vi.fn().mockResolvedValue(pending);
    const poller = startRuntimeCapabilityPolling({
      getSystemCapabilities: (signal) => getSystemCapabilities({ signal }),
      onCapabilities: vi.fn(),
      baseMs: 100,
      maxMs: 100,
    });
    poller.noteCapabilities(pending);

    poller.stop();
    await vi.advanceTimersByTimeAsync(5_000);
    expect(getSystemCapabilities).not.toHaveBeenCalled();
  });

  it("does not schedule further polls after noteCapabilities marks MLX ready", async () => {
    const pending = capabilities({ probeState: "pending" });
    const fresh = capabilities({ probeState: "fresh", mlxAvailable: true, primary_runtime: "mlx" });
    const getSystemCapabilities = vi.fn().mockResolvedValue(fresh);
    const poller = startRuntimeCapabilityPolling({
      getSystemCapabilities: (signal) => getSystemCapabilities({ signal }),
      onCapabilities: vi.fn(),
      baseMs: 100,
      maxMs: 100,
    });
    poller.noteCapabilities(pending);

    poller.noteCapabilities(fresh);
    await vi.advanceTimersByTimeAsync(5_000);
    expect(getSystemCapabilities).not.toHaveBeenCalled();
    poller.stop();
  });

  it("keeps polling after a fresh mlx-unavailable bootstrap until cleaner mlx flips", async () => {
    const freshUnavailable = capabilities({
      probeState: "fresh",
      mlxAvailable: false,
      primary_runtime: "mlx",
    });
    const freshAvailable = capabilities({
      probeState: "fresh",
      mlxAvailable: true,
      primary_runtime: "mlx",
    });
    const getSystemCapabilities = vi.fn().mockResolvedValue(freshAvailable);
    const onCapabilities = vi.fn();

    const poller = startRuntimeCapabilityPolling({
      getSystemCapabilities: (signal) => getSystemCapabilities({ signal }),
      onCapabilities,
      baseMs: 100,
      maxMs: 100,
    });
    poller.noteCapabilities(freshUnavailable);

    await vi.advanceTimersByTimeAsync(100);
    expect(getSystemCapabilities).toHaveBeenCalledTimes(1);
    expect(onCapabilities).toHaveBeenCalledWith(freshAvailable);

    const callsAfterReady = getSystemCapabilities.mock.calls.length;
    await vi.advanceTimersByTimeAsync(5_000);
    expect(getSystemCapabilities).toHaveBeenCalledTimes(callsAfterReady);
    poller.stop();
  });
});

describe("retainUsedCapabilities", () => {
  it("keeps the previous snapshot only when every value is the same", async () => {
    const { retainUsedCapabilities } = await import("./runtimeCapabilityPolling");
    const previous = { default_tts_engine: "kokoro", cleaner_runtime_availability: { mlx: true }, runtime_probe: { state: "fresh", refresh_in_progress: false } } as never;
    const same = { runtime_probe: { refresh_in_progress: false, state: "fresh" }, cleaner_runtime_availability: { mlx: true }, default_tts_engine: "kokoro" } as never;
    const changed = { default_tts_engine: "kokoro", cleaner_runtime_availability: { mlx: false }, runtime_probe: { state: "fresh", refresh_in_progress: false } } as never;
    expect(retainUsedCapabilities(previous, same)).toBe(previous);
    expect(retainUsedCapabilities(previous, changed)).toBe(changed);
    expect(retainUsedCapabilities(null, same)).toBe(same);
  });

  it("ignores counters and live memory readings but not the worker limits derived from them", async () => {
    const { retainUsedCapabilities } = await import("./runtimeCapabilityPolling");
    const recommendation = (available: number, maxSafe: number) => ({
      memory_measurement_available: true, total_memory_bytes: 32e9, available_memory_bytes: available,
      reserved_memory_bytes: 4e9, headroom_memory_bytes: available - 4e9, static_estimated_worker_bytes: 2e9,
      warmup_memory_delta_bytes: null, estimated_per_worker_memory_bytes: 2e9, requested_parallel_chapters: 4,
      max_safe_parallel_chapters: maxSafe, recommended_parallel_chapters: maxSafe, reason: "static estimate with live headroom",
      calculation: `available - reserved = ${available}`,
    });
    const snapshot = (available: number, maxSafe: number, waits: number) => ({
      default_tts_engine: "kokoro", default_tts_quantization: "", cleaner_runtime_availability: { mlx: true },
      runtime_probe: { state: "pending", refresh_in_progress: true },
      runtime_validation_cache_size: waits, global_api_limiter: { wait_count: waits }, worker_pool: { reuse_hits: waits },
      metrics: { global_api_limiter_wait_count: waits },
      parallel_worker_recommendation: recommendation(available, maxSafe),
    }) as never;
    const previous = snapshot(12e9, 4, 0);
    expect(retainUsedCapabilities(previous, snapshot(11.5e9, 4, 3))).toBe(previous);
    const fewerWorkers = snapshot(5e9, 1, 3);
    expect(retainUsedCapabilities(previous, fewerWorkers)).toBe(fewerWorkers);
    const settled = { ...(previous as object), runtime_probe: { state: "fresh", refresh_in_progress: false } } as never;
    expect(retainUsedCapabilities(previous, settled)).toBe(settled);
  });
});
