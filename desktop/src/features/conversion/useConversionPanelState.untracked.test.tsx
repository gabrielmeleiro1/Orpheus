// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import { createProgressStages } from "../events/progressStageModel";
import { START_CHECKING_MESSAGE } from "./conversionPanelModel";
import type { ConversionJobController } from "./useConversionJob";
import { useConversionPanelState } from "./useConversionPanelState";

vi.mock("./useConversionOptions", () => ({ useConversionOptions: () => ({
  loading: false, error: "", retryRuntimeOptions: vi.fn(), selectedEngine: "kokoro", selectedVoice: "voice",
  cleanerBackend: "basic", cleanerApiKey: "", cleanerModel: "", cleanerModelOptions: [], engineOptions: [],
  filteredVoiceOptions: [], voiceFilter: "", quantizationOptions: [],
}) }));

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it("counts a running repair's row and labels it while the connection is lost", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const job = (connectionLost: boolean, described = true) => ({
    uiState: "running", activeJobId: "repair-job", activeJobTitle: described ? "Lantern" : "", activeJobMetaLoaded: described,
    progressPct: 75, stages: createProgressStages(),
    etaText: connectionLost ? "" : "ETA 4s", connectionLost,
    statusText: connectionLost ? "Connection lost: reconnecting…" : "Synthesizing",
    clearFormError: vi.fn(), startConversion: vi.fn(), waitForTerminalState: vi.fn(), refreshActiveConversion: vi.fn(),
  }) as unknown as ConversionJobController;
  let current!: ReturnType<typeof useConversionPanelState>;
  function Probe({ conversionJob }: { conversionJob: ConversionJobController }) {
    current = useConversionPanelState({ activeTab: "convert", hidden: false, apiClient: {} as ApiClient,
      connectionState: "connected", conversionJob,
      onOpenSettings: vi.fn() });
    return null;
  }
  const root = createRoot(document.createElement("div"));
  try {
    // The job is announced before its title is read: no nameless row in between.
    await act(async () => root.render(<Probe conversionJob={job(false, false)} />));
    expect(current.sourceQueue.untrackedJob).toBeNull();
    expect(current.sourceQueue.queueCount).toBe(0);

    await act(async () => root.render(<Probe conversionJob={job(false)} />));
    expect(current.sourceQueue.untrackedJob?.title).toBe("Lantern");
    expect(current.sourceQueue.queueCount).toBe(1);
    expect(current.sourceQueue.untrackedJob?.progressLabel).toContain("4s left");

    await act(async () => root.render(<Probe conversionJob={job(true)} />));
    expect(current.sourceQueue.untrackedJob?.progressLabel).toBe("Connection lost: reconnecting…");
  } finally {
    await act(async () => root.unmount());
  }
});

it("ignores the second click of a double-click that lands on Cancel", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const conversionJob = {
    uiState: "running", activeJobId: "job", activeJobTitle: "Book", progressPct: 1, stages: createProgressStages(),
    etaText: "", connectionLost: false, statusText: "", clearFormError: vi.fn(), startConversion: vi.fn(),
    waitForTerminalState: vi.fn(), refreshActiveConversion: vi.fn(), cancelActiveConversion: vi.fn(async () => {}),
  } as unknown as ConversionJobController;
  let current!: ReturnType<typeof useConversionPanelState>;
  function Probe() {
    current = useConversionPanelState({ activeTab: "convert", hidden: false, apiClient: {} as ApiClient,
      connectionState: "connected", conversionJob,
      onOpenSettings: vi.fn() });
    return null;
  }
  const root = createRoot(document.createElement("div"));
  try {
    await act(async () => root.render(<Probe />));
    await act(async () => current.actions.onCancelJob(2));
    expect(conversionJob.cancelActiveConversion).not.toHaveBeenCalled();
    await act(async () => current.actions.onCancelJob(1));
    expect(conversionJob.cancelActiveConversion).toHaveBeenCalledTimes(1);
  } finally {
    await act(async () => root.unmount());
  }
});

