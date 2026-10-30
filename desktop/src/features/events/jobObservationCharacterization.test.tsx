// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient, type ApiClient } from "../../api/client";
import { CONVERSION_START_TIMEOUT_MS } from "../../api/resources/conversion";
import type { ActiveConversionResponse, DesktopEventPayload, StartConversionRequest } from "../../types/api";
import { createQueueItem, type ConversionQueueItem } from "../conversion/conversionPanelModel";
import { useConversionJob } from "../conversion/useConversionJob";
import { useConversionQueueController } from "../conversion/useConversionQueueController";
import { useConversionQueueRunner } from "../conversion/useConversionQueueRunner";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

class EventConnection {
  static connections: EventConnection[] = [];
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  constructor(readonly url: string) { EventConnection.connections.push(this); }
  close() {}
  send(payload: DesktopEventPayload) { this.onmessage?.({ data: JSON.stringify(payload) }); }
}

function snapshot(id: string, status: NonNullable<ActiveConversionResponse["job"]>["status"] = "running"): ActiveConversionResponse {
  return { active: status === "running", job: {
    id, status, book_id: null, error: null, title: `Title ${id}`, author: "Author",
  } };
}

const payload = (item?: ConversionQueueItem): StartConversionRequest => ({
  source_path: item?.sourcePath ?? "book.txt", conversion_mode: "audiobook", title: "Submitted title",
  runtime_options: { tts_engine: "kokoro", voice: "af_heart", speed: 1 },
});
const json = (value: unknown) => new Response(JSON.stringify(value));

