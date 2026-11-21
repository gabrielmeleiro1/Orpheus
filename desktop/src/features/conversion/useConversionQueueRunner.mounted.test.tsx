// @vitest-environment happy-dom

import { act, useCallback, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import { APP_RESTARTED_MESSAGE, createQueueItem, INTERRUPTED_CONVERSION_MESSAGE, type ConversionQueueItem } from "./conversionPanelModel";
import { useConversionQueueRunner } from "./useConversionQueueRunner";
import type { ConversionJobController, StartConversionResult } from "./useConversionJob";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function jobMock(overrides: Partial<ConversionJobController> = {}): ConversionJobController {
  return {
    startConversion: vi.fn(async () => ({ ok: true, jobId: "job" })),
    waitForTerminalState: vi.fn(async (jobId: string) => ({ jobId, status: "completed", message: "" })),
    refreshActiveConversion: vi.fn(async () => {}),
    cancelActiveConversion: vi.fn(async () => {}),
    ...overrides,
  } as unknown as ConversionJobController;
}

const cleanupPending: StartConversionResult = {
  ok: false, status: 409, serverCode: "conversion_cleanup_pending", error: "The previous conversion is still finishing cleanup.",
};

describe("mounted conversion queue ownership and admission", () => {
  let root: Root;
  let container: HTMLDivElement;
  let runner: ReturnType<typeof useConversionQueueRunner>;
  let items: ConversionQueueItem[];
  let queueStatus: string;
  let initialItems: ConversionQueueItem[];

  function Probe({ client, job, ready = true }: { client: ApiClient; job: ConversionJobController; ready?: boolean }) {
    const [queueItems, setQueueItems] = useState(initialItems);
    const [status, setStatus] = useState("");
    const updateQueueItem = useCallback((id: string, update: (item: ConversionQueueItem) => ConversionQueueItem) => {
      setQueueItems((current) => current.map((item) => item.id === id ? update(item) : item));
    }, []);
    items = queueItems;
    queueStatus = status;
    runner = useConversionQueueRunner({
      apiClient: client, conversionJob: job, runtimeOptionsReady: ready,
      queueItems, selectedQueueItem: queueItems[0], updateQueueItem, setQueueStatus: setStatus,
      addQueueItems: vi.fn(), syncSelectedQueueEdits: vi.fn(),
      buildStartPayload: (item) => ({ source_path: item?.sourcePath ?? "/tmp/first.epub", conversion_mode: "audiobook",
        runtime_options: { tts_engine: "engine", voice: "voice", speed: 1 } }),
    });
    return null;
  }

  beforeEach(() => {
    vi.useFakeTimers();
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    initialItems = ["first", "second"].map((name) => ({ ...createQueueItem(`/tmp/${name}.epub`, "audiobook"), status: "ready" }));
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.useRealTimers();
  });

  it("continues after terminal publication while the backend finishes cleanup", async () => {
    const client = {} as ApiClient;
    const startConversion = vi.fn<ConversionJobController["startConversion"]>()
      .mockResolvedValueOnce({ ok: true, jobId: "first-job" })
      .mockResolvedValueOnce(cleanupPending)
      .mockResolvedValueOnce({ ok: true, jobId: "second-job" });
    const job = jobMock({ startConversion });
    await act(async () => root.render(<Probe client={client} job={job} />));
    let run!: Promise<void>;
    await act(async () => { run = runner.onRunQueue(); });
    expect(items.map((item) => item.status)).toEqual(["done", "converting"]);
    expect(startConversion).toHaveBeenCalledTimes(2);
    expect(job.waitForTerminalState).toHaveBeenCalledExactlyOnceWith("first-job");
    await act(async () => { await vi.advanceTimersByTimeAsync(500); await run; });
    expect(items.map((item) => item.status)).toEqual(["done", "done"]);
    expect(startConversion).toHaveBeenCalledTimes(3);
    expect(startConversion.mock.calls[1]).toEqual(startConversion.mock.calls[2]);
    expect(queueStatus).toBe("Queue run finished.");
    expect(runner.queueRunning).toBe(false);
  });

  it("leaves a row whose own job ended without an outcome out of a queue run", async () => {
    initialItems[0] = { ...initialItems[0], status: "warning", jobId: "old-job", error: INTERRUPTED_CONVERSION_MESSAGE };
    const job = jobMock();
    await act(async () => root.render(<Probe client={{} as ApiClient} job={job} />));
    await act(async () => runner.onRunQueue());
    expect(job.startConversion).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({ source_path: "/tmp/second.epub" }), expect.anything());
    expect(items.map((item) => [item.status, item.error])).toEqual([["warning", INTERRUPTED_CONVERSION_MESSAGE], ["done", ""]]);
  });

  it("leaves a row whose start may have made a book to its own Start", async () => {
    initialItems[0] = { ...initialItems[0], status: "warning", error: APP_RESTARTED_MESSAGE, startUncertain: true };
    const job = jobMock();
    await act(async () => root.render(<Probe client={{} as ApiClient} job={job} />));
    await act(async () => runner.onRunQueue());
    expect(job.startConversion).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({ source_path: "/tmp/second.epub" }), expect.anything());
    expect(items[0]).toMatchObject({ status: "warning", error: APP_RESTARTED_MESSAGE });

    await act(async () => runner.onStartConversion(items[0]));
    expect(job.startConversion).toHaveBeenLastCalledWith(
      expect.objectContaining({ source_path: "/tmp/first.epub" }), expect.anything());
    expect(items[0].status).toBe("done");
  });

  it("rejects queue and single-item starts while a restored job owns the backend", async () => {
    const client = {} as ApiClient;
    const job = jobMock({ activeJobId: "restored-job", uiState: "running" });
    await act(async () => root.render(<Probe client={client} job={job} />));
    await act(async () => runner.onRunQueue());
    await act(async () => runner.onStartConversion());
    expect(job.startConversion).not.toHaveBeenCalled();
    expect(items.map((item) => item.status)).toEqual(["ready", "ready"]);
    expect(runner.commandRunning).toBe(false);
  });

  it("pauses with a retryable row when cleanup exceeds its budget", async () => {
    const client = {} as ApiClient;
    const startConversion = vi.fn<ConversionJobController["startConversion"]>().mockResolvedValue(cleanupPending);
    const job = jobMock({ startConversion });
    await act(async () => root.render(<Probe client={client} job={job} />));
    let run!: Promise<void>;
    await act(async () => { run = runner.onRunQueue(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); await run; });
    expect(items.map((item) => item.status)).toEqual(["warning", "ready"]);
    expect(items[0].error).toContain("Try this item again");
    expect(queueStatus).toContain("Queue paused");
    expect(job.waitForTerminalState).not.toHaveBeenCalled();
    const attempts = startConversion.mock.calls.length;
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(startConversion).toHaveBeenCalledTimes(attempts);
    startConversion.mockResolvedValue({ ok: true, jobId: "retry-job" });
    await act(async () => runner.onRunQueue());
    expect(items.map((item) => item.status)).toEqual(["done", "done"]);
  });

  it.each(["single", "queue"])("keeps a %s source retryable while the runtime readiness check is pending", async (command) => {
    const message = "Wait for the local runtime check to finish before starting a conversion.";
    const job = jobMock({ startConversion: vi.fn<ConversionJobController["startConversion"]>(async () => ({
      ok: false, status: 409, serverCode: "runtime_check_pending", error: message,
    })) });
    await act(async () => root.render(<Probe client={{} as ApiClient} job={job} />));
    await act(async () => command === "single" ? runner.onStartConversion() : runner.onRunQueue());
    expect(items.map((item) => item.status)).toEqual(["warning", "ready"]);
    expect(items[0].error).toBe(message);
    expect(job.waitForTerminalState).not.toHaveBeenCalled();
    expect(runner.commandRunning).toBe(false);
    if (command === "queue") expect(queueStatus).toContain("Queue paused");
  });

  it.each(["single", "queue"])("releases a hung %s command on client replacement without letting stale completion release its successor", async (command) => {
    const oldStart = deferred<StartConversionResult>();
    const newStart = deferred<StartConversionResult>();
    const oldClient = {} as ApiClient;
    const newClient = {} as ApiClient;
    const oldJob = jobMock({ startConversion: vi.fn(() => oldStart.promise) });
    const newJob = jobMock({ startConversion: vi.fn(() => newStart.promise) });
    await act(async () => root.render(<Probe client={oldClient} job={oldJob} />));
    let oldRun!: Promise<void>;
    await act(async () => { oldRun = command === "single" ? runner.onStartConversion() : runner.onRunQueue(); });
    await act(async () => root.render(<Probe client={newClient} job={newJob} />));
    expect(items[0].status).toBe("warning");
    expect(runner.queueRunning).toBe(false);
    let newRun!: Promise<void>;
    await act(async () => { newRun = runner.onStartConversion(items[1]); });
    expect(newJob.startConversion).toHaveBeenCalledTimes(1);
    await act(async () => { oldStart.resolve({ ok: true, jobId: "obsolete" }); await oldRun; });
    expect(oldJob.waitForTerminalState).not.toHaveBeenCalled();
    expect(items[0].status).toBe("warning");
    await act(async () => runner.onStartConversion(items[0]));
    expect(newJob.startConversion).toHaveBeenCalledTimes(1);
    await act(async () => { newStart.resolve({ ok: true, jobId: "current" }); await newRun; });
    expect(items[1].status).toBe("done");
  });

  it("cancels cleanup retries when the client changes and permits a new command", async () => {
    const oldClient = {} as ApiClient;
    const newClient = {} as ApiClient;
    const oldJob = jobMock({ startConversion: vi.fn().mockResolvedValue(cleanupPending) });
    const newJob = jobMock();
    await act(async () => root.render(<Probe client={oldClient} job={oldJob} />));
    let oldRun!: Promise<void>;
    await act(async () => { oldRun = runner.onRunQueue(); });
    await act(async () => { root.render(<Probe client={newClient} job={newJob} />); });
    await act(async () => { await oldRun; await vi.advanceTimersByTimeAsync(30_000); });
    expect(oldJob.startConversion).toHaveBeenCalledTimes(1);
    expect(newJob.startConversion).not.toHaveBeenCalled();
    await act(async () => runner.onRunQueue());
    // The interrupted row may have reached the old backend, so only its own Start retries it.
    expect(items.map((item) => [item.status, item.error])).toEqual([["warning", APP_RESTARTED_MESSAGE], ["done", ""]]);
    expect(items[0].startUncertain).toBe(true);
  });

  it.each(["single", "queue"])("cancels %s cleanup backoff without starting the retry", async (command) => {
    const client = {} as ApiClient;
    const job = jobMock({ startConversion: vi.fn().mockResolvedValue(cleanupPending) });
    await act(async () => root.render(<Probe client={client} job={job} />));
    let run!: Promise<void>;
    await act(async () => { run = command === "single" ? runner.onStartConversion() : runner.onRunQueue(); });
    expect(runner.commandRunning).toBe(true);
    await act(async () => { runner.cancelCommand(); await run; });
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(job.startConversion).toHaveBeenCalledTimes(1);
    expect(job.waitForTerminalState).not.toHaveBeenCalled();
    expect(items.map((item) => item.status)).toEqual(["warning", "ready"]);
    expect(queueStatus).toBe("Queue cancelled.");
    expect(runner.commandRunning).toBe(false);
  });

  it("drops a cancelled queue's status when the next conversion starts", async () => {
    const client = {} as ApiClient;
    const job = jobMock({ startConversion: vi.fn().mockResolvedValueOnce(cleanupPending).mockResolvedValue({ ok: true, jobId: "next" }) });
    await act(async () => root.render(<Probe client={client} job={job} />));
    let run!: Promise<void>;
    await act(async () => { run = runner.onRunQueue(); });
    await act(async () => { runner.cancelCommand(); await run; });
    expect(queueStatus).toBe("Queue cancelled.");
    await act(async () => runner.onStartConversion(items[1]));
    expect(items[1].status).toBe("done");
    expect(queueStatus).not.toBe("Queue cancelled.");
  });

  it("keeps a cancelled network start owned until its identity is known, then cancels that job", async () => {
    const pending = deferred<StartConversionResult>();
    const client = {} as ApiClient;
    const job = jobMock({ startConversion: vi.fn(() => pending.promise),
      waitForTerminalState: vi.fn(async (jobId: string) => ({ jobId, status: "cancelled" as const, message: "" })) });
    await act(async () => root.render(<Probe client={client} job={job} />));
    let run!: Promise<void>;
    await act(async () => { run = runner.onRunQueue(); });
    await act(async () => runner.cancelCommand());
    await act(async () => runner.onRunQueue());
    expect(job.startConversion).toHaveBeenCalledTimes(1);
    expect(runner.commandRunning).toBe(true);
    await act(async () => { pending.resolve({ ok: true, jobId: "started-after-cancel" }); await run; });
    expect(job.cancelActiveConversion).toHaveBeenCalledTimes(1);
    expect(job.waitForTerminalState).toHaveBeenCalledExactlyOnceWith("started-after-cancel");
    expect(job.startConversion).toHaveBeenCalledTimes(1);
    expect(items[1].status).toBe("ready");
    expect(runner.commandRunning).toBe(false);
  });

  it.each(["single", "queue"])("reconciles a cancelled %s start whose network outcome is unknown", async (command) => {
    const pending = deferred<StartConversionResult>();
    const client = {} as ApiClient;
    const job = jobMock({ startConversion: vi.fn(() => pending.promise) });
    await act(async () => root.render(<Probe client={client} job={job} />));
    let run!: Promise<void>;
    await act(async () => { run = command === "single" ? runner.onStartConversion() : runner.onRunQueue(); });
    await act(async () => runner.cancelCommand());
    await act(async () => { pending.resolve({ ok: false, error: "Connection closed" }); await run; });
    expect(job.startConversion).toHaveBeenCalledTimes(1);
    expect(job.refreshActiveConversion).toHaveBeenCalledTimes(1);
    expect(items.map((item) => item.status)).toEqual(["warning", "ready"]);
    expect(items[0].error).toContain("Check Library");
    expect(runner.commandRunning).toBe(false);
  });

  it.each([
    [{ ok: true, jobId: "reconciled" } as StartConversionResult, "Conversion start confirmed.", "done"],
    [{ ok: false, error: "Could not confirm whether the conversion started." } as StartConversionResult,
      "Could not confirm whether the conversion started.", "failed"],
  ])("replaces the checking notice once an ambiguous single start resolves (%#)", async (result, status, rowStatus) => {
    const pending = deferred<StartConversionResult>();
    const client = {} as ApiClient;
    const startConversion = vi.fn(() => pending.promise);
    const job = jobMock({ startConversion });
    await act(async () => root.render(<Probe client={client} job={job} />));
    let run!: Promise<void>;
    await act(async () => { run = runner.onStartConversion(); });
    await act(async () => root.render(<Probe client={client} job={{ ...job, isCheckingStart: true }} />));
    expect(queueStatus).toBe("Checking whether the conversion started…");
    await act(async () => {
      pending.resolve({ ok: false, ambiguous: true, clientRequestId: "request", error: "Network unavailable" });
      await run;
    });
    expect(queueStatus).toBe("Checking whether the conversion started…");
    await act(async () => root.render(<Probe client={client} job={{ ...job, isCheckingStart: false,
      startReconciliation: { clientRequestId: "request", result } }} />));
    expect(queueStatus).toBe(status);
    expect(items[0].status).toBe(rowStatus);
  });

  it.each([
    ["unknown", "Terminal wait cancelled", "warning", "Could not confirm the conversion. Check Library before retrying."],
    ["failed", "", "failed", "conversion failed"],
  ] as const)("marks a late-confirmed start that ends %s like any other row", async (terminalStatus, message, rowStatus, rowError) => {
    const pending = deferred<StartConversionResult>();
    const client = {} as ApiClient;
    const job = jobMock({ startConversion: vi.fn(() => pending.promise),
      waitForTerminalState: vi.fn(async (jobId: string) => ({ jobId, status: terminalStatus, message })) });
    await act(async () => root.render(<Probe client={client} job={job} />));
    let run!: Promise<void>;
    await act(async () => { run = runner.onStartConversion(); });
    await act(async () => {
      pending.resolve({ ok: false, ambiguous: true, clientRequestId: "request", error: "Network unavailable" });
      await run;
    });
    await act(async () => root.render(<Probe client={client} job={{ ...job,
      startReconciliation: { clientRequestId: "request", result: { ok: true, jobId: "late" } } }} />));
    expect(job.waitForTerminalState).toHaveBeenCalledExactlyOnceWith("late");
    expect(items[0]).toMatchObject({ jobId: "late", status: rowStatus, error: rowError });
  });

  it.each([
    [{ ok: false, ambiguous: true, error: "Network unavailable" } as StartConversionResult, true],
    [{ ok: false, serverCode: "runtime_options_pending", error: "Wait for runtime options to load before continuing." } as StartConversionResult, false],
  ])("records whether a paused start may have made a book (%#)", async (result, uncertain) => {
    const job = jobMock({ startConversion: vi.fn().mockResolvedValue(result) });
    await act(async () => root.render(<Probe client={{} as ApiClient} job={job} />));
    await act(async () => runner.onRunQueue());
    expect(items[0]).toMatchObject({ status: "warning", error: result.ok ? "" : result.error, startUncertain: uncertain });
  });

  it("stops cleanup retries on unmount", async () => {
    const client = {} as ApiClient;
    const job = jobMock({ startConversion: vi.fn().mockResolvedValue(cleanupPending) });
    await act(async () => root.render(<Probe client={client} job={job} />));
    let run!: Promise<void>;
    await act(async () => { run = runner.onRunQueue(); });
    await act(async () => root.render(null));
    await act(async () => { await run; await vi.advanceTimersByTimeAsync(30_000); });
    expect(job.startConversion).toHaveBeenCalledTimes(1);
  });

  it.each([
    { ok: false, status: 409, serverCode: null, error: "A conversion is already running" },
    { ok: false, status: null, serverCode: null, error: "Network unavailable" },
  ] as StartConversionResult[])("does not replay an unclassified start rejection: $error", async (failure) => {
    const client = {} as ApiClient;
    const job = jobMock({ startConversion: vi.fn().mockResolvedValue(failure) });
    await act(async () => root.render(<Probe client={client} job={job} />));
    await act(async () => runner.onRunQueue());
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(job.startConversion).toHaveBeenCalledTimes(1);
    expect(items.map((item) => item.status)).toEqual(["failed", "ready"]);
  });
});