it("ignores a separate fast click that lands on Cancel right after Start, then cancels as usual", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.useFakeTimers({ toFake: ["performance", "Date", "setTimeout", "clearTimeout"] });
  const conversionJob = {
    uiState: "idle", activeJobId: null, activeJobTitle: "", progressPct: 0, stages: createProgressStages(), etaText: "",
    connectionLost: false, statusText: "", clearFormError: vi.fn(), refreshActiveConversion: vi.fn(),
    startConversion: vi.fn(async () => ({ ok: true, jobId: "job-1" })),
    waitForTerminalState: vi.fn(() => new Promise(() => {})), cancelActiveConversion: vi.fn(async () => {}),
  } as unknown as ConversionJobController;
  const client = pasteClient();
  let current!: ReturnType<typeof useConversionPanelState>;
  function Probe() {
    current = useConversionPanelState({ activeTab: "convert", hidden: false, apiClient: client,
      connectionState: "connected", conversionJob,
      onOpenSettings: vi.fn() });
    return null;
  }
  const root = createRoot(document.createElement("div"));
  try {
    await act(async () => root.render(<Probe />));
    await act(async () => { current.actions.setSourceInput("paste"); });
    await act(async () => current.actions.setPastedText("Pasted source"));
    await act(async () => { await current.actions.onAddPastedText(); });
    // Longer than the guard since the page loaded: only a Start arms it.
    vi.advanceTimersByTime(5_000);
    await act(async () => { void current.actions.onStartConversion(); });
    expect(current.conversionActions.hasActiveJob).toBe(true);

    // Each is its own click (count 1), as touch and slow double-click settings report them.
    // Clicks 40, 120, 250 and 499 ms after Start.
    for (const waitMs of [40, 80, 130, 249]) {
      vi.advanceTimersByTime(waitMs);
      await act(async () => current.actions.onCancelJob(1));
    }
    expect(conversionJob.cancelActiveConversion).not.toHaveBeenCalled();
    expect(current.sourceQueue.queueItems[0].statusText).toBe("Converting…");

    // Past the window a keyboard activation (click count 0) cancels at once.
    vi.advanceTimersByTime(1);
    await act(async () => current.actions.onCancelJob(0));
    expect(conversionJob.cancelActiveConversion).toHaveBeenCalledTimes(1);
  } finally {
    await act(async () => root.unmount());
  }
});

it("notes on a queued row that the book is already in the library", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const client = { uploadSourceText: vi.fn(async () => ({
    source_handle: "upload:paste.md", original_name: "paste.md", size_bytes: 10,
    preflight: { title: "Lantern", author: null, chapter_count: 2, source_type: "md", sample: "s", warnings: [], size_bytes: 10,
      requires_ocr: false, pdf_text_layer: null, existing_books: [{ id: 4, title: "The Lantern", match: "same_title" }] },
  })) } as unknown as ApiClient;
  const conversionJob = { clearFormError: vi.fn(), startConversion: vi.fn(), waitForTerminalState: vi.fn(),
    refreshActiveConversion: vi.fn() } as unknown as ConversionJobController;
  let current!: ReturnType<typeof useConversionPanelState>;
  function Probe() {
    current = useConversionPanelState({ activeTab: "convert", hidden: false, apiClient: client,
      connectionState: "connected", conversionJob,
      onOpenSettings: vi.fn() });
    return null;
  }
  const root = createRoot(document.createElement("div"));
  try {
    await act(async () => root.render(<Probe />));
    await act(async () => { current.actions.setSourceInput("paste"); });
    await act(async () => current.actions.setPastedText("Pasted source"));
    await act(async () => { await current.actions.onAddPastedText(); });
    expect(current.sourceQueue.queueItems[0].meta).toBe("Already in your library: The Lantern · MD · 2 chapters");
  } finally {
    await act(async () => root.unmount());
  }
});