describe("conversion observation across transport changes", () => {
  let root: Root;
  let container: HTMLDivElement;
  let job: ReturnType<typeof useConversionJob>;
  let queue: ReturnType<typeof useConversionQueueController>;
  let runner: ReturnType<typeof useConversionQueueRunner>;
  let active: ActiveConversionResponse;
  let lookup: () => Promise<Response>;
  let start: (request: StartConversionRequest) => Promise<Response>;
  let submissions: StartConversionRequest[];
  let startSignal: AbortSignal | null | undefined;

  function Probe({ client, hidden = false }: { client: ApiClient; hidden?: boolean }) {
    job = useConversionJob({ apiClient: client, baseUrl: "http://conversion.test" });
    queue = useConversionQueueController({ apiClient: client });
    runner = useConversionQueueRunner({
      apiClient: client, conversionJob: job, runtimeOptionsReady: true,
      queueItems: queue.queueItems, selectedQueueItem: queue.selectedQueueItem,
      buildStartPayload: payload, addQueueItems: (items) => items.forEach((item) => queue.addPastedSource(item)),
      updateQueueItem: queue.updateQueueItem, syncSelectedQueueEdits: queue.syncSelectedQueueEdits,
      setQueueStatus: queue.setQueueStatus,
    });
    return <section hidden={hidden}>
      <output>{queue.queueItems.map((item) => `${item.label}: ${item.status}`).join(", ")}</output>
      <p>{queue.queueStatus}</p>
    </section>;
  }

  const client = () => createApiClient({ baseUrl: "http://conversion.test", token: "session" });
  const mount = (apiClient: ApiClient, hidden = false) => act(async () => root.render(<Probe client={apiClient} hidden={hidden} />));
  const connection = () => EventConnection.connections.at(-1)!;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.useFakeTimers();
    EventConnection.connections = [];
    vi.stubGlobal("EventSource", EventConnection);
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    active = { active: false, job: null };
    submissions = [];
    startSignal = undefined;
    lookup = async () => json(active);
    start = async () => {
      const id = `job-${submissions.length}`;
      active = snapshot(id);
      return json({ job_id: id, status: "running" });
    };
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const path = new URL(String(input)).pathname;
      if (path === "/api/desktop/query-token") return json({ token: "events", expires_at: 9999999999 });
      if (path === "/api/conversions/active") return lookup();
      if (path === "/api/conversions" && init?.method === "POST") {
        const request = JSON.parse(String(init.body)) as StartConversionRequest;
        submissions.push(request);
        startSignal = init.signal;
        return start(request);
      }
      if (path === "/api/conversions/accepted/cancel") {
        active = snapshot("accepted", "cancelled");
        connection().send({ event_type: "state", job_id: "accepted", state: "cancelled" });
        return json({ job_id: "accepted", cancelled: true });
      }
      throw new Error(`Unexpected request: ${path}`);
    });
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("reconciles the replacement connection and ignores a late snapshot from the retired client", async () => {
    active = snapshot("old");
    await mount(client());
    await act(async () => connection().onopen?.());
    expect(job.activeJobId).toBe("old");
    const delayed = deferred<Response>();
    lookup = () => delayed.promise;
    let terminal!: ReturnType<typeof job.waitForTerminalState>;
    await act(async () => { terminal = job.waitForTerminalState("old"); });
    const retired = connection();
    await act(async () => retired.onerror?.());
    expect(job.streamState).toBe("error");
    active = snapshot("new");
    lookup = async () => json(active);
    await mount(client());
    expect(await terminal).toMatchObject({ jobId: "old", status: "unknown" });
    await act(async () => connection().onopen?.());
    expect(job.streamState).toBe("open");
    expect(job.activeJobId).toBe("new");
    expect(job.activeJobTitle).toBe("Title new");
    await act(async () => {
      delayed.resolve(json(snapshot("old", "completed")));
      retired.send({ event_type: "state", job_id: "old", state: "completed" });
    });
    expect(job.activeJobId).toBe("new");
    expect(job.uiState).toBe("running");
    active = snapshot("new", "completed");
    await act(async () => connection().onopen?.());
    expect(job.uiState).toBe("completed");
    expect(job.activeJobId).toBeNull();
  });

  it("accepted_start_with_lost_response_reconciles_queue_row_and_clears_start_error", async () => {
    const response = deferred<Response>();
    start = () => response.promise;
    await mount(client());
    await act(async () => {
      queue.addPastedSource({ ...createQueueItem("book.txt", "audiobook"), status: "ready" });
    });
    let running!: Promise<void>;
    await act(async () => { running = runner.onRunQueue(); });
    expect(job.isStarting).toBe(true);
    active = { ...snapshot("accepted"), job: { ...snapshot("accepted").job!, client_request_id: submissions[0].client_request_id } };
    await act(async () => { response.reject(new TypeError("Start response lost")); });
    expect(queue.queueItems[0]).toMatchObject({ status: "converting", jobId: "accepted", error: "" });
    expect(job.isStarting).toBe(false);
    expect(job.formError).toBe("");
    expect(job.activeJobId).toBe("accepted");
    expect(job.activeJobTitle).toBe("Title accepted");
    expect(job.uiState).toBe("running");
    await act(async () => connection().onopen?.());
    expect(queue.queueItems[0].status).toBe("converting");
    expect(submissions).toHaveLength(1);
    active = snapshot("accepted", "completed");
    await act(async () => { connection().onopen?.(); await running; });
  });

  it("hung_start_releases_submission_and_reconciles_without_replaying", async () => {
    start = () => new Promise((_resolve, reject) => {
      startSignal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
    });
    await mount(client());
    const source = "upload:book.txt";
    let starting!: ReturnType<typeof job.startConversion>;
    await act(async () => { starting = job.startConversion({ ...payload(), source_path: source }); });
    active = { ...snapshot("accepted"), job: { ...snapshot("accepted").job!, client_request_id: submissions[0].client_request_id } };
    await act(async () => vi.advanceTimersByTimeAsync(CONVERSION_START_TIMEOUT_MS));
    expect(job.isStarting).toBe(false);
    expect(await starting).toEqual({ ok: true, jobId: "accepted" });
    expect(job.formError).toBe("");
    expect(startSignal?.aborted).toBe(true);
    expect(submissions).toHaveLength(1);
  });

  it("ambiguous_start_without_an_accepted_job_is_reported_failed", async () => {
    await mount(client());
    const reconciliation = deferred<Response>();
    lookup = () => reconciliation.promise;
    start = async () => { throw new TypeError("Start response lost"); };
    await act(async () => {
      queue.addPastedSource({ ...createQueueItem("book.txt", "audiobook"), status: "ready" });
    });
    let running!: Promise<void>;
    await act(async () => { running = runner.onRunQueue(); });
    expect(job.formError).toBe("");
    expect(job.isCheckingStart).toBe(true);
    expect(queue.queueItems[0].status).toBe("converting");
    expect(queue.queueStatus).toContain("Checking");
    lookup = async () => json(active);
    await act(async () => { reconciliation.resolve(json(active)); await running; });
    expect(queue.queueItems[0]).toMatchObject({ status: "failed", error: "Start response lost" });
    expect(job.formError).toBe("Start response lost");
    expect(job.isCheckingStart).toBe(false);
    expect(job.isStarting).toBe(false);
    expect(submissions).toHaveLength(1);
  });

  it.each(["single", "queue"])("settles a cancelled hung %s start and cancels its accepted job", async (command) => {
    start = () => new Promise((_resolve, reject) => {
      startSignal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
    });
    await mount(client());
    await act(async () => {
      queue.addPastedSource({ ...createQueueItem("book.txt", "audiobook"), status: "ready" });
    });
    let running!: Promise<void>;
    await act(async () => { running = command === "queue" ? runner.onRunQueue() : runner.onStartConversion(queue.queueItems[0]); });
    active = { ...snapshot("accepted"), job: { ...snapshot("accepted").job!, client_request_id: submissions[0].client_request_id } };
    await act(async () => { runner.cancelCommand(); await running; });
    expect(startSignal?.aborted).toBe(true);
    expect(runner.commandRunning).toBe(false);
    expect(job.isStarting).toBe(false);
    expect(job.formError).toBe("");
    expect(queue.queueItems[0]).toMatchObject({ jobId: "accepted", error: "conversion cancelled" });
    expect(submissions).toHaveLength(1);
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/api/conversions/accepted/cancel"), expect.anything());
  });

  it.each([400, 500])("keeps an HTTP %s rejection definite without reconciling or replaying", async (status) => {
    await mount(client());
    const activeLookup = vi.fn(async () => json(active));
    lookup = activeLookup;
    start = async () => new Response(JSON.stringify({ detail: "Cannot start" }), { status });
    await act(async () => {
      expect(await job.startConversion(payload())).toMatchObject({ ok: false, status });
    });
    expect(job.formError).toContain("Cannot start");
    expect(job.isCheckingStart).toBe(false);
    expect(activeLookup).not.toHaveBeenCalled();
    expect(submissions).toHaveLength(1);
  });

  it.each(["another-submission", null, undefined])("fails an ambiguous start with an unrelated or missing submission id (%s)", async (requestId) => {
    await mount(client());
    await act(async () => {
      queue.addPastedSource({ ...createQueueItem("book.txt", "audiobook"), status: "ready" });
    });
    start = async () => {
      active = { ...snapshot("accepted"), job: { ...snapshot("accepted").job!, client_request_id: requestId } };
      throw new TypeError("Start response lost");
    };
    await act(async () => { await runner.onRunQueue(); });
    expect(queue.queueItems[0]).toMatchObject({ status: "failed", jobId: null, error: "Start response lost" });
    expect(job.isCheckingStart).toBe(false);
    expect(job.isStarting).toBe(false);
    expect(job.formError).toBe("Start response lost");
    active = { ...snapshot("accepted"), job: { ...snapshot("accepted").job!, client_request_id: submissions[0].client_request_id } };
    await act(async () => connection().onopen?.());
    expect(queue.queueItems[0]).toMatchObject({ status: "failed", jobId: null });
    expect(job.activeJobId).toBe("accepted");
    expect(job.isCheckingStart).toBe(false);
    active = snapshot("accepted", "completed");
    await act(async () => connection().onopen?.());
    expect(queue.queueItems[0].status).toBe("failed");
    expect(job.uiState).toBe("completed");
    expect(submissions).toHaveLength(1);
  });

  it("ends checking after bounded retries and allows status refresh", async () => {
    await mount(client());
    const failedLookup = vi.fn(async () => { throw new TypeError("Backend unreachable"); });
    lookup = failedLookup;
    start = async () => { throw new TypeError("Start response lost"); };
    await act(async () => {
      queue.addPastedSource({ ...createQueueItem("book.txt", "audiobook"), status: "ready" });
    });
    await act(async () => runner.onRunQueue());
    expect(job.isCheckingStart).toBe(true);
    expect(queue.queueItems[0]).toMatchObject({ status: "warning", jobId: null });
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(job.isCheckingStart).toBe(false);
    expect(queue.queueItems[0].status).toBe("failed");
    expect(job.formError).toMatch(/could not confirm.*refresh.*retry/i);
    const attempts = failedLookup.mock.calls.length;
    expect(attempts).toBeGreaterThan(1);
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(failedLookup).toHaveBeenCalledTimes(attempts);
    active = snapshot("accepted");
    lookup = async () => json(active);
    await act(async () => { await job.refreshActiveConversion(); });
    expect(job.activeJobId).toBe("accepted");
    expect(queue.queueItems[0]).toMatchObject({ status: "failed", jobId: null });
    expect(submissions).toHaveLength(1);
  });

  it("refreshes a checking start and adopts the matching submission", async () => {
    await mount(client());
    lookup = async () => { throw new TypeError("Backend unreachable"); };
    start = async (request) => {
      active = { ...snapshot("accepted"), job: { ...snapshot("accepted").job!, client_request_id: request.client_request_id } };
      throw new TypeError("Start response lost");
    };
    await act(async () => {
      queue.addPastedSource({ ...createQueueItem("book.txt", "audiobook"), status: "ready" });
    });
    await act(async () => runner.onRunQueue());
    expect(job.isCheckingStart).toBe(true);
    lookup = async () => json(active);
    await act(async () => { await job.refreshActiveConversion(); });
    expect(job.isCheckingStart).toBe(false);
    expect(job.formError).toBe("");
    expect(queue.queueItems[0]).toMatchObject({ status: "converting", jobId: "accepted", error: "" });
    active = snapshot("accepted", "completed");
    await act(async () => connection().onopen?.());
    expect(queue.queueItems[0].status).toBe("done");
    expect(submissions).toHaveLength(1);
  });

  it.each(["completed", "cancelled", "failed"] as const)("resolves a missed %s event from the backend snapshot", async (status) => {
    active = snapshot("book");
    await mount(client());
    let terminal!: ReturnType<typeof job.waitForTerminalState>;
    const finished = vi.fn();
    await act(async () => { terminal = job.waitForTerminalState("book"); void terminal.then(finished); });
    expect(finished).not.toHaveBeenCalled();
    active = snapshot("book", status);
    await act(async () => vi.advanceTimersByTimeAsync(1500));
    expect(await terminal).toMatchObject({ jobId: "book", status });
    expect(job.uiState).toBe(status);
    expect(job.activeJobId).toBeNull();
    expect(await job.waitForTerminalState("book")).toMatchObject({ jobId: "book", status });
  });

  it("returns unknown when a missed terminal event has no remaining job snapshot", async () => {
    active = snapshot("missing");
    await mount(client());
    active = { active: false, job: null };
    let terminal!: ReturnType<typeof job.waitForTerminalState>;
    const finished = vi.fn();
    await act(async () => { terminal = job.waitForTerminalState("missing"); void terminal.then(finished); });
    await act(async () => vi.advanceTimersByTimeAsync(1500));
    expect(finished).not.toHaveBeenCalled();
    await act(async () => vi.advanceTimersByTimeAsync(1500));
    expect(await terminal).toMatchObject({ jobId: "missing", status: "unknown", message: expect.stringContaining("likely missed") });
  });

  it("continues the conversion queue while its mounted controls are hidden", async () => {
    const apiClient = client();
    await mount(apiClient);
    await act(async () => {
      for (const path of ["first.txt", "second.txt"]) {
        queue.addPastedSource({ ...createQueueItem(path, "audiobook"), status: "ready" });
      }
    });
    let running!: Promise<void>;
    await act(async () => { running = runner.onRunQueue(); });
    expect(queue.queueItems.map((item) => item.status)).toEqual(["converting", "ready"]);
    await mount(apiClient, true);
    active = snapshot("job-1", "completed");
    await act(async () => connection().send({ event_type: "state", job_id: "job-1", state: "completed" }));
    expect(submissions.map((request) => request.source_path)).toEqual(["first.txt", "second.txt"]);
    expect(queue.queueItems.map((item) => item.status)).toEqual(["done", "converting"]);
    expect(runner.queueRunning).toBe(true);
    active = snapshot("job-2", "completed");
    await act(async () => { connection().send({ event_type: "state", job_id: "job-2", state: "completed" }); await running; });
    expect(runner.queueRunning).toBe(false);
    await mount(apiClient);
    expect(queue.queueItems.map((item) => item.status)).toEqual(["done", "done"]);
    expect(container.textContent).toContain("Queue run finished.");
    expect(job.uiState).toBe("completed");
  });
});
