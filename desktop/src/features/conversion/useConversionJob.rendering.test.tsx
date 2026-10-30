// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import type { ActiveConversionResponse } from "../../types/api";
import { useConversionJob } from "./useConversionJob";

const stream = vi.hoisted(() => ({ refreshActiveConversionState: vi.fn() }));
vi.mock("../events/useEventStream", async () => {
  const { useMemo } = await import("react");
  return {
    useEventStream: () => useMemo(() => ({
      activeJobId: "job-1", uiState: "running", refreshActiveConversionState: stream.refreshActiveConversionState,
      markConversionStarted: vi.fn(),
    }), []),
  };
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

it("keeps the job controller when a reconcile poll repeats the same job snapshot", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const snapshot = (): ActiveConversionResponse => ({
    active: true,
    job: { id: "job-1", status: "running", title: "Moby Dick", author: "Melville", book_id: null, client_request_id: null, error: null },
  } as ActiveConversionResponse);
  stream.refreshActiveConversionState.mockImplementation(async () => snapshot());
  const apiClient = {} as ApiClient;
  let renders = 0;
  let controller!: ReturnType<typeof useConversionJob>;
  function Probe() {
    renders += 1;
    controller = useConversionJob({ apiClient, baseUrl: "http://desktop.test" });
    return null;
  }
  const root = createRoot(document.createElement("div"));
  try {
    await act(async () => root.render(<Probe />));
    await act(async () => { await controller.refreshActiveConversion(); });
    expect(controller.activeJobTitle).toBe("Moby Dick");
    const settled = controller;
    const rendersBefore = renders;

    // While a job runs, the event stream reconciles every two seconds.
    for (let poll = 0; poll < 5; poll += 1) {
      await act(async () => { await controller.refreshActiveConversion(); });
    }
    // React may run one bailout render for the first identical update; the
    // controller (and so every consumer) must stay the same object.
    expect(renders - rendersBefore).toBeLessThanOrEqual(1);
    expect(controller).toBe(settled);
  } finally {
    await act(async () => root.unmount());
  }
});