it("shows how long a large paste will be before it is started", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const conversionJob = { clearFormError: vi.fn(), startConversion: vi.fn(), waitForTerminalState: vi.fn(),
    refreshActiveConversion: vi.fn() } as unknown as ConversionJobController;
  let current!: ReturnType<typeof useConversionPanelState>;
  function Probe() {
    current = useConversionPanelState({ activeTab: "convert", hidden: false, apiClient: {} as ApiClient,
      connectionState: "connected", conversionJob,
      onOpenSettings: vi.fn() });
    return null;
  }
  const root = createRoot(document.createElement("div"));
  try {
    await act(async () => root.render(<Probe />));
    await act(async () => { current.actions.setSourceInput("paste"); });
    await act(async () => current.actions.setPastedText("x".repeat(66_600)));
    expect(current.sourceQueue.pasteStatus).toBe("66,600 characters · About 1 hr of audio");
  } finally {
    await act(async () => root.unmount());
  }
});

function pasteClient(): ApiClient {
  return { uploadSourceText: vi.fn(async () => ({
    source_handle: "upload:paste.md", original_name: "paste.md", size_bytes: 10,
    preflight: { title: "Lantern", author: null, chapter_count: 1, source_type: "md", sample: "s", warnings: [], size_bytes: 10,
      requires_ocr: false, pdf_text_layer: null },
  })) } as unknown as ApiClient;
}

it("stops calling a cancelled book Cancelled once a repair from Library completes it", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const client = pasteClient();
  let finish!: (terminal: { jobId: string; status: string; message: string }) => void;
  const terminal = new Promise<{ jobId: string; status: string; message: string }>((resolve) => { finish = resolve; });
  const idle = {
    uiState: "idle", activeJobId: null, activeJobTitle: "", progressPct: 0, stages: createProgressStages(), etaText: "",
    connectionLost: false, statusText: "", clearFormError: vi.fn(), refreshActiveConversion: vi.fn(),
    startConversion: vi.fn(async () => ({ ok: true, jobId: "job-1" })), waitForTerminalState: vi.fn(() => terminal),
  };
  const ownBook = { jobId: "job-1", bookId: 7 };
  const repairBook = { jobId: "repair_7_1", bookId: 7 };
  const jobs = {
    idle: idle as unknown as ConversionJobController,
    ownBookKnown: { ...idle, activeJobBook: ownBook } as unknown as ConversionJobController,
    repairRunning: { ...idle, uiState: "running", activeJobId: "repair_7_1", activeJobTitle: "Lantern",
      activeJobMetaLoaded: true, progressPct: 40, activeJobBook: repairBook } as unknown as ConversionJobController,
    repairDone: { ...idle, uiState: "completed", activeJobBook: repairBook } as unknown as ConversionJobController,
  };
  let current!: ReturnType<typeof useConversionPanelState>;
  function Probe({ conversionJob }: { conversionJob: ConversionJobController }) {
    current = useConversionPanelState({ activeTab: "convert", hidden: false, apiClient: client,
      connectionState: "connected", conversionJob,
      onOpenSettings: vi.fn() });
    return null;
  }
  const root = createRoot(document.createElement("div"));
  try {
    await act(async () => root.render(<Probe conversionJob={jobs.idle} />));
    await act(async () => { current.actions.setSourceInput("paste"); });
    await act(async () => current.actions.setPastedText("Pasted source"));
    await act(async () => { await current.actions.onAddPastedText(); });
    let run!: Promise<void>;
    await act(async () => { run = current.actions.onStartConversion(); });
    expect(current.sourceQueue.queueItems[0].statusText).toBe("Converting…");
    // The backend reports the book once the first chapters are saved.
    await act(async () => root.render(<Probe conversionJob={jobs.ownBookKnown} />));
    await act(async () => { finish({ jobId: "job-1", status: "cancelled", message: "" }); await run; });
    expect(current.sourceQueue.queueItems[0].statusText).toBe("Cancelled");

    // The repair rebuilds this row's book: the row shows it, with no second row.
    await act(async () => root.render(<Probe conversionJob={jobs.repairRunning} />));
    expect(current.sourceQueue.queueItems[0]).toMatchObject({
      title: "Lantern", statusText: "Converting…", converting: true, finished: false, progressPct: 40, canRemove: false,
    });
    expect(current.sourceQueue.untrackedJob).toBeNull();
    expect(current.sourceQueue.queueCount).toBe(1);

    await act(async () => root.render(<Probe conversionJob={jobs.repairDone} />));
    expect(current.sourceQueue.queueItems[0]).toMatchObject({
      statusText: "Done", statusClassName: "queue-item-status status-done", finished: true,
    });
    expect(current.sourceQueue.queueItems[0].error).toBeUndefined();
    expect(current.conversionActions.startLabel).toBe("Convert again");
  } finally {
    await act(async () => root.unmount());
  }
});

