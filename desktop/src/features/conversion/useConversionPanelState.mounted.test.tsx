// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import type { ConversionJobController } from "./useConversionJob";
import { useConversionPanelState } from "./useConversionPanelState";
import { SourceQueueView } from "./views/SourceQueueView";
import { ConversionActionsView } from "./views/ConversionActionsView";

const hydration = vi.hoisted(() => ({ loading: false, voice: "voice", error: "", retry: vi.fn() }));
afterEach(() => { hydration.loading = false; hydration.voice = "voice"; hydration.error = ""; hydration.retry.mockReset(); });

vi.mock("./useConversionOptions", () => ({ useConversionOptions: () => ({
  loading: hydration.loading, error: hydration.error, retryRuntimeOptions: hydration.retry,
  selectedEngine: "kokoro", selectedVoice: hydration.voice, cleanerBackend: "basic",
  cleanerApiKey: "", cleanerModel: "", cleanerModelOptions: [], engineOptions: [],
  filteredVoiceOptions: [], voiceFilter: "", quantizationOptions: [],
}) }));

it.each([["Edited title", "Edited author"], ["", ""]])("restarts a finished source with metadata '%s' and '%s' while keeping the previous row", async (title, author) => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const client = { uploadSourceText: vi.fn(async () => ({
    source_handle: "upload:paste.md", original_name: "paste.md",
    preflight: { title: "Original title", author: "Original author", chapter_count: 1, chapter_titles: ["Opening"], source_type: "md",
      sample: "sample", warnings: [], size_bytes: 10, requires_ocr: false, pdf_text_layer: null },
  })) } as unknown as ApiClient;
  const startConversion = vi.fn(async () => ({ ok: true, jobId: `job-${startConversion.mock.calls.length}` }));
  const conversionJob = {
    clearFormError: vi.fn(), startConversion,
    waitForTerminalState: vi.fn(async (jobId: string) => ({ jobId, status: "completed", message: "" })),
    refreshActiveConversion: vi.fn(),
  } as unknown as ConversionJobController;
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
    await act(async () => { current.actions.setConversionMode("document_audio"); current.actions.setSourceInput("paste"); });
    await act(async () => current.actions.setPastedText("Pasted source"));
    await act(async () => { await current.actions.onAddPastedText(); });
    expect(current.sourceQueue.preflightCard).toMatchObject({ chapterCount: 1, chapterTitles: ["Opening"], sizeLabel: "1 KB" });
    expect(current.sourceQueue.queueItems[0].meta).toBe("Original author · MD · 1 chapter");
    await act(async () => { await current.actions.onStartConversion(); });
    const finished = current.sourceQueue.queueItems[0];
    expect(finished.finished).toBe(true);
    expect(startConversion).toHaveBeenLastCalledWith(expect.objectContaining({ title: "Original title", author: "Original author" }), expect.objectContaining({ signal: expect.any(AbortSignal) }));

    await act(async () => current.actions.syncSelectedQueueEdits({ title, author }));
    await act(async () => { await current.actions.onStartConversion(); });

    expect(startConversion).toHaveBeenLastCalledWith(expect.objectContaining({
      source_path: "upload:paste.md", title: title || null, author: author || null,
    }), expect.objectContaining({ signal: expect.any(AbortSignal) }));
    expect(current.sourceQueue.queueItems).toHaveLength(2);
    expect(current.sourceQueue.queueItems[0]).toEqual({ ...finished, selected: false, canMoveDown: true });
    expect(current.sourceQueue.queueItems[1].id).not.toBe(finished.id);
    expect(current.sourceQueue.queueItems[1].finished).toBe(true);
    expect(current.sourceQueue).toMatchObject({ titleOverride: title, authorOverride: author });
    await act(async () => current.actions.selectQueueItem(finished.id));
    expect(current.sourceQueue).toMatchObject({ titleOverride: "Original title", authorOverride: "Original author" });
  } finally {
    await act(async () => root.unmount());
  }
});

