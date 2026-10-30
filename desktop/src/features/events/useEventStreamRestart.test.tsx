// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import type { ActiveConversionResponse } from "../../types/api";
import { useEventStream } from "./useEventStream";

class EventConnection {
  static connections: EventConnection[] = [];
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  close = vi.fn();
  constructor(readonly url: string) { EventConnection.connections.push(this); }
  send(payload: Record<string, unknown>) { this.onmessage?.({ data: JSON.stringify(payload) }); }
}

function active(id: string): ActiveConversionResponse {
  return { active: true, job: { id, status: "running", book_id: null, error: null, title: id, author: id } };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function progress(instance: string, sequence: number, jobId: string, pct: number) {
  return {
    event_type: "progress", backend_instance_id: instance, event_sequence: sequence,
    event_id: String(sequence), job_id: jobId, progress: pct,
  };
}

function identify(source: EventConnection, instance: string) {
  source.send({ event_type: "stream", backend_instance_id: instance });
}

describe("event streams across backend restarts", () => {
  let root: Root;
  let container: HTMLDivElement;
  let result: ReturnType<typeof useEventStream>;
  let getActiveConversion: ReturnType<typeof vi.fn<() => Promise<ActiveConversionResponse>>>;
  let client: ApiClient;

  function Probe() {
    result = useEventStream({ apiClient: client, baseUrl: "http://desktop.test" });
    return null;
  }

  const connection = () => EventConnection.connections.at(-1)!;
  const mount = () => act(async () => root.render(<Probe />));
  const reconnect = async () => {
    await act(async () => connection().onerror?.());
    await act(async () => vi.advanceTimersByTimeAsync(500));
    await act(async () => connection().onopen?.());
    return connection();
  };

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.useFakeTimers();
    vi.spyOn(Math, "random").mockReturnValue(1);
    vi.stubGlobal("EventSource", EventConnection);
    EventConnection.connections = [];
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    getActiveConversion = vi.fn().mockResolvedValue({ active: false, job: null });
    client = { getActiveConversion, usesDesktopAuth: false } as unknown as ApiClient;
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("accepts the restarted sequence and new job before reconnect reconciliation resolves", async () => {
    getActiveConversion.mockResolvedValue(active("old-job"));
    await mount();
    await act(async () => connection().send(progress("old-instance", 90, "old-job", 80)));
    const pending = deferred<ActiveConversionResponse>();
    getActiveConversion.mockReturnValue(pending.promise);
    const next = await reconnect();
    await act(async () => {
      identify(next, "new-instance");
      next.send(progress("new-instance", 1, "new-job", 12));
    });
    expect(result.activeJobId).toBe("new-job");
    expect(result.progressPct).toBe(12);
    await act(async () => pending.resolve({ active: false, job: null }));
    expect(result.activeJobId).toBe("new-job");
  });

  it("clears old progress on an idle restart and fences the previous snapshot", async () => {
    await mount();
    await act(async () => connection().send(progress("old-instance", 90, "old-job", 80)));
    const pending = deferred<ActiveConversionResponse>();
    getActiveConversion.mockReturnValueOnce(pending.promise)
      .mockResolvedValue({ active: false, job: null });
    const next = await reconnect();
    await act(async () => identify(next, "new-instance"));
    expect(result.activeJobId).toBeNull();
    expect(result.progressPct).toBe(0);
    await act(async () => pending.resolve(active("old-job")));
    expect(result.activeJobId).toBeNull();
  });

  it("ignores duplicate and older sequences while retaining the cursor on same-instance reconnects", async () => {
    getActiveConversion.mockResolvedValue(active("job"));
    await mount();
    await act(async () => connection().send(progress("instance", 9, "job", 70)));
    const next = await reconnect();
    await act(async () => {
      identify(next, "instance");
      next.send(progress("instance", 9, "job", 20));
      next.send(progress("instance", 8, "job", 10));
    });
    expect(result.progressPct).toBe(70);
    await act(async () => next.send(progress("instance", 10, "job", 90)));
    expect(result.progressPct).toBe(90);
  });

  it("ignores callbacks from a closed connection after its replacement opens", async () => {
    await mount();
    const old = connection();
    const next = await reconnect();
    const lookups = getActiveConversion.mock.calls.length;
    await act(async () => {
      old.send({ event_type: "progress", job_id: "old-job", progress: 80 });
      old.onopen?.();
      old.onerror?.();
    });
    expect(result.activeJobId).toBeNull();
    expect(result.streamState).toBe("open");
    expect(getActiveConversion).toHaveBeenCalledTimes(lookups);
    expect(next.close).not.toHaveBeenCalled();
  });

  it("does not return to a retired backend instance", async () => {
    await mount();
    await act(async () => connection().send(progress("old-instance", 90, "old-job", 80)));
    await reconnect();
    await act(async () => identify(connection(), "new-instance"));
    await act(async () => connection().send(progress("old-instance", 91, "old-job", 90)));
    expect(result.activeJobId).toBeNull();
  });

  it("accepts legacy events when their event ids restart", async () => {
    getActiveConversion.mockResolvedValue(active("job"));
    await mount();
    await act(async () => connection().send({ event_type: "progress", event_id: "99", job_id: "job", progress: 60 }));
    await reconnect();
    await act(async () => connection().send({ event_type: "progress", event_id: "1", job_id: "job", progress: 70 }));
    expect(result.progressPct).toBe(70);
  });
});