it("shows a repair on the row of a book cancelled before its book id was known", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const client = pasteClient();
  const idle = {
    uiState: "idle", activeJobId: null, activeJobTitle: "", activeJobMetaLoaded: false, progressPct: 0,
    stages: createProgressStages(), etaText: "", connectionLost: false, statusText: "", clearFormError: vi.fn(),
    refreshActiveConversion: vi.fn(), startConversion: vi.fn(async () => ({ ok: true, jobId: "job-1" })),
    waitForTerminalState: vi.fn(async (jobId: string) => ({ jobId, status: "cancelled", message: "" })),
  };
  const repairBook = { jobId: "repair_7_1", bookId: 7 };
  const jobs = {
    idle: idle as unknown as ConversionJobController,
    // Read after the job ended; no snapshot of the running job carried it.
    cancelledBookKnown: { ...idle, uiState: "cancelled", activeJobBook: { jobId: "job-1", bookId: 7 } } as unknown as ConversionJobController,
    repairRunning: { ...idle, uiState: "running", activeJobId: "repair_7_1", activeJobTitle: "Lantern",
      activeJobMetaLoaded: true, progressPct: 40, activeJobBook: repairBook } as unknown as ConversionJobController,
    repairDone: { ...idle, uiState: "completed", activeJobBook: repairBook } as unknown as ConversionJobController,
  };
  let current!: ReturnType<typeof useConversionPanelState>;
  function Probe({ conversionJob }: { conversionJob: ConversionJobController }) {
    current = useConversionPanelState({ activeTab: "convert", hidden: false, apiClient: client,
      connectionState: "connected", conversionJob,
      onOpenSettings: vi.fn() });
    return null;
  }
  const root = createRoot(document.createElement("div"));
  try {
    await act(async () => root.render(<Probe conversionJob={jobs.idle} />));
    await act(async () => { current.actions.setSourceInput("paste"); });
    await act(async () => current.actions.setPastedText("Pasted source"));
    await act(async () => { await current.actions.onAddPastedText(); });
    await act(async () => { await current.actions.onStartConversion(); });
    expect(current.sourceQueue.queueItems[0].statusText).toBe("Cancelled");
    await act(async () => root.render(<Probe conversionJob={jobs.cancelledBookKnown} />));
    expect(current.sourceQueue.queueItems[0].statusText).toBe("Cancelled");

    await act(async () => root.render(<Probe conversionJob={jobs.repairRunning} />));
    expect(current.sourceQueue.queueItems[0]).toMatchObject({ statusText: "Converting…", converting: true, progressPct: 40 });
    expect(current.sourceQueue.untrackedJob).toBeNull();
    expect(current.sourceQueue.queueCount).toBe(1);

    await act(async () => root.render(<Probe conversionJob={jobs.repairDone} />));
    expect(current.sourceQueue.queueItems[0].statusText).toBe("Done");
  } finally {
    await act(async () => root.unmount());
  }
});

