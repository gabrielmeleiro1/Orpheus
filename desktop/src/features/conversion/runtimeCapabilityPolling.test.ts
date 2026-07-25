/** @vitest-environment happy-dom */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SystemCapabilitiesResponse } from "../../types/api";
import {
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
});

describe("startRuntimeCapabilityPolling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
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
      initialProbe: pending.runtime_probe,
      baseMs: 100,
      maxMs: 100,
      maxWaitMs: 10_000,
    });

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
      initialProbe: refreshing.runtime_probe,
      baseMs: 100,
      maxMs: 100,
    });

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
      initialProbe: pending.runtime_probe,
      baseMs: 100,
      maxMs: 100,
    });

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
      initialProbe: pending.runtime_probe,
      baseMs: 100,
      maxMs: 100,
    });

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
      initialCapabilities: freshUnavailable,
      baseMs: 100,
      maxMs: 100,
    });

    await vi.advanceTimersByTimeAsync(100);
    expect(getSystemCapabilities).toHaveBeenCalledTimes(1);
    expect(onCapabilities).toHaveBeenCalledWith(freshAvailable);

    const callsAfterReady = getSystemCapabilities.mock.calls.length;
    await vi.advanceTimersByTimeAsync(5_000);
    expect(getSystemCapabilities).toHaveBeenCalledTimes(callsAfterReady);
    poller.stop();
  });
});