it.each(["single conversion", "queue conversion", "failed reconciliation"])("Clear finished retains preflight warnings and clears warnings after %s", async (scenario) => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const deleteUpload = vi.fn(async () => ({}));
  const client = { deleteUpload, getActiveConversion: vi.fn(async () => {
    if (scenario === "failed reconciliation") throw new Error("Backend disconnected");
    return { active: false, job: null };
  }), uploadSourceText: vi.fn(async () => ({
    source_handle: "upload:paste.md", original_name: "paste.md",
    preflight: { title: "Paste", author: null, chapter_count: 1, source_type: "md",
      sample: "sample", warnings: ["Short source"], size_bytes: 10, requires_ocr: false, pdf_text_layer: null },
  })) } as unknown as ApiClient;
  const conversionJob = {
    clearFormError: vi.fn(),
    startConversion: vi.fn(async () => ({ ok: true, jobId: "converted-paste" })),
    waitForTerminalState: vi.fn(async (jobId: string) => ({ jobId, status: "unknown", message: "" })),
    refreshActiveConversion: vi.fn(),
  } as unknown as ConversionJobController;
  let current!: ReturnType<typeof useConversionPanelState>;
  function Probe() {
    current = useConversionPanelState({ activeTab: "convert", hidden: false, apiClient: client,
      connectionState: "connected",
      conversionJob,
      onOpenSettings: vi.fn() });
    return <SourceQueueView sourceQueue={current.sourceQueue} capabilities={current.capabilities} actions={current.actions} />;
  }
  const container = document.createElement("div");
  const root = createRoot(container);
  try {
    await act(async () => root.render(<Probe />));
    await act(async () => { current.actions.setConversionMode("document_audio"); current.actions.setSourceInput("paste"); });
    await act(async () => current.actions.setPastedText("Pasted source"));
    await act(async () => { await current.actions.onAddPastedText(); });
    expect(current.sourceQueue.pastedText).toBe("");
    expect(current.sourceQueue.queueItems[0].finished).toBe(false);
    const findClear = () => [...container.querySelectorAll("button")].find((button) => button.textContent === "Clear finished");
    // Nothing has finished yet, so there is nothing to clear.
    expect(findClear()).toBeUndefined();
    expect(current.sourceQueue.queueCount).toBe(1);
    expect(deleteUpload).not.toHaveBeenCalled();

    await act(async () => {
      if (scenario === "single conversion") await current.actions.onStartConversion();
      else await current.actions.onRunQueue();
    });
    expect(conversionJob.waitForTerminalState).toHaveBeenCalledWith("converted-paste");
    expect(current.sourceQueue.queueItems[0].error).toBe("Could not confirm the conversion. Check Library before retrying.");
    expect(current.sourceQueue.queueItems[0].finished).toBe(true);
    const clear = findClear();
    expect(clear?.disabled).toBe(false);
    await act(async () => clear?.click());
    expect(current.sourceQueue.queueCount).toBe(0);
    expect(deleteUpload).toHaveBeenCalledExactlyOnceWith("upload:paste.md");
  } finally {
    await act(async () => root.unmount());
  }
});

