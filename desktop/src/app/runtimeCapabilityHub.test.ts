import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { ApiClient } from "../api/client";
import type { SystemCapabilitiesResponse } from "../types/api";
import { subscribeRuntimeCapabilities } from "./runtimeCapabilityHub";

function snapshot(state: "pending" | "fresh"): SystemCapabilitiesResponse {
  return {
    default_tts_engine: "kokoro", default_tts_quantization: "bf16",
    cleaner_runtime_availability: { mlx: state === "fresh" },
    mlx_runtime_available: state === "fresh",
    runtime_probe: { state, refresh_in_progress: state === "pending" },
  };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

it("polls once per client for every consumer and stops only when the last one leaves", async () => {
  const getSystemCapabilities = vi.fn(async () => snapshot("pending"));
  const apiClient = { getSystemCapabilities } as unknown as ApiClient;
  const first = vi.fn();
  const second = vi.fn();
  const a = subscribeRuntimeCapabilities(apiClient, { onCapabilities: first });
  const b = subscribeRuntimeCapabilities(apiClient, { onCapabilities: second });
  expect(a.pollApplied).toBe(false);
  await vi.advanceTimersByTimeAsync(1_500);
  expect(getSystemCapabilities).toHaveBeenCalledOnce();
  expect(first).toHaveBeenCalledOnce();
  expect(second).toHaveBeenCalledOnce();
  expect(a.pollApplied).toBe(true);

  a.unsubscribe();
  await vi.advanceTimersByTimeAsync(3_000);
  expect(getSystemCapabilities).toHaveBeenCalledTimes(2);
  expect(first).toHaveBeenCalledOnce();
  expect(second).toHaveBeenCalledTimes(2);

  b.unsubscribe();
  await vi.advanceTimersByTimeAsync(60_000);
  expect(getSystemCapabilities).toHaveBeenCalledTimes(2);
  expect(vi.getTimerCount()).toBe(0);
});

it("stops polling once the probe settles, and a later consumer does not re-poll a settled runtime", async () => {
  const getSystemCapabilities = vi.fn(async () => snapshot("fresh"));
  const apiClient = { getSystemCapabilities } as unknown as ApiClient;
  const a = subscribeRuntimeCapabilities(apiClient, { onCapabilities: vi.fn() });
  await vi.advanceTimersByTimeAsync(1_500);
  expect(getSystemCapabilities).toHaveBeenCalledOnce();
  await vi.advanceTimersByTimeAsync(30_000);
  expect(getSystemCapabilities).toHaveBeenCalledOnce();

  const b = subscribeRuntimeCapabilities(apiClient, { onCapabilities: vi.fn() });
  expect(b.pollApplied).toBe(false);
  await vi.advanceTimersByTimeAsync(30_000);
  expect(getSystemCapabilities).toHaveBeenCalledOnce();
  // A consumer's own pending snapshot resumes polling for everyone.
  b.noteCapabilities(snapshot("pending"));
  await vi.advanceTimersByTimeAsync(6_000);
  expect(getSystemCapabilities).toHaveBeenCalledTimes(2);
  a.unsubscribe();
  b.unsubscribe();
});

it("keeps clients separate", async () => {
  const one = { getSystemCapabilities: vi.fn(async () => snapshot("fresh")) } as unknown as ApiClient;
  const two = { getSystemCapabilities: vi.fn(async () => snapshot("fresh")) } as unknown as ApiClient;
  const a = subscribeRuntimeCapabilities(one, { onCapabilities: vi.fn() });
  const b = subscribeRuntimeCapabilities(two, { onCapabilities: vi.fn() });
  await vi.advanceTimersByTimeAsync(1_500);
  expect(one.getSystemCapabilities).toHaveBeenCalledOnce();
  expect(two.getSystemCapabilities).toHaveBeenCalledOnce();
  a.unsubscribe();
  b.unsubscribe();
});