describe("a failed conversion's row", () => {
  it.each(["single", "queue"])("shows the backend's reason after a %s run", async (command) => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    const reason = "Orpheus cannot write to the output folder. Check that the drive is connected.";
    const job = jobMock({ waitForTerminalState: vi.fn(async (jobId: string) => ({ jobId, status: "failed" as const, message: reason })) });
    let runner!: ReturnType<typeof useConversionQueueRunner>;
    let rows: ConversionQueueItem[] = [];
    function Probe() {
      const [queueItems, setQueueItems] = useState<ConversionQueueItem[]>([{ ...createQueueItem("/tmp/one.epub", "audiobook"), status: "ready" }]);
      rows = queueItems;
      const updateQueueItem = useCallback((id: string, update: (item: ConversionQueueItem) => ConversionQueueItem) => {
        setQueueItems((current) => current.map((item) => item.id === id ? update(item) : item));
      }, []);
      runner = useConversionQueueRunner({
        apiClient: {} as ApiClient, conversionJob: job, runtimeOptionsReady: true, queueItems,
        selectedQueueItem: queueItems[0], updateQueueItem, setQueueStatus: vi.fn(), addQueueItems: vi.fn(),
        syncSelectedQueueEdits: vi.fn(),
        buildStartPayload: () => ({ source_path: "/tmp/one.epub", conversion_mode: "audiobook",
          runtime_options: { tts_engine: "engine", voice: "voice", speed: 1 } }),
      });
      return null;
    }
    const root = createRoot(document.createElement("div"));
    try {
      await act(async () => root.render(<Probe />));
      await act(async () => (command === "single" ? runner.onStartConversion() : runner.onRunQueue()));
      expect(rows[0]).toMatchObject({ status: "failed", error: reason });
    } finally {
      await act(async () => root.unmount());
      vi.unstubAllGlobals();
    }
  });
});

