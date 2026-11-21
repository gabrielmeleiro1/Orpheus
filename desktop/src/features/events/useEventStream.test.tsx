// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient, type ApiClient } from "../../api/client";
import type { ActiveConversionResponse, DesktopEventPayload } from "../../types/api";
import { useConversionJob } from "../conversion/useConversionJob";
import { useEventStream } from "./useEventStream";

const transport = vi.hoisted(() => ({
  onOpen: null as null | (() => void),
  onPayload: null as null | ((payload: DesktopEventPayload) => void),
}));

vi.mock("./useEventStreamTransport", () => ({
  useEventStreamTransport: ({ onOpen, onPayload }: {
    onOpen: () => void;
    onPayload: (payload: DesktopEventPayload) => void;
  }) => {
    transport.onOpen = onOpen;
    transport.onPayload = onPayload;
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
      repair: false,
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
    transport.onOpen = null;
    transport.onPayload = null;
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    delete (globalThis as typeof globalThis & { eventProbe?: unknown }).eventProbe;
    delete (globalThis as typeof globalThis & { eventApi?: unknown }).eventApi;
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("releases a stalled reconciliation so the next poll can discover completion", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify(activeResponse("job-a"))))
      .mockImplementationOnce((_input, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    }))
      .mockImplementation(async () => new Response(JSON.stringify({ active: false, job: { ...activeResponse("job-a").job!, status: "completed" } })));
    (globalThis as typeof globalThis & { eventApi: ApiClient }).eventApi = createApiClient({ baseUrl: "http://desktop.test" });
    await act(async () => { root = createRoot(container); root.render(<Probe />); });
    await act(async () => vi.advanceTimersByTimeAsync(2000));
    await act(async () => vi.advanceTimersByTimeAsync(8000));
    expect(getProbe().logs.some((log) => log.msg === "active conversion lookup failed")).toBe(true);
    await act(async () => vi.advanceTimersByTimeAsync(2000));
    expect(getProbe().uiState).toBe("completed");
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("accepts a slow cancellation snapshot without superseding it with another poll", async () => {
    vi.useFakeTimers();
    const running = activeResponse("job-a");
    const getActiveConversion = vi.fn().mockResolvedValueOnce(running)
      .mockImplementation(() => new Promise<ActiveConversionResponse>((resolve) => {
        setTimeout(() => resolve({ active: false, job: { ...running.job!, status: "cancelled" } }), 3000);
      }));
    const cancelConversion = vi.fn().mockResolvedValue({ cancelled: true });
    (globalThis as typeof globalThis & { eventApi: ApiClient }).eventApi = { getActiveConversion, cancelConversion } as unknown as ApiClient;
    await act(async () => { root = createRoot(container); root.render(<Probe />); });
    await act(async () => { await getProbe().cancelActiveConversion(); });

    await act(async () => vi.advanceTimersByTimeAsync(5000));

    expect(getProbe().uiState).toBe("cancelled");
    expect(getProbe().isCancelling).toBe(false);
    expect(getActiveConversion).toHaveBeenCalledTimes(2);
  });

  it("accepts an authoritative terminal snapshot after a different job was last observed", async () => {
    const getActiveConversion = vi.fn().mockResolvedValueOnce(activeResponse("job-a"))
      .mockResolvedValue({ active: false, job: { ...activeResponse("job-b").job!, status: "completed" } });
    (globalThis as typeof globalThis & { eventApi: ApiClient }).eventApi = { getActiveConversion } as unknown as ApiClient;
    await act(async () => { root = createRoot(container); root.render(<Probe />); });

    await act(async () => { await getProbe().refreshActiveConversionState(); });

    expect(getProbe().uiState).toBe("completed");
    expect(getProbe().activeJobId).toBeNull();
  });

  it("releases cancellation when its job lookup stalls without sending a cancel", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({ active: false, job: null })))
      .mockImplementation((_input, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    }));
    (globalThis as typeof globalThis & { eventApi: ApiClient }).eventApi = createApiClient({ baseUrl: "http://desktop.test" });
    await act(async () => { root = createRoot(container); root.render(<Probe />); });
    await act(async () => { void getProbe().cancelActiveConversion(); });
    expect(getProbe().isCancelling).toBe(true);
    await act(async () => vi.advanceTimersByTimeAsync(8000));
    expect(getProbe().isCancelling).toBe(false);
    expect(getProbe().logs.some((log) => log.msg === "request timed out")).toBe(true);
    expect(fetchMock.mock.calls.every(([, init]) => init?.method !== "POST")).toBe(true);
  });

  it("closes the startup snapshot gap on the first stream open", async () => {
    const running = activeResponse("job-a");
    const getActiveConversion = vi.fn().mockResolvedValueOnce(running)
      .mockResolvedValue({ active: false, job: { ...running.job!, status: "completed" } });
    (globalThis as typeof globalThis & { eventApi: ApiClient }).eventApi = { getActiveConversion } as unknown as ApiClient;
    await act(async () => { root = createRoot(container); root.render(<Probe />); });
    expect(getProbe().uiState).toBe("running");
    await act(async () => transport.onOpen?.());
    expect(getProbe().uiState).toBe("completed");
    expect(getProbe().activeJobId).toBeNull();
  });

  it("keeps job discovery valid when a log arrives during the lookup", async () => {
    const lookup = deferred<ActiveConversionResponse>();
    (globalThis as typeof globalThis & { eventApi: ApiClient }).eventApi = {
      getActiveConversion: vi.fn(() => lookup.promise),
    } as unknown as ApiClient;
    await act(async () => { root = createRoot(container); root.render(<Probe />); });
    await act(async () => transport.onPayload?.({ event_type: "log", job_id: "job-a", message: "Loading model" }));
    await act(async () => lookup.resolve(activeResponse("job-a")));
    expect(getProbe().activeJobId).toBe("job-a");
  });

  it("acquires the active job identity from progress before discovery resolves", async () => {
    const lookup = deferred<ActiveConversionResponse>();
    (globalThis as typeof globalThis & { eventApi: ApiClient }).eventApi = {
      getActiveConversion: vi.fn(() => lookup.promise),
    } as unknown as ApiClient;
    await act(async () => { root = createRoot(container); root.render(<Probe />); });
    await act(async () => transport.onPayload?.({ event_type: "progress", job_id: "job-a", progress: 42 }));
    await act(async () => lookup.resolve({ active: false, job: null }));
    expect(getProbe().uiState).toBe("running");
    expect(getProbe().activeJobId).toBe("job-a");
    expect(getProbe().progressPct).toBe(42);
  });

  it.each(["recovered", "started"])("keeps reconciling a %s job after its waiter returns unknown", async (origin) => {
    vi.useFakeTimers();
    const running = activeResponse("job-a");
    const getActiveConversion = vi.fn().mockResolvedValueOnce(origin === "recovered" ? running : { active: false, job: null })
      .mockRejectedValue(new Error("Backend unavailable"));
    (globalThis as typeof globalThis & { eventApi: ApiClient }).eventApi = { getActiveConversion } as unknown as ApiClient;
    await act(async () => { root = createRoot(container); root.render(<Probe />); });
    if (origin === "started") await act(async () => getProbe().markConversionStarted("job-a"));
    let terminal!: ReturnType<ReturnType<typeof useEventStream>["waitForTerminalState"]>;
    await act(async () => { terminal = getProbe().waitForTerminalState("job-a"); });
    for (let poll = 0; poll < 4; poll += 1) {
      await act(async () => vi.advanceTimersByTimeAsync(1500));
    }
    expect((await terminal).status).toBe("unknown");
    getActiveConversion.mockResolvedValue({ active: false, job: { ...running.job!, status: "completed" } });
    await act(async () => vi.advanceTimersByTimeAsync(2000));
    expect(getProbe().uiState).toBe("completed");
    expect(getProbe().activeJobId).toBeNull();
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
      transport.onPayload?.({
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

  it("retires an in-flight cancellation lookup when its backend client is replaced", async () => {
    const lookup = deferred<ActiveConversionResponse>();
    const oldClient = {
      getActiveConversion: vi.fn().mockResolvedValueOnce({ active: false, job: null })
        .mockReturnValue(lookup.promise),
      cancelConversion: vi.fn().mockResolvedValue({ cancelled: true }),
    } as unknown as ApiClient;
    (globalThis as typeof globalThis & { eventApi: ApiClient }).eventApi = oldClient;
    await act(async () => { root = createRoot(container); root.render(<Probe />); });
    let cancellation!: Promise<void>;
    await act(async () => { cancellation = getProbe().cancelActiveConversion(); });
    expect(getProbe().isCancelling).toBe(true);

    (globalThis as typeof globalThis & { eventApi: ApiClient }).eventApi = {
      getActiveConversion: vi.fn().mockRejectedValue(new Error("new backend is unavailable")),
    } as unknown as ApiClient;
    await act(async () => root.render(<Probe />));
    await act(async () => { lookup.resolve(activeResponse("old-job")); await cancellation; });

    expect(oldClient.cancelConversion).not.toHaveBeenCalled();
    expect(getProbe().activeJobId).toBeNull();
    expect(getProbe().isCancelling).toBe(false);
  });

  it("ignores a retained cancellation callback from a replaced backend", async () => {
    const oldClient = {
      getActiveConversion: vi.fn().mockResolvedValue(activeResponse("old-job")),
      cancelConversion: vi.fn().mockResolvedValue({ cancelled: true }),
    } as unknown as ApiClient;
    (globalThis as typeof globalThis & { eventApi: ApiClient }).eventApi = oldClient;
    await act(async () => { root = createRoot(container); root.render(<Probe />); });
    const staleCancel = getProbe().cancelActiveConversion;
    (globalThis as typeof globalThis & { eventApi: ApiClient }).eventApi = {
      getActiveConversion: vi.fn().mockResolvedValue(activeResponse("new-job")),
    } as unknown as ApiClient;
    await act(async () => root.render(<Probe />));
    await act(async () => staleCancel());

    expect(oldClient.cancelConversion).not.toHaveBeenCalled();
    expect(getProbe().activeJobId).toBe("new-job");
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
      transport.onPayload?.({
        event_type: "state",
        job_id: "job-a",
        state: "completed",
        message: "old job completed",
      });
      transport.onPayload?.({
        event_type: "state",
        job_id: "job-a",
        state: "running",
        message: "old job resumed",
      });
      transport.onPayload?.({
        event_type: "progress",
        job_id: "job-a",
        progress: 90,
        message: "old job progress",
      });
    });

    expect(getProbe().activeJobId).toBe("job-b");
    expect(getProbe().progressPct).toBe(0);
  });

  it("ignores stale logs from an older job but keeps current and terminal logs", async () => {
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
      transport.onPayload?.({
        event_type: "log",
        job_id: "job-a",
        level: "info",
        message: "old job log",
      });
      transport.onPayload?.({
        event_type: "log",
        job_id: "job-b",
        level: "info",
        message: "current job log",
      });
    });

    expect(getProbe().logs.some((entry) => entry.msg === "old job log")).toBe(false);
    expect(getProbe().logs.some((entry) => entry.msg === "current job log")).toBe(true);

    await act(async () => {
      transport.onPayload?.({
        event_type: "state",
        job_id: "job-b",
        state: "completed",
      });
      transport.onPayload?.({
        event_type: "log",
        job_id: "job-b",
        level: "info",
        message: "terminal summary",
      });
    });

    expect(getProbe().activeJobId).toBeNull();
    expect(getProbe().logs.some((entry) => entry.msg === "terminal summary")).toBe(true);
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
      transport.onPayload?.({ event_type: "state", job_id: "job-b", state: "completed" });
      transport.onPayload?.({ event_type: "state", job_id: "job-c", state: "running" });
    });
    expect(getProbe().activeJobId).toBe("job-c");
    await act(async () => {
      transport.onPayload?.({ event_type: "state", job_id: "job-b", state: "running" });
      transport.onPayload?.({ event_type: "progress", job_id: "job-b", progress: 90 });
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

  it("clears the old cancellation latch as soon as a newer conversion starts", async () => {
    let rejectOld!: (error: Error) => void;
    const cancelConversion = vi.fn(() => new Promise((_, reject) => { rejectOld = reject; }));
    const getActiveConversion = vi.fn(async () => ({ active: false, job: null }));
    (globalThis as typeof globalThis & { eventApi: ApiClient }).eventApi = { getActiveConversion, cancelConversion } as unknown as ApiClient;
    await act(async () => { root = createRoot(container); root.render(<Probe />); });
    await act(async () => { getProbe().markConversionStarted("job-a"); });
    const oldCancel = getProbe().cancelActiveConversion();
    await vi.waitFor(() => expect(cancelConversion).toHaveBeenCalledWith("job-a"));
    await act(async () => { getProbe().markConversionStarted("job-b"); });
    expect(getProbe().activeJobId).toBe("job-b");
    expect(getProbe().isCancelling).toBe(false);
    await act(async () => { rejectOld(new Error("old failure")); await oldCancel; });
    expect(getProbe().isCancelling).toBe(false);
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
    expect(getProbe().isCancelling).toBe(true);
    expect(getProbe().logs.some((entry) => entry.msg.includes("superseded"))).toBe(false);
    await act(async () => { resolvers[1]?.(activeResponse("job-b")); await newer; });
    expect(cancelConversion).toHaveBeenCalledWith("job-b");
  });
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((promiseResolve) => { resolve = promiseResolve; });
  return { promise, resolve };
}

function ConversionProbe({ client }: { client: ApiClient }): null {
  useConversionJob({ apiClient: client, baseUrl: "http://desktop.test" });
  return null;
}

function StreamProbe({ client }: { client: ApiClient }): null {
  const stream = useEventStream({ apiClient: client, baseUrl: "http://desktop.test" });
  (globalThis as typeof globalThis & { streamProbe?: typeof stream }).streamProbe = stream;
  return null;
}

describe("useEventStream startup reconciliation", () => {
  let root: Root;
  let container: HTMLDivElement;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
    transport.onOpen = null;
    transport.onPayload = null;
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.clearAllMocks();
    delete (globalThis as typeof globalThis & { streamProbe?: unknown }).streamProbe;
  });

  it("reconciles at startup and on every stream open", async () => {
    const getActiveConversion = vi.fn(async () => ({ active: false, job: null }));
    const client = { getActiveConversion } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<ConversionProbe client={client} />);
    });
    expect(getActiveConversion).toHaveBeenCalledTimes(1);

    await act(async () => transport.onOpen?.());
    expect(getActiveConversion).toHaveBeenCalledTimes(2);

    await act(async () => transport.onOpen?.());
    expect(getActiveConversion).toHaveBeenCalledTimes(3);
  });

  it("does not let a late reconnect refresh overwrite a newer stream event", async () => {
    const request = deferred<ActiveConversionResponse>();
    const client = { getActiveConversion: vi.fn(() => request.promise) } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<StreamProbe client={client} />);
    });
    await act(async () => transport.onOpen?.());
    await act(async () => transport.onOpen?.());

    await act(async () => transport.onPayload?.({
      event_type: "state",
      job_id: "new-job",
      state: "running",
      message: "new job started",
    }));
    request.resolve({
      active: true,
      job: { id: "old-job", status: "running" },
    } as ActiveConversionResponse);
    await act(async () => { await request.promise; });

    expect((globalThis as typeof globalThis & {
      streamProbe: { activeJobId: string | null };
    }).streamProbe.activeJobId).toBe("new-job");
  });
});
