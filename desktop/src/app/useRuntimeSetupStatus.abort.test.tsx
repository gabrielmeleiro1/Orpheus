// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import type { ApiClient } from "../api/client";
import { useRuntimeSetupStatus } from "./useRuntimeSetupStatus";

afterEach(() => vi.unstubAllGlobals());

function stalledClient() {
  const signals: AbortSignal[] = [];
  const apiClient = {
    getRuntimeSetupStatus: vi.fn((options?: { signal?: AbortSignal }) => {
      if (options?.signal) signals.push(options.signal);
      return new Promise(() => undefined);
    }),
  } as unknown as ApiClient;
  return { apiClient, signals };
}

it("aborts an outstanding setup status read when its client is replaced or unmounted", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const first = stalledClient();
  const second = stalledClient();
  function Probe({ apiClient }: { apiClient: ApiClient }) {
    useRuntimeSetupStatus(apiClient);
    return null;
  }
  const root = createRoot(document.createElement("div"));
  await act(async () => root.render(<Probe apiClient={first.apiClient} />));
  expect(first.signals).toHaveLength(1);
  expect(first.signals[0].aborted).toBe(false);

  // A replacement backend must not leave the old read open for its 120 s budget.
  await act(async () => root.render(<Probe apiClient={second.apiClient} />));
  expect(first.signals[0].aborted).toBe(true);
  expect(second.signals[0].aborted).toBe(false);

  await act(async () => root.unmount());
  expect(second.signals[0].aborted).toBe(true);
});