describe("queue runs", () => {
  function setup(job: ConversionJobController) {
    const client = {} as ApiClient;
    let runner!: ReturnType<typeof useConversionQueueRunner>;
    let rows: ConversionQueueItem[] = [];
    let status = "";
    let addRow!: (item: ConversionQueueItem) => void;
    function Probe() {
      const [queueItems, setQueueItems] = useState<ConversionQueueItem[]>(["first", "second"].map((name) =>
        ({ ...createQueueItem(`/tmp/${name}.epub`, "audiobook"), title: name, status: "ready" })));
      const [queueStatus, setQueueStatus] = useState("");
      rows = queueItems;
      status = queueStatus;
      addRow = (item) => setQueueItems((current) => [...current, item]);
      const updateQueueItem = useCallback((id: string, update: (item: ConversionQueueItem) => ConversionQueueItem) => {
        setQueueItems((current) => current.map((item) => item.id === id ? update(item) : item));
      }, []);
      runner = useConversionQueueRunner({
        apiClient: client, conversionJob: job, runtimeOptionsReady: true, queueItems,
        selectedQueueItem: queueItems[0], updateQueueItem, setQueueStatus, addQueueItems: vi.fn(), syncSelectedQueueEdits: vi.fn(),
        buildStartPayload: (item) => ({ source_path: item?.sourcePath ?? "", conversion_mode: "audiobook",
          runtime_options: { tts_engine: "engine", voice: "voice", speed: 1 } }),
      });
      return null;
    }
    return { Probe, runner: () => runner, rows: () => rows, status: () => status, addRow: (item: ConversionQueueItem) => addRow(item) };
  }

  it("picks up a book added while another converts", async () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    const first = deferred<{ jobId: string; status: "completed"; message: string }>();
    const job = jobMock({
      startConversion: vi.fn(async (payload: { source_path: string }) => ({ ok: true as const, jobId: payload.source_path })) as unknown as ConversionJobController["startConversion"],
      waitForTerminalState: vi.fn(async (jobId: string) => jobId === "/tmp/first.epub"
        ? first.promise : { jobId, status: "completed" as const, message: "" }),
    });
    const queue = setup(job);
    const root = createRoot(document.createElement("div"));
    try {
      await act(async () => root.render(<queue.Probe />));
      let run!: Promise<void>;
      await act(async () => { run = queue.runner().onRunQueue(); });
      await act(async () => queue.addRow({ ...createQueueItem("/tmp/third.epub", "audiobook"), title: "third", status: "ready" }));
      await act(async () => { first.resolve({ jobId: "/tmp/first.epub", status: "completed", message: "" }); await run; });
      expect(queue.rows().map((row) => row.status)).toEqual(["done", "done", "done"]);
      expect(job.startConversion).toHaveBeenCalledTimes(3);
    } finally {
      await act(async () => root.unmount());
      vi.unstubAllGlobals();
    }
  });

  it("goes on to the next book when one book is cancelled", async () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    const job = jobMock({
      startConversion: vi.fn(async (payload: { source_path: string }) => ({ ok: true as const, jobId: payload.source_path })) as unknown as ConversionJobController["startConversion"],
      waitForTerminalState: vi.fn(async (jobId: string) => ({ jobId,
        status: jobId === "/tmp/first.epub" ? "cancelled" as const : "completed" as const, message: "" })),
    });
    const queue = setup(job);
    const root = createRoot(document.createElement("div"));
    try {
      await act(async () => root.render(<queue.Probe />));
      await act(async () => queue.runner().onRunQueue());
      expect(queue.rows().map((row) => [row.status, row.error])).toEqual([["failed", "conversion cancelled"], ["done", ""]]);
      expect(queue.status()).toBe("Queue run finished. 1 book was cancelled.");
    } finally {
      await act(async () => root.unmount());
      vi.unstubAllGlobals();
    }
  });
});