it("keeps Convert enabled after repeated conversions and when finished history is selected", async () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const client = { uploadSourceText: vi.fn(async () => ({
    source_handle: `upload:paste-${client.uploadSourceText.mock.calls.length}.md`, original_name: "paste.md",
    preflight: { title: "Original title", author: null, chapter_count: 1, source_type: "md",
      sample: "sample", warnings: [], size_bytes: 10, requires_ocr: false, pdf_text_layer: null },
  })) };
  const startConversion = vi.fn(async () => ({ ok: true, jobId: `job-${startConversion.mock.calls.length}` }));
  const conversionJob = {
    clearFormError: vi.fn(), startConversion,
    waitForTerminalState: vi.fn(async (jobId: string) => ({ jobId, status: "completed", message: "" })),
    refreshActiveConversion: vi.fn(),
  } as unknown as ConversionJobController;
  let current!: ReturnType<typeof useConversionPanelState>;
  function Probe() {
    current = useConversionPanelState({ activeTab: "convert", hidden: false, apiClient: client as unknown as ApiClient,
      connectionState: "connected", conversionJob,
      onOpenSettings: vi.fn() });
    return <ConversionActionsView {...current} />;
  }
  const container = document.createElement("div");
  const root = createRoot(container);
  const convert = () => container.querySelector<HTMLButtonElement>("#start-btn")!;
  try {
    await act(async () => root.render(<Probe />));
    await act(async () => { current.actions.setConversionMode("document_audio"); current.actions.setSourceInput("paste"); });
    await act(async () => current.actions.setPastedText("Pasted source"));
    await act(async () => { await current.actions.onAddPastedText(); });
    await act(async () => convert().click());
    await act(async () => current.actions.syncSelectedQueueEdits({ title: "Second title" }));
    await act(async () => convert().click());
    expect(current.sourceQueue.queueItems).toHaveLength(2);
    await act(async () => current.actions.syncSelectedQueueEdits({ title: "Third title" }));
    expect(convert().hidden).toBe(false);
    expect(convert().disabled).toBe(false);
    await act(async () => convert().click());
    expect(startConversion).toHaveBeenLastCalledWith(expect.objectContaining({ title: "Third title" }), expect.objectContaining({ signal: expect.any(AbortSignal) }));
    expect(startConversion).toHaveBeenCalledTimes(3);

    const finishedId = current.sourceQueue.queueItems[0].id;
    for (const text of ["Next source", "Another source"]) {
      await act(async () => current.actions.setPastedText(text));
      await act(async () => { await current.actions.onAddPastedText(); });
    }
    expect(current.capabilities.showRunQueue).toBe(true);
    await act(async () => current.actions.selectQueueItem(finishedId));
    expect(current.capabilities.showRunQueue).toBe(true);
    expect(convert().hidden).toBe(false);
    expect(convert().disabled).toBe(false);
  } finally {
    await act(async () => root.unmount());
  }
});


it.each(["direct", "shortcut", "queue", "button", "queue continuation"])("blocks %s until the current runtime options are hydrated", async (command) => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  let finish!: () => void;
  const terminal = new Promise<void>((resolve) => { finish = resolve; });
  const client = { uploadSourceText: vi.fn(async () => ({
    source_handle: `upload:paste-${client.uploadSourceText.mock.calls.length}.md`, original_name: "paste.md",
    preflight: { title: "Paste", author: null, chapter_count: 1, source_type: "md",
      sample: "sample", warnings: [], size_bytes: 10, requires_ocr: false, pdf_text_layer: null },
  })) };
  const startConversion = vi.fn(async () => ({ ok: true, jobId: "job-1" }));
  const conversionJob = {
    clearFormError: vi.fn(), startConversion,
    waitForTerminalState: vi.fn(async () => { await terminal; return { status: "completed" }; }),
    refreshActiveConversion: vi.fn(),
  } as unknown as ConversionJobController;
  const startCommandRef = { current: null as (() => void) | null };
  let current!: ReturnType<typeof useConversionPanelState>;
  function Probe() {
    current = useConversionPanelState({ activeTab: "convert", hidden: false, apiClient: client as unknown as ApiClient,
      runtimeSelectionGeneration: hydration.loading ? 1 : 0,
      connectionState: "connected", conversionJob, startCommandRef,
      onOpenSettings: vi.fn() });
    return <ConversionActionsView {...current} />;
  }
  const container = document.createElement("div");
  const root = createRoot(container);
  let running: Promise<void> | undefined;
  try {
    await act(async () => root.render(<Probe />));
    await act(async () => { current.actions.setConversionMode("document_audio"); current.actions.setSourceInput("paste"); });
    for (let index = 0; index < 2; index += 1) {
      await act(async () => current.actions.setPastedText("Pasted source"));
      await act(async () => { await current.actions.onAddPastedText(); });
    }
    if (command === "queue continuation") {
      await act(async () => { running = current.actions.onRunQueue(); });
      expect(startConversion).toHaveBeenCalledTimes(1);
    }
    hydration.loading = true;
    await act(async () => root.render(<Probe />));
    expect(current.capabilities.canStartConversion).toBe(false);
    expect(current.capabilities.canRunQueue).toBe(false);
    await act(async () => {
      finish();
      if (command === "direct") await current.actions.onStartConversion();
      if (command === "shortcut") startCommandRef.current?.();
      if (command === "queue") await current.actions.onRunQueue();
      if (command === "button") container.querySelector<HTMLButtonElement>("#start-btn")?.click();
      await running;
    });
    expect(startConversion).toHaveBeenCalledTimes(command === "queue continuation" ? 1 : 0);
    expect(current.sourceQueue.queueItems.at(-1)?.statusText).toBe("Ready");
    hydration.loading = false;
    await act(async () => root.render(<Probe />));
    await act(async () => { await current.actions.onRunQueue(); });
    expect(startConversion).toHaveBeenCalledTimes(2);
  } finally {
    finish();
    await act(async () => root.unmount());
  }
});


