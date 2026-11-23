// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { ApiClient } from "../api/client";
import type { RuntimeSetupStatusResponse } from "../types/api";
import { useRuntimeSetupStatus } from "./useRuntimeSetupStatus";

const base: RuntimeSetupStatusResponse = {
  schema_version: 1, configured: true, state: "ready", state_error: null,
  selected_profile: "mlx", selected_model_groups: [{ id: "tts-mlx", runtime_ready: true, assets_ready: true, assets_reason: null, ready: true }],
  packages: { ready: true, missing: [], incompatible: [] },
  probe: { ready: true, checks: [] }, ready: true, restart_required: false,
  profiles: [], backend: { instance_id: "one", status: "running" },
};
const checking: RuntimeSetupStatusResponse = { ...base, state: "checking", ready: false };
const adopted: RuntimeSetupStatusResponse = { ...base, selected_model_groups: [] };

type Pending = { resolve: (status: RuntimeSetupStatusResponse) => void; reject: (error: unknown) => void; signal?: AbortSignal };

function controllableClient() {
  const reads: Pending[] = [];
  const apiClient = {
    getRuntimeSetupStatus: vi.fn((options?: { signal?: AbortSignal }) => new Promise<RuntimeSetupStatusResponse>((resolve, reject) => {
      reads.push({ resolve, reject, signal: options?.signal });
    })),
  } as unknown as ApiClient;
  return { apiClient, reads };
}

let latest: ReturnType<typeof useRuntimeSetupStatus>;
function Probe({ apiClient }: { apiClient: ApiClient }) {
  latest = useRuntimeSetupStatus(apiClient);
  return null;
}

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.useFakeTimers();
  localStorage.clear();
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  localStorage.clear();
});

it("keeps a status applied from Settings over an older in-flight read", async () => {
  const { apiClient, reads } = controllableClient();
  const root = createRoot(document.createElement("div"));
  await act(async () => root.render(<Probe apiClient={apiClient} />));
  await act(async () => reads[0].resolve(checking));
  // The check retries while the runtime is being verified; this read is now in flight.
  await act(async () => vi.advanceTimersByTime(2_000));
  expect(reads).toHaveLength(2);

  await act(async () => latest.onSettingsSetupReady(adopted));
  expect(reads[1].signal?.aborted).toBe(true);
  // A stale reply that still lands (no abort support) must not win either.
  await act(async () => reads[1].resolve(checking));
  expect(latest.setupStatus).toBe(adopted);
  // The adopted status is settled, so no further check is scheduled.
  await act(async () => vi.advanceTimersByTime(10_000));
  expect(reads).toHaveLength(2);
  expect(latest.setupStatus).toBe(adopted);
  await act(async () => root.unmount());
});

it("keeps checking after an applied status that still needs verification, and recovers from errors", async () => {
  const { apiClient, reads } = controllableClient();
  const root = createRoot(document.createElement("div"));
  await act(async () => root.render(<Probe apiClient={apiClient} />));
  await act(async () => reads[0].resolve(checking));
  await act(async () => vi.advanceTimersByTime(2_000));
  expect(reads).toHaveLength(2);

  const appliedChecking = { ...checking, selected_model_groups: [] };
  await act(async () => latest.onSettingsSetupReady(appliedChecking));
  await act(async () => reads[1].reject(new DOMException("aborted", "AbortError")));
  expect(latest.setupStatus).toBe(appliedChecking);

  await act(async () => vi.advanceTimersByTime(2_000));
  expect(reads).toHaveLength(3);
  await act(async () => reads[2].reject(new TypeError("Failed to fetch")));
  expect(latest.setupStatus).toBe(appliedChecking);
  await act(async () => vi.advanceTimersByTime(2_000));
  expect(reads).toHaveLength(4);
  await act(async () => reads[3].resolve(base));
  expect(latest.setupStatus).toBe(base);
  await act(async () => root.unmount());
});