it("keeps one row with one state when the backend is lost mid-conversion", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const client = pasteClient();
  let finish!: (terminal: { jobId: string; status: string; message: string }) => void;
  const terminal = new Promise<{ jobId: string; status: string; message: string }>((resolve) => { finish = resolve; });
  const idle = {
    uiState: "idle", activeJobId: null, activeJobTitle: "", activeJobMetaLoaded: false, progressPct: 0,
    stages: createProgressStages(), etaText: "", connectionLost: false, statusText: "", clearFormError: vi.fn(),
    refreshActiveConversion: vi.fn(), cancelActiveConversion: vi.fn(async () => {}),
    startConversion: vi.fn(async () => ({ ok: true, jobId: "job-1" })), waitForTerminalState: vi.fn(() => terminal),
  };
  const running = { ...idle, uiState: "running", activeJobId: "job-1", activeJobTitle: "Lantern", activeJobMetaLoaded: true,
    progressPct: 12, statusText: "Cleaning" };
  const lost = { ...running, connectionLost: true, statusText: "Connection lost: reconnecting…" };
  const jobs = {
    idle: idle as unknown as ConversionJobController,
    running: running as unknown as ConversionJobController,
    lost: lost as unknown as ConversionJobController,
    completed: { ...idle, uiState: "completed", activeJobBook: { jobId: "job-1", bookId: 7 } } as unknown as ConversionJobController,
  };
  let current!: ReturnType<typeof useConversionPanelState>;
  function Probe({ conversionJob }: { conversionJob: ConversionJobController }) {
    current = useConversionPanelState({ activeTab: "convert", hidden: false, apiClient: client,
      connectionState: "connected", conversionJob,
      onOpenSettings: vi.fn() });
    return null;
  }
  const root = createRoot(document.createElement("div"));
  try {
    await act(async () => root.render(<Probe conversionJob={jobs.idle} />));
    await act(async () => { current.actions.setSourceInput("paste"); });
    await act(async () => current.actions.setPastedText("Pasted source"));
    await act(async () => { await current.actions.onAddPastedText(); });
    let run!: Promise<void>;
    await act(async () => { run = current.actions.onStartConversion(); });
    await act(async () => root.render(<Probe conversionJob={jobs.running} />));
    expect(current.conversionActions.runningStatus).toBeUndefined();

    await act(async () => root.render(<Probe conversionJob={jobs.lost} />));
    expect(current.sourceQueue.queueItems[0].progressLabel).toBe("Connection lost: reconnecting…");
    expect(current.conversionActions.runningStatus).toBe("Connection lost: reconnecting…");

    // The wait for the job's end gives up; the stream still reports the last state it saw.
    await act(async () => { finish({ jobId: "job-1", status: "unknown", message: "" }); await run; });
    expect(current.sourceQueue.queueItems).toHaveLength(1);
    expect(current.sourceQueue.queueItems[0]).toMatchObject({
      statusText: "Warning", error: "Could not confirm the conversion. Check Library before retrying.", converting: false,
    });
    expect(current.sourceQueue.queueItems[0].progressPct).toBeUndefined();
    expect(current.sourceQueue.untrackedJob).toBeNull();
    expect(current.sourceQueue.queueCount).toBe(1);
    expect(current.conversionActions.runningStatus).toBe("Connection lost: reconnecting…");

    // The connection returns and the job is still running: the same row shows it again.
    await act(async () => root.render(<Probe conversionJob={jobs.running} />));
    expect(current.sourceQueue.queueItems[0]).toMatchObject({ statusText: "Converting…", converting: true, progressPct: 12 });
    expect(current.sourceQueue.queueItems[0].error).toBeUndefined();
    expect(current.sourceQueue.untrackedJob).toBeNull();

    await act(async () => root.render(<Probe conversionJob={jobs.completed} />));
    expect(current.sourceQueue.queueItems[0]).toMatchObject({ statusText: "Done", finished: true });
    expect(current.sourceQueue.queueItems[0].error).toBeUndefined();
  } finally {
    await act(async () => root.unmount());
  }
});