it.each([false, true])("waits for current runtime options when a pasted source finishes uploading with hydration complete: %s", async (hydrated) => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  let finishUpload!: () => void;
  const upload = new Promise<void>((resolve) => { finishUpload = resolve; });
  const client = { uploadSourceText: vi.fn(async () => {
    await upload;
    return { source_handle: "upload:paste.md", original_name: "paste.md",
      preflight: { title: "Paste", author: null, chapter_count: 1, source_type: "md",
        sample: "sample", warnings: [], size_bytes: 10, requires_ocr: false, pdf_text_layer: null } };
  }) };
  const startConversion = vi.fn(async () => ({ ok: true, jobId: "job-1" }));
  const conversionJob = { clearFormError: vi.fn(), startConversion,
    waitForTerminalState: vi.fn(async () => ({ status: "completed" })),
  } as unknown as ConversionJobController;
  let current!: ReturnType<typeof useConversionPanelState>;
  let generation = 0;
  function Probe() {
    current = useConversionPanelState({ activeTab: "convert", hidden: false, apiClient: client as unknown as ApiClient,
      runtimeSelectionGeneration: generation, connectionState: "connected",
      conversionJob, onOpenSettings: vi.fn() });
    return null;
  }
  const root = createRoot(document.createElement("div"));
  try {
    await act(async () => root.render(<Probe />));
    await act(async () => { current.actions.setConversionMode("document_audio"); current.actions.setSourceInput("paste"); });
    await act(async () => current.actions.setPastedText("Pasted source"));
    let starting!: Promise<void>;
    await act(async () => { starting = current.actions.onStartConversion(); });
    generation += 1;
    hydration.loading = true;
    await act(async () => root.render(<Probe />));
    hydration.loading = !hydrated;
    hydration.voice = "new-voice";
    await act(async () => root.render(<Probe />));
    await act(async () => { finishUpload(); await starting; });
    if (hydrated) {
      expect(startConversion).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
        runtime_options: expect.objectContaining({ voice: "new-voice" }),
      }), expect.objectContaining({ signal: expect.any(AbortSignal) }));
    } else {
      expect(startConversion).not.toHaveBeenCalled();
      expect(current.sourceQueue.queueItems[0].statusText).toBe("Ready");
    }
  } finally {
    finishUpload();
    await act(async () => root.unmount());
  }
});


it("shows a working runtime retry action while conversion is blocked by bootstrap failure", async () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  hydration.error = "request timed out";
  let current!: ReturnType<typeof useConversionPanelState>;
  const client = {} as ApiClient;
  const conversionJob = {} as ConversionJobController;
  function Probe() {
    current = useConversionPanelState({ activeTab: "convert", hidden: false, apiClient: client,
      connectionState: "connected", conversionJob,
      onOpenSettings: vi.fn() });
    return <ConversionActionsView {...current} />;
  }
  const container = document.createElement("div");
  const root = createRoot(container);
  try {
    await act(async () => root.render(<Probe />));
    const retryButton = container.querySelector<HTMLButtonElement>("#retry-runtime-options-btn") ?? undefined;
    expect(retryButton).toBeDefined();
    expect(current.capabilities.canStartConversion).toBe(false);
    expect(current.conversionActions.blockingReason).toBe("Voices could not load.");
    expect(container.textContent).toContain("Voices could not load.");
    await act(async () => retryButton!.click());
    expect(hydration.retry).toHaveBeenCalledTimes(1);
    hydration.error = "";
    await act(async () => root.render(<Probe />));
    expect(container.querySelector("#retry-runtime-options-btn")).toBeNull();
  } finally {
    await act(async () => root.unmount());
  }
});

