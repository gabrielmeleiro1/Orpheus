// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SystemCapabilitiesResponse } from "../../types/api";
import { useParallelWorkers } from "./useParallelWorkers";

const recommendation = {
  memory_measurement_available: true,
  total_memory_bytes: 8 * 1024 ** 3,
  available_memory_bytes: 3.9 * 1024 ** 3,
  reserved_memory_bytes: 1 * 1024 ** 3,
  headroom_memory_bytes: 2.9 * 1024 ** 3,
  static_estimated_worker_bytes: 4 * 1024 ** 3,
  warmup_memory_delta_bytes: null,
  estimated_per_worker_memory_bytes: 4 * 1024 ** 3,
  requested_parallel_chapters: 4,
  max_safe_parallel_chapters: 1,
  recommended_parallel_chapters: 1,
  reason: "memory pressure",
  calculation: "available memory supports one worker",
};

function capabilities(): SystemCapabilitiesResponse {
  return { parallel_worker_recommendation: recommendation } as unknown as SystemCapabilitiesResponse;
}

let container: HTMLDivElement;
let root: Root;
let current: ReturnType<typeof useParallelWorkers> | null = null;

function Probe({ persisted = {} }: { persisted?: { parallel_manual_max?: number } }) {
  const persistedSettingsRef = { current: persisted };
  current = useParallelWorkers({
    apiClient: {} as never,
    systemCapabilities: capabilities(),
    persistedSettingsRef,
  });
  return null;
}

beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  container = document.createElement("div");
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  current = null;
});

describe("useParallelWorkers recommendation defaults", () => {
  it("selects the measured safe recommendation instead of the requested cap", () => {
    act(() => root.render(<Probe />));
    expect(current?.parallelManualMax).toBe("1");
    expect(current?.selectedParallelWorkerLimit).toBe(1);
    expect(current?.manualParallelWorkerUpperBound).toBe(4);
  });

  it("preserves an explicit persisted worker choice", () => {
    act(() => root.render(<Probe persisted={{ parallel_manual_max: 3 }} />));
    expect(current?.parallelManualMax).toBe("3");
    expect(current?.selectedParallelWorkerLimit).toBe(3);
  });
});