it("offers to convert a finished book again instead of an unqualified Start", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const conversionJob = {
    clearFormError: vi.fn(), refreshActiveConversion: vi.fn(),
    startConversion: vi.fn(async () => ({ ok: true, jobId: "job-1" })),
    waitForTerminalState: vi.fn(async (jobId: string) => ({ jobId, status: "completed", message: "" })),
  } as unknown as ConversionJobController;
  const client = pasteClient();
  let current!: ReturnType<typeof useConversionPanelState>;
  function Probe() {
    current = useConversionPanelState({ activeTab: "convert", hidden: false, apiClient: client,
      connectionState: "connected", conversionJob,
      onOpenSettings: vi.fn() });
    return null;
  }
  const root = createRoot(document.createElement("div"));
  try {
    await act(async () => root.render(<Probe />));
    await act(async () => { current.actions.setSourceInput("paste"); });
    await act(async () => current.actions.setPastedText("Pasted source"));
    await act(async () => { await current.actions.onAddPastedText(); });
    expect(current.conversionActions.startLabel).toBe("Start");

    await act(async () => { await current.actions.onStartConversion(); });
    // Only a finished row is left: the one action available restarts it, and says so.
    expect(current.sourceQueue.queueItems.map((item) => item.statusText)).toEqual(["Done"]);
    expect(current.capabilities.canStartConversion).toBe(true);
    expect(current.conversionActions.startLabel).toBe("Convert again");
  } finally {
    await act(async () => root.unmount());
  }
});

it("starts the waiting book, not the finished one that is still selected", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const conversionJob = {
    clearFormError: vi.fn(), refreshActiveConversion: vi.fn(),
    startConversion: vi.fn(async () => ({ ok: true, jobId: `job-${conversionJob.startConversion.mock.calls.length}` })),
    waitForTerminalState: vi.fn(async (jobId: string) => ({ jobId, status: "completed", message: "" })),
  };
  const client = pasteClient();
  const startCommandRef = { current: null as (() => void) | null };
  let current!: ReturnType<typeof useConversionPanelState>;
  function Probe() {
    current = useConversionPanelState({ activeTab: "convert", hidden: false, apiClient: client, startCommandRef,
      connectionState: "connected", conversionJob: conversionJob as unknown as ConversionJobController,
      onOpenSettings: vi.fn() });
    return null;
  }
  const addPaste = async () => {
    await act(async () => current.actions.setPastedText("Pasted source"));
    await act(async () => { await current.actions.onAddPastedText(); });
  };
  const root = createRoot(document.createElement("div"));
  try {
    await act(async () => root.render(<Probe />));
    await act(async () => { current.actions.setSourceInput("paste"); });
    await addPaste();
    await act(async () => { await current.actions.onStartConversion(); });
    await addPaste();
    const [finished, waiting] = current.sourceQueue.queueItems;
    expect([finished.statusText, waiting.statusText]).toEqual(["Done", "Ready"]);

    // Back on the finished row: converting it again is offered, but not as the primary.
    await act(async () => current.actions.selectQueueItem(finished.id));
    expect(current.conversionActions.startLabel).toBe("Convert again");
    expect(current.capabilities).toMatchObject({
      showRunQueue: true, canRunQueue: true, showStartConversion: true, canStartConversion: true,
    });

    await act(async () => { startCommandRef.current?.(); });
    expect(conversionJob.startConversion).toHaveBeenCalledTimes(2);
    expect(current.sourceQueue.queueItems.map((item) => item.statusText)).toEqual(["Done", "Done"]);
  } finally {
    await act(async () => root.unmount());
  }
});

