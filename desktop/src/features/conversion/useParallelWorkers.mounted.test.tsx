// @vitest-environment happy-dom

import { act, useRef } from "react";
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
  const persistedSettingsRef = useRef(persisted);
  const apiClient = useRef({} as never);
  const systemCapabilities = useRef(capabilities());
  current = useParallelWorkers({
    apiClient: apiClient.current,
    systemCapabilities: systemCapabilities.current,
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

  it("says how many of a larger request will run on this computer", () => {
    act(() => root.render(<Probe />));
    expect(current?.usableParallelWorkerLimit).toBe(1);
    expect(current?.parallelWorkerStatus).toBe("This computer has memory for one chapter at a time.");

    act(() => current!.setParallelManualMax("2"));
    expect(current?.selectedParallelWorkerLimit).toBe(2);
    expect(current?.usableParallelWorkerLimit).toBe(1);
    expect(current?.parallelWorkerStatus).toBe(
      "Runs 1 of the 2 requested: this computer has memory for one chapter at a time.",
    );
  });

  it.each([
    ["1.5", "1"], ["-3", "1"], ["2e0", "2"], ["0", "1"], ["999", "4"], ["invalid", "1"],
  ])("normalizes the numeric worker count %s without joining unrelated digits", (input, expected) => {
    act(() => root.render(<Probe />));
    act(() => current!.setParallelManualMax(input));
    expect(current?.parallelManualMax).toBe(expected);
    expect(current?.selectedParallelWorkerLimit).toBe(Number(expected));
  });
});

describe("useParallelWorkers hydration", () => {
  function Controlled({ apiClient, systemCapabilities, persisted }: {
    apiClient: object; systemCapabilities: SystemCapabilitiesResponse; persisted: { parallel_manual_max?: number };
  }) {
    const persistedSettingsRef = useRef(persisted);
    persistedSettingsRef.current = persisted;
    current = useParallelWorkers({ apiClient: apiClient as never, systemCapabilities, persistedSettingsRef });
    return null;
  }

  it("keeps a cleared field across capability polls and hydrates again for a new client", () => {
    const client = {};
    const persisted = { parallel_manual_max: 3 };
    act(() => root.render(<Controlled apiClient={client} systemCapabilities={capabilities()} persisted={persisted} />));
    expect(current?.parallelManualMax).toBe("3");
    act(() => current!.setParallelManualMax(""));
    // Saving the cleared field persists the clamped limit; a poll must not refill the input.
    const saved = { parallel_manual_max: 1 };
    act(() => root.render(<Controlled apiClient={client} systemCapabilities={capabilities()} persisted={saved} />));
    expect(current?.parallelManualMax).toBe("");
    // The recommendation itself still follows each poll.
    const fresher = { parallel_worker_recommendation: { ...recommendation, max_safe_parallel_chapters: 2 } } as unknown as SystemCapabilitiesResponse;
    act(() => root.render(<Controlled apiClient={client} systemCapabilities={fresher} persisted={saved} />));
    expect(current?.parallelRecommendation?.max_safe_parallel_chapters).toBe(2);
    expect(current?.parallelManualMax).toBe("");

    act(() => root.render(<Controlled apiClient={{}} systemCapabilities={capabilities()} persisted={{ parallel_manual_max: 4 }} />));
    expect(current?.parallelManualMax).toBe("4");
  });
});
