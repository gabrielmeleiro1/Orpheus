// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient, type ApiClient } from "../../api/client";
import type { ActiveConversionResponse, DesktopEventPayload, StartConversionRequest } from "../../types/api";
import { createQueueItem, type ConversionQueueItem } from "./conversionPanelModel";
import { ConversionActionsView } from "./views/ConversionActionsView";
import { useConversionJob } from "./useConversionJob";
import { useConversionQueueController } from "./useConversionQueueController";
import { useConversionQueueRunner } from "./useConversionQueueRunner";

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
    id, status, book_id: null, error: null, title: `Title ${id}`, author: "Author", repair: false,
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
      <ConversionActionsView
        sourceQueue={{ queueCount: queue.queueItems.length, queueItems: [], sourcePath: "book.txt", queueStatus: queue.queueStatus, queueRunning: runner.queueRunning } as never}
        conversionActions={{ showRefreshJob: job.isCheckingStart || Boolean(job.startReconciliation && !job.startReconciliation.result.ok), hasActiveJob: Boolean(job.activeJobId) || runner.commandRunning, startLabel: job.isCheckingStart ? "Checking…" : "Start conversion", blockingReason: "", cancelLabel: "Cancel" } as never}
        capabilities={{ showStartConversion: true, canStartConversion: !job.isCheckingStart && !runner.commandRunning, canRefreshJob: true } as never}
        actions={{ onRefreshJob: job.refreshActiveConversion, onStartConversion: () => runner.onStartConversion() } as never}
      />
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

  it("does not label a new SSE job with the previous job's metadata before reconciliation", async () => {
    active = snapshot("lighthouse");
    await mount(client());
    expect(job.activeJobTitle).toBe("Title lighthouse");
    await act(async () => connection().send({ event_type: "state", job_id: "lighthouse", state: "completed" }));
    expect(job.activeJobId).toBeNull();
    expect(job.activeJobTitle).toBe("Title lighthouse");
    await act(async () => connection().send({ event_type: "state", job_id: "mill", state: "running" }));
    expect(job.activeJobId).toBe("mill");
    expect(job.activeJobTitle).toBe("");
    expect(job.activeJobAuthor).toBe("");
    await act(async () => connection().send({ event_type: "state", job_id: "mill", state: "completed" }));
    expect(job.activeJobTitle).toBe("");
    active = snapshot("mill", "completed");
    await act(async () => job.refreshActiveConversion());
    expect(job.activeJobTitle).toBe("Title mill");
    expect(job.activeJobAuthor).toBe("Author");
  });

  it("retires job metadata immediately when the backend client changes", async () => {
    active = snapshot("same-id");
    await mount(client());
    expect(job.activeJobTitle).toBe("Title same-id");
    const pending = deferred<Response>();
    lookup = () => pending.promise;
    await mount(client());
    expect(job.activeJobTitle).toBe("");
    expect(job.activeJobAuthor).toBe("");
    await act(async () => pending.resolve(json({ ...snapshot("same-id"), job: {
      ...snapshot("same-id").job!, title: "Replacement backend title",
    } })));
    expect(job.activeJobTitle).toBe("Replacement backend title");
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
    active = { ...snapshot("accepted"), job: { ...snapshot("accepted").job!, client_request_id: submissions[0].client_request_id } } as ActiveConversionResponse;
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

  it.each(["another-client", null])("ambiguous_start_with_another_clients_job_is_reported_failed (%s)", async (requestId) => {
    await mount(client());
    await act(async () => queue.addPastedSource({ ...createQueueItem("book.txt", "audiobook"), status: "ready" }));
    start = async () => {
      active = { ...snapshot("other"), job: { ...snapshot("other").job!, client_request_id: requestId } } as ActiveConversionResponse;
      throw new TypeError("Start response lost");
    };
    await act(async () => runner.onRunQueue());
    expect(queue.queueItems[0].status).toBe("failed");
    expect(job.isCheckingStart).toBe(false);
    expect(job.formError).toBe("Start response lost");
    expect(submissions).toHaveLength(1);
  });

  it("checking_state_resolves_when_reconciliation_keeps_failing", async () => {
    await mount(client());
    const failedLookup = vi.fn(async () => { throw new TypeError("Backend unreachable"); });
    lookup = failedLookup;
    start = async () => { throw new TypeError("Start response lost"); };
    await act(async () => queue.addPastedSource({ ...createQueueItem("book.txt", "audiobook"), status: "ready" }));
    await act(async () => runner.onRunQueue());
    expect(job.isCheckingStart).toBe(true);
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(job.isCheckingStart).toBe(false);
    expect(queue.queueItems[0].status).toBe("failed");
    expect(job.formError).toMatch(/could not confirm.*refresh.*retry/i);
    expect(container.querySelector<HTMLButtonElement>("#refresh-btn")?.disabled).toBe(false);
    expect(container.querySelector<HTMLButtonElement>("#start-btn")?.disabled).toBe(false);
    const attempts = failedLookup.mock.calls.length;
    expect(attempts).toBeGreaterThan(1);
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(failedLookup).toHaveBeenCalledTimes(attempts);
    expect(submissions).toHaveLength(1);
  });

  it("checking_state_reconciles_after_a_failed_lookup_and_offers_refresh", async () => {
    await mount(client());
    start = async (request) => {
      active = { ...snapshot("accepted"), job: { ...snapshot("accepted").job!, client_request_id: request.client_request_id } } as ActiveConversionResponse;
      throw new TypeError("Start response lost");
    };
    lookup = async () => { throw new TypeError("Backend unreachable"); };
    await act(async () => queue.addPastedSource({ ...createQueueItem("book.txt", "audiobook"), status: "ready" }));
    await act(async () => runner.onRunQueue());
    expect(job.isCheckingStart).toBe(true);
    expect(container.querySelector<HTMLButtonElement>("#refresh-btn")?.disabled).toBe(false);
    lookup = async () => json(active);
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    expect(job.isCheckingStart).toBe(false);
    expect(queue.queueItems[0]).toMatchObject({ status: "converting", jobId: "accepted", error: "" });
    expect(job.formError).toBe("");
    active = snapshot("accepted", "completed");
    await act(async () => connection().onopen?.());
    expect(queue.queueItems[0].status).toBe("done");
    expect(submissions).toHaveLength(1);
  });
});
