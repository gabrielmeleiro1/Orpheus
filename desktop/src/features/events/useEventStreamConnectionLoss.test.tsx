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

const running: ActiveConversionResponse = {
  active: true, job: { id: "job", status: "running", book_id: null, error: null, title: "Book", author: null, repair: false },
};
const progress = (instance: string, sequence: number) => ({
  event_type: "progress", backend_instance_id: instance, event_sequence: sequence, job_id: "job",
  stage: "synthesis", progress: 0.75, overall_progress: 0.75, eta: 4,
});

describe("a running conversion when the backend connection drops", () => {
  let root: Root;
  let result: ReturnType<typeof useEventStream>;
  let getActiveConversion: ReturnType<typeof vi.fn>;
  let client: ApiClient;
  const connection = () => EventConnection.connections.at(-1)!;

  beforeEach(async () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    vi.useFakeTimers();
    vi.spyOn(Math, "random").mockReturnValue(1);
    vi.stubGlobal("EventSource", EventConnection);
    EventConnection.connections = [];
    getActiveConversion = vi.fn().mockResolvedValue(running);
    client = { getActiveConversion, usesDesktopAuth: false } as unknown as ApiClient;
    function Probe() {
      result = useEventStream({ apiClient: client, baseUrl: "http://desktop.test" });
      return null;
    }
    root = createRoot(document.createElement("div"));
    await act(async () => root.render(<Probe />));
    await act(async () => connection().onopen?.());
    await act(async () => connection().send(progress("instance-1", 5)));
    expect(result.uiState).toBe("running");
    expect(result.etaText).not.toBe("");
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("marks the job as reconnecting and drops the frozen ETA", async () => {
    await act(async () => connection().onerror?.());
    expect(result.connectionLost).toBe(true);
    expect(result.statusText).toBe("Connection lost: reconnecting…");
    expect(result.etaText).toBe("");
    expect(result.progressPct).toBe(75);

    // The same backend comes back: the job is live again.
    await act(async () => vi.advanceTimersByTimeAsync(500));
    await act(async () => connection().onopen?.());
    await act(async () => connection().send(progress("instance-1", 6)));
    expect(result.connectionLost).toBe(false);
    expect(result.uiState).toBe("running");
  });

  it("reports the job as interrupted when a restarted backend answers", async () => {
    getActiveConversion.mockResolvedValue({ active: false, job: null });
    await act(async () => connection().onerror?.());
    await act(async () => vi.advanceTimersByTimeAsync(500));
    await act(async () => connection().onopen?.());
    await act(async () => connection().send({ event_type: "stream", backend_instance_id: "instance-2" }));
    expect(result.uiState).toBe("failed");
    expect(result.statusText).toMatch(/interrupted/i);
    expect(result.statusText).toMatch(/Repair audio/);
    expect(result.activeJobId).toBeNull();
  });
});