it("lets the visible Cancel action stop a cleanup retry before another conversion starts", async () => {
  vi.useFakeTimers();
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const client = { uploadSourceText: vi.fn(async () => ({
    source_handle: "upload:paste.md", original_name: "paste.md",
    preflight: { title: "Paste", author: null, chapter_count: 1, source_type: "md",
      sample: "sample", warnings: [], size_bytes: 10, requires_ocr: false, pdf_text_layer: null },
  })) } as unknown as ApiClient;
  const conversionJob = {
    clearFormError: vi.fn(), startConversion: vi.fn().mockResolvedValue({ ok: false, status: 409,
      serverCode: "conversion_cleanup_pending", error: "Previous conversion cleanup pending" }),
    cancelActiveConversion: vi.fn(async () => {}), refreshActiveConversion: vi.fn(),
  } as unknown as ConversionJobController;
  let current!: ReturnType<typeof useConversionPanelState>;
  function Probe() {
    current = useConversionPanelState({ activeTab: "convert", hidden: false, apiClient: client,
      connectionState: "connected", conversionJob,
      onOpenSettings: vi.fn() });
    return <ConversionActionsView {...current} />;
  }
  const container = document.createElement("div");
  const root = createRoot(container);
  try {
    await act(async () => root.render(<Probe />));
    await act(async () => { current.actions.setConversionMode("document_audio"); current.actions.setSourceInput("paste"); });
    await act(async () => current.actions.setPastedText("Pasted source"));
    await act(async () => current.actions.onAddPastedText());
    let run!: Promise<void>;
    await act(async () => { run = current.actions.onRunQueue(); });
    const cancelButton = container.querySelector<HTMLButtonElement>("#cancel-btn");
    expect(cancelButton?.disabled).toBe(false);
    // Cancel acts once a click can no longer be the tail of the Start gesture;
    // the first cleanup retry has gone out by then.
    await act(async () => vi.advanceTimersByTimeAsync(600));
    expect(conversionJob.startConversion).toHaveBeenCalledTimes(2);
    await act(async () => { cancelButton!.click(); await run; });
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(conversionJob.startConversion).toHaveBeenCalledTimes(2);
    expect(conversionJob.cancelActiveConversion).toHaveBeenCalledTimes(1);
    expect(current.sourceQueue.queueItems[0].statusClassName).toContain("status-warning");
    expect(current.sourceQueue.queueRunning).toBe(false);
    expect(current.sourceQueue.queueStatus).toBe("Queue cancelled.");
  } finally {
    await act(async () => root.unmount());
    vi.useRealTimers();
  }
});


it.each(["checking", "unconfirmed"])("shows Refresh for a %s conversion start without a job id", async (state) => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const conversionJob = { isCheckingStart: state === "checking", activeJobId: null, clearFormError: vi.fn(),
    startReconciliation: state === "unconfirmed" ? { clientRequestId: "request", result: { ok: false, error: "Could not confirm" } } : null,
    refreshActiveConversion: vi.fn() } as unknown as ConversionJobController;
  const client = {} as ApiClient;
  const container = document.createElement("div");
  const root = createRoot(container);
  function Probe() {
    const state = useConversionPanelState({ activeTab: "convert", hidden: false, apiClient: client,
      connectionState: "connected", conversionJob,
      onOpenSettings: vi.fn() });
    return <ConversionActionsView sourceQueue={state.sourceQueue} capabilities={state.capabilities}
      conversionActions={state.conversionActions} actions={state.actions} />;
  }
  try {
    await act(async () => root.render(<Probe />));
    expect(container.querySelector<HTMLButtonElement>("#refresh-btn")?.disabled).toBe(false);
    await act(async () => container.querySelector<HTMLButtonElement>("#refresh-btn")?.click());
    expect(conversionJob.refreshActiveConversion).toHaveBeenCalledOnce();
  } finally {
    await act(async () => root.unmount());
  }
});