it("offers a row whose start may have made a book its own Start, not Start all", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const conversionJob = {
    clearFormError: vi.fn(), refreshActiveConversion: vi.fn(),
    startConversion: vi.fn(async () => ({ ok: false, ambiguous: true, error: START_CHECKING_MESSAGE })),
    waitForTerminalState: vi.fn(),
  };
  const client = pasteClient();
  let current!: ReturnType<typeof useConversionPanelState>;
  function Probe() {
    current = useConversionPanelState({ activeTab: "convert", hidden: false, apiClient: client,
      startCommandRef: { current: null }, connectionState: "connected",
      conversionJob: conversionJob as unknown as ConversionJobController, onOpenSettings: vi.fn() });
    return null;
  }
  const root = createRoot(document.createElement("div"));
  try {
    await act(async () => root.render(<Probe />));
    await act(async () => { current.actions.setSourceInput("paste"); });
    for (const text of ["First source", "Second source"]) {
      await act(async () => current.actions.setPastedText(text));
      await act(async () => { await current.actions.onAddPastedText(); });
      await act(async () => { await current.actions.onStartConversion(); });
    }
    expect(conversionJob.startConversion).toHaveBeenCalledTimes(2);
    expect(current.sourceQueue.queueItems).toHaveLength(2);
    expect(current.capabilities).toMatchObject({ canRunQueue: false, showStartConversion: true, canStartConversion: true });
  } finally {
    await act(async () => root.unmount());
  }
});

it.each(["unavailable", "checking"] as const)("keeps Create from starting anything while the runtime is %s", async (runtimeState) => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const conversionJob = {
    clearFormError: vi.fn(), refreshActiveConversion: vi.fn(),
    startConversion: vi.fn(async () => ({ ok: true, jobId: "job-1" })),
    waitForTerminalState: vi.fn(async (jobId: string) => ({ jobId, status: "completed", message: "" })),
  } as unknown as ConversionJobController;
  const client = pasteClient();
  const startCommandRef = { current: null as (() => void) | null };
  let current!: ReturnType<typeof useConversionPanelState>;
  function Probe({ state }: { state: "ready" | "checking" | "unavailable" }) {
    current = useConversionPanelState({ activeTab: "convert", hidden: false, apiClient: client, runtimeState: state,
      connectionState: "connected", conversionJob, startCommandRef,
      onOpenSettings: vi.fn() });
    return null;
  }
  const root = createRoot(document.createElement("div"));
  try {
    await act(async () => root.render(<Probe state={runtimeState} />));
    await act(async () => { current.actions.setSourceInput("paste"); });
    for (let index = 0; index < 2; index += 1) {
      await act(async () => current.actions.setPastedText("Pasted source"));
      await act(async () => { await current.actions.onAddPastedText(); });
    }
    expect(current.capabilities.canStartConversion).toBe(false);
    expect(current.capabilities.canRunQueue).toBe(false);
    expect(current.conversionActions.blockingReason).toBe(runtimeState === "unavailable"
      ? "Conversion is unavailable. Set up a voice model in Settings."
      : "Wait for the runtime check to finish.");
    expect(current.conversionActions.showSettingsShortcut).toBe(runtimeState === "unavailable");
    await act(async () => { startCommandRef.current?.(); });
    expect(conversionJob.startConversion).not.toHaveBeenCalled();

    await act(async () => root.render(<Probe state="ready" />));
    expect(current.conversionActions.blockingReason).toBeNull();
    expect(current.capabilities.canRunQueue).toBe(true);
  } finally {
    await act(async () => root.unmount());
  }
});

