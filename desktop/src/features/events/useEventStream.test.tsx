// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import type { ActiveConversionResponse, DesktopEventPayload } from "../../types/api";
import { useEventStream } from "./useEventStream";

const transportHarness = vi.hoisted(() => ({
  onPayload: null as ((payload: DesktopEventPayload) => void) | null,
}));

vi.mock("./useEventStreamTransport", () => ({
  useEventStreamTransport: (args: { onPayload: (payload: DesktopEventPayload) => void }) => {
    transportHarness.onPayload = args.onPayload;
    return "open";
  },
}));

function Probe(): null {
  const result = useEventStream({ apiClient: (globalThis as typeof globalThis & { eventApi: ApiClient }).eventApi, baseUrl: "http://desktop.test" });
  (globalThis as typeof globalThis & { eventProbe?: typeof result }).eventProbe = result;
  return null;
}

function getProbe(): ReturnType<typeof useEventStream> {
  return (globalThis as typeof globalThis & { eventProbe: ReturnType<typeof useEventStream> }).eventProbe;
}

function activeResponse(id: string): ActiveConversionResponse {
  return {
    active: true,
    job: {
      id,
      book_id: null,
      status: "running",
      error: null,
      title: id,
      author: id,
    },
  };
}

describe("useEventStream reconciliation fencing", () => {
  let root: Root;
  let container: HTMLDivElement;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
    transportHarness.onPayload = null;
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    delete (globalThis as typeof globalThis & { eventProbe?: unknown }).eventProbe;
    delete (globalThis as typeof globalThis & { eventApi?: unknown }).eventApi;
    vi.clearAllMocks();
  });

  it("does not let an older inactive lookup clear a job marked started", async () => {
    let resolveLookup!: (response: ActiveConversionResponse) => void;
    const getActiveConversion = vi.fn(
      () => new Promise<ActiveConversionResponse>((resolve) => { resolveLookup = resolve; }),
    );
    (globalThis as typeof globalThis & { eventApi: ApiClient }).eventApi = {
      getActiveConversion,
    } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<Probe />);
    });

    await act(async () => {
      getProbe().markConversionStarted("job-b");
    });
    expect(getProbe().activeJobId).toBe("job-b");

    await act(async () => {
      resolveLookup({ active: false, job: null });
    });

    expect(getProbe().activeJobId).toBe("job-b");
  });

  it("does not let an older lookup replace a job announced by an authoritative event", async () => {
    let resolveLookup!: (response: ActiveConversionResponse) => void;
    const getActiveConversion = vi.fn(
      () => new Promise<ActiveConversionResponse>((resolve) => { resolveLookup = resolve; }),
    );
    (globalThis as typeof globalThis & { eventApi: ApiClient }).eventApi = {
      getActiveConversion,
    } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<Probe />);
    });

    await act(async () => {
      transportHarness.onPayload?.({
        event_type: "state",
        job_id: "job-b",
        state: "running",
        message: "running",
      });
    });
    expect(getProbe().activeJobId).toBe("job-b");

    await act(async () => {
      resolveLookup(activeResponse("job-a"));
    });

    expect(getProbe().activeJobId).toBe("job-b");
  });

  it("does not let an older overlapping lookup overwrite a newer response", async () => {
    const resolvers: Array<(response: ActiveConversionResponse) => void> = [];
    const getActiveConversion = vi.fn(
      () => new Promise<ActiveConversionResponse>((resolve) => { resolvers.push(resolve); }),
    );
    (globalThis as typeof globalThis & { eventApi: ApiClient }).eventApi = {
      getActiveConversion,
    } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<Probe />);
    });
    const newerLookup = getProbe().refreshActiveConversionState();

    await act(async () => {
      resolvers[1]?.(activeResponse("job-b"));
      await newerLookup;
    });
    expect(getProbe().activeJobId).toBe("job-b");

    await act(async () => {
      resolvers[0]?.({ active: false, job: null });
    });
    expect(getProbe().activeJobId).toBe("job-b");
  });

  it("lets the latest lookup win when an older lookup resolves first", async () => {
    const resolvers: Array<(response: ActiveConversionResponse) => void> = [];
    const getActiveConversion = vi.fn(() => new Promise<ActiveConversionResponse>((resolve) => { resolvers.push(resolve); }));
    (globalThis as typeof globalThis & { eventApi: ApiClient }).eventApi = { getActiveConversion } as unknown as ApiClient;
    await act(async () => { root = createRoot(container); root.render(<Probe />); });
    const newer = getProbe().refreshActiveConversionState();
    await act(async () => { resolvers[0]?.({ active: false, job: null }); });
    await act(async () => { resolvers[1]?.(activeResponse("job-b")); await newer; });
    expect(getProbe().activeJobId).toBe("job-b");
  });

  it("aborts a cancellation lookup superseded by a newer job", async () => {
    let resolveCancelLookup!: (response: ActiveConversionResponse) => void;
    let lookupCount = 0;
    const getActiveConversion = vi.fn(() => {
      lookupCount += 1;
      if (lookupCount === 1) {
        return Promise.resolve({ active: false, job: null } as ActiveConversionResponse);
      }
      return new Promise<ActiveConversionResponse>((resolve) => { resolveCancelLookup = resolve; });
    });
    const cancelConversion = vi.fn(async () => ({ cancelled: true }));
    (globalThis as typeof globalThis & { eventApi: ApiClient }).eventApi = {
      getActiveConversion,
      cancelConversion,
    } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<Probe />);
    });
    const cancellation = getProbe().cancelActiveConversion();
    await vi.waitFor(() => expect(getActiveConversion).toHaveBeenCalledTimes(2));

    await act(async () => {
      getProbe().markConversionStarted("job-b");
      resolveCancelLookup(activeResponse("job-a"));
      await cancellation;
    });

    expect(cancelConversion).not.toHaveBeenCalled();
    expect(getProbe().activeJobId).toBe("job-b");
    expect(getProbe().isCancelling).toBe(false);
  });

  it("ignores late state from an older job after a newer job starts", async () => {
    const getActiveConversion = vi.fn(async () => ({ active: false, job: null }));
    (globalThis as typeof globalThis & { eventApi: ApiClient }).eventApi = {
      getActiveConversion,
    } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<Probe />);
    });
    await act(async () => {
      getProbe().markConversionStarted("job-b");
      transportHarness.onPayload?.({
        event_type: "state",
        job_id: "job-a",
        state: "completed",
        message: "old job completed",
      });
      transportHarness.onPayload?.({
        event_type: "state",
        job_id: "job-a",
        state: "running",
        message: "old job resumed",
      });
      transportHarness.onPayload?.({
        event_type: "progress",
        job_id: "job-a",
        progress: 90,
        message: "old job progress",
      });
    });

    expect(getProbe().activeJobId).toBe("job-b");
    expect(getProbe().progressPct).toBe(0);
  });

  it("releases ownership after terminal state so the next job can run", async () => {
    const getActiveConversion = vi.fn(async () => ({ active: false, job: null }));
    (globalThis as typeof globalThis & { eventApi: ApiClient }).eventApi = { getActiveConversion } as unknown as ApiClient;
    await act(async () => {
      root = createRoot(container);
      root.render(<Probe />);
    });
    await act(async () => {
      getProbe().markConversionStarted("job-b");
      transportHarness.onPayload?.({ event_type: "state", job_id: "job-b", state: "completed" });
      transportHarness.onPayload?.({ event_type: "state", job_id: "job-c", state: "running" });
    });
    expect(getProbe().activeJobId).toBe("job-c");
    await act(async () => {
      transportHarness.onPayload?.({ event_type: "state", job_id: "job-b", state: "running" });
      transportHarness.onPayload?.({ event_type: "progress", job_id: "job-b", progress: 90 });
    });
    expect(getProbe().activeJobId).toBe("job-c");
  });

  it("does not supersede a pending cancel with a same-job refresh", async () => {
    let resolveCancel!: (response: { cancelled: boolean }) => void;
    const cancelConversion = vi.fn(() => new Promise<{ cancelled: boolean }>((resolve) => { resolveCancel = resolve; }));
    const getActiveConversion = vi.fn(async () => ({ active: true, job: { id: "job-b", status: "running" } }));
    (globalThis as typeof globalThis & { eventApi: ApiClient }).eventApi = { getActiveConversion, cancelConversion } as unknown as ApiClient;
    await act(async () => { root = createRoot(container); root.render(<Probe />); });
    await act(async () => { getProbe().markConversionStarted("job-b"); });
    const cancellation = getProbe().cancelActiveConversion();
    await vi.waitFor(() => expect(cancelConversion).toHaveBeenCalledWith("job-b"));
    await act(async () => { await getProbe().refreshActiveConversionState(); });
    await act(async () => { resolveCancel({ cancelled: true }); await cancellation; });
    expect(getProbe().isCancelling).toBe(true);
  });

  it("does not relatch a target cleared by an inactive refresh", async () => {
    let resolveCancel!: (response: { cancelled: boolean }) => void;
    let active = true;
    const cancelConversion = vi.fn(() => new Promise<{ cancelled: boolean }>((resolve) => { resolveCancel = resolve; }));
    const getActiveConversion = vi.fn(async () => active
      ? { active: true, job: { id: "job-b", status: "running" } }
      : { active: false, job: null });
    (globalThis as typeof globalThis & { eventApi: ApiClient }).eventApi = { getActiveConversion, cancelConversion } as unknown as ApiClient;
    await act(async () => { root = createRoot(container); root.render(<Probe />); });
    await act(async () => { getProbe().markConversionStarted("job-b"); });
    const cancellation = getProbe().cancelActiveConversion();
    await vi.waitFor(() => expect(cancelConversion).toHaveBeenCalledWith("job-b"));
    active = false;
    await act(async () => { await getProbe().refreshActiveConversionState(); });
    await act(async () => { resolveCancel({ cancelled: true }); await cancellation; });
    expect(getProbe().activeJobId).toBeNull();
    expect(getProbe().isCancelling).toBe(false);
  });

  it("ignores a stale cancel rejection after a newer cancellation starts", async () => {
    let rejectOld!: (error: Error) => void;
    const cancelConversion = vi.fn()
      .mockImplementationOnce(() => new Promise((_, reject) => { rejectOld = reject; }))
      .mockResolvedValueOnce({ cancelled: true });
    const getActiveConversion = vi.fn(async () => ({ active: false, job: null }));
    (globalThis as typeof globalThis & { eventApi: ApiClient }).eventApi = { getActiveConversion, cancelConversion } as unknown as ApiClient;
    await act(async () => { root = createRoot(container); root.render(<Probe />); });
    await act(async () => { getProbe().markConversionStarted("job-a"); });
    const oldCancel = getProbe().cancelActiveConversion();
    await vi.waitFor(() => expect(cancelConversion).toHaveBeenCalledWith("job-a"));
    await act(async () => { getProbe().markConversionStarted("job-b"); });
    await getProbe().cancelActiveConversion();
    await act(async () => { rejectOld(new Error("old failure")); await oldCancel; });
    expect(getProbe().activeJobId).toBe("job-b");
    expect(getProbe().isCancelling).toBe(true);
    expect(getProbe().logs.some((entry) => entry.msg.includes("old failure"))).toBe(false);
  });

  it("lets only the latest same-job cancellation response mutate state", async () => {
    const resolvers: Array<(response: { cancelled: boolean }) => void> = [];
    const cancelConversion = vi.fn(() => new Promise<{ cancelled: boolean }>((resolve) => { resolvers.push(resolve); }));
    const getActiveConversion = vi.fn(async () => ({ active: false, job: null }));
    (globalThis as typeof globalThis & { eventApi: ApiClient }).eventApi = { getActiveConversion, cancelConversion } as unknown as ApiClient;
    await act(async () => { root = createRoot(container); root.render(<Probe />); });
    await act(async () => { getProbe().markConversionStarted("job-b"); });
    const older = getProbe().cancelActiveConversion();
    const newer = getProbe().cancelActiveConversion();
    await vi.waitFor(() => expect(cancelConversion).toHaveBeenCalledTimes(2));
    await act(async () => { resolvers[1]?.({ cancelled: false }); await newer; });
    await act(async () => { resolvers[0]?.({ cancelled: true }); await older; });
    expect(getProbe().isCancelling).toBe(false);
    expect(getProbe().logs.some((entry) => entry.msg.includes("cancellation requested"))).toBe(false);
  });

  it("lets only the latest fallback cancellation lookup claim a job", async () => {
    const resolvers: Array<(response: ActiveConversionResponse) => void> = [];
    let calls = 0;
    const getActiveConversion = vi.fn(() => {
      calls += 1;
      if (calls === 1) return Promise.resolve({ active: false, job: null } as ActiveConversionResponse);
      return new Promise<ActiveConversionResponse>((resolve) => { resolvers.push(resolve); });
    });
    const cancelConversion = vi.fn(async () => ({ cancelled: true }));
    (globalThis as typeof globalThis & { eventApi: ApiClient }).eventApi = { getActiveConversion, cancelConversion } as unknown as ApiClient;
    await act(async () => { root = createRoot(container); root.render(<Probe />); });
    const older = getProbe().cancelActiveConversion();
    const newer = getProbe().cancelActiveConversion();
    await vi.waitFor(() => expect(resolvers).toHaveLength(2));
    await act(async () => { resolvers[0]?.(activeResponse("job-a")); await older; });
    expect(cancelConversion).not.toHaveBeenCalled();
    await act(async () => { resolvers[1]?.(activeResponse("job-b")); await newer; });
    expect(cancelConversion).toHaveBeenCalledWith("job-b");
  });
});