it.each(["checking", "error"] as const)("keeps Create from starting anything while the connection is %s", async (connectionState) => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const conversionJob = {
    clearFormError: vi.fn(), refreshActiveConversion: vi.fn(),
    startConversion: vi.fn(async () => ({ ok: true, jobId: "job-1" })),
    waitForTerminalState: vi.fn(async (jobId: string) => ({ jobId, status: "completed", message: "" })),
  } as unknown as ConversionJobController;
  const client = pasteClient();
  const startCommandRef = { current: null as (() => void) | null };
  let current!: ReturnType<typeof useConversionPanelState>;
  function Probe({ state, runtimeState = "ready" }: { state: "connected" | "checking" | "error"; runtimeState?: "ready" | "checking" }) {
    current = useConversionPanelState({ activeTab: "convert", hidden: false, apiClient: client, runtimeState,
      connectionState: state, conversionJob, startCommandRef,
      onOpenSettings: vi.fn() });
    return null;
  }
  const root = createRoot(document.createElement("div"));
  try {
    await act(async () => root.render(<Probe state="connected" />));
    await act(async () => { current.actions.setSourceInput("paste"); });
    for (let index = 0; index < 2; index += 1) {
      await act(async () => current.actions.setPastedText("Pasted source"));
      await act(async () => { await current.actions.onAddPastedText(); });
    }
    expect(current.capabilities.canRunQueue).toBe(true);

    await act(async () => root.render(<Probe state={connectionState} />));
    expect(current.capabilities.canStartConversion).toBe(false);
    expect(current.capabilities.canRunQueue).toBe(false);
    expect(current.conversionActions.blockingReason).toBe("Orpheus is reconnecting. Start is available once it is back.");
    expect(current.conversionActions.showSettingsShortcut).toBe(false);
    await act(async () => { startCommandRef.current?.(); });
    expect(conversionJob.startConversion).not.toHaveBeenCalled();

    // A first connection is covered by the runtime check, which names itself.
    await act(async () => root.render(<Probe state={connectionState} runtimeState="checking" />));
    expect(current.conversionActions.blockingReason).toBe("Wait for the runtime check to finish.");

    await act(async () => root.render(<Probe state="connected" />));
    expect(current.conversionActions.blockingReason).toBeNull();
    expect(current.capabilities.canRunQueue).toBe(true);
  } finally {
    await act(async () => root.unmount());
  }
});

it("shows a long source's length on its row and a blocked source's reason once", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const reason = "This PDF is password-protected. Remove the password, then add it again.";
  const uploads = [
    { title: "Long", author: null, chapter_count: 12, source_type: "md", sample: "s", warnings: [], size_bytes: 10,
      requires_ocr: false, pdf_text_layer: null, blocking_error: null, estimated_audio_seconds: 12_000 },
    { title: "Locked", author: null, chapter_count: 0, source_type: "pdf", sample: "", warnings: [reason], size_bytes: 10,
      requires_ocr: false, pdf_text_layer: null, estimated_audio_seconds: null,
      blocking_error: { code: "pdf_password_protected", message: reason } },
  ];
  const client = { uploadSourceText: vi.fn(async () => ({
    source_handle: `upload:paste-${uploads.length}.md`, original_name: "paste.md", size_bytes: 10, preflight: uploads.shift(),
  })) } as unknown as ApiClient;
  const conversionJob = { clearFormError: vi.fn(), startConversion: vi.fn(), waitForTerminalState: vi.fn(),
    refreshActiveConversion: vi.fn() } as unknown as ConversionJobController;
  let current!: ReturnType<typeof useConversionPanelState>;
  function Probe() {
    current = useConversionPanelState({ activeTab: "convert", hidden: false, apiClient: client,
      connectionState: "connected", conversionJob,
      onOpenSettings: vi.fn() });
    return null;
  }
  const root = createRoot(document.createElement("div"));
  try {
    await act(async () => root.render(<Probe />));
    await act(async () => { current.actions.setSourceInput("paste"); });
    for (let index = 0; index < 2; index += 1) {
      await act(async () => current.actions.setPastedText("Pasted source"));
      await act(async () => { await current.actions.onAddPastedText(); });
    }
    const [long, locked] = current.sourceQueue.queueItems;
    expect(long.meta).toBe("MD · 12 chapters · About 3 hr 20 min of audio");
    expect(locked).toMatchObject({ statusText: "Failed", error: reason });

    await act(async () => current.actions.selectQueueItem(locked.id));
    expect(current.sourceQueue.preflightCard).toMatchObject({ canConvert: false, blockingReason: reason, warnings: [] });
  } finally {
    await act(async () => root.unmount());
  }
});
