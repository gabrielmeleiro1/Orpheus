// @vitest-environment happy-dom

import { act, useCallback, useRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import { createApiClient } from "../../api/client";
import { createConversionApi } from "../../api/resources/conversion";
import type { ConversionJobTerminalEvent, DesktopEventPayload, StartConversionRequest } from "../../types/api";
import { useTerminalStateWaiters } from "../events/useTerminalStateWaiters";
import { createQueueItem } from "./conversionPanelModel";
import type { ConversionJobController } from "./useConversionJob";
import { useConversionQueueController } from "./useConversionQueueController";
import { useConversionQueueRunner } from "./useConversionQueueRunner";
import { useConversionPreview } from "./useConversionPreview";
import { useCleanerOptions } from "./useCleanerOptions";
import { resolveConversionBlocker } from "./useConversionPanelState";
import type { PersistedSettings } from "./conversionOptionsShared";
import type { useConversionOptions } from "./useConversionOptions";
import { SourceQueueView } from "./views/SourceQueueView";
import type { ConversionPanelActions, SourceQueueViewModel } from "./conversionPanelStateTypes";

let root: Root;
let container: HTMLDivElement;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.useFakeTimers();
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

async function mount<T>(hook: () => T) {
  let value: T;
  function Probe() { value = hook(); return null; }
  await act(async () => root.render(<Probe />));
  return { get current() { return value!; }, rerender: () => act(async () => root.render(<Probe />)) };
}

function jobController(overrides: Partial<ConversionJobController> = {}): ConversionJobController {
  return {
    startConversion: vi.fn(async () => ({ ok: true, jobId: "job" })),
    waitForTerminalState: vi.fn(async (jobId) => ({ jobId, status: "completed", message: "" })),
    refreshActiveConversion: vi.fn(),
    ...overrides,
  } as unknown as ConversionJobController;
}

const payload = (item?: ReturnType<typeof createQueueItem>): StartConversionRequest => ({
  source_path: item?.sourcePath ?? "book.txt", conversion_mode: "audiobook", cleaner_backend: "basic",
});

it("keeps observing the same running job for hours and resolves its eventual terminal state", async () => {
  const getActiveConversion = vi.fn(async () => ({ active: true, job: { id: "long-book", status: "running" } }));
  const client = { getActiveConversion } as unknown as ApiClient;
  let resolveEvent: ReturnType<typeof useTerminalStateWaiters>["resolveTerminalState"];
  const hook = await mount(() => {
    const onTerminalStatus = useCallback((event: DesktopEventPayload) => {
      resolveEvent({ jobId: String(event.job_id), status: "completed", message: "" });
    }, []);
    const waiters = useTerminalStateWaiters({ apiClient: client, onTerminalStatus });
    resolveEvent = waiters.resolveTerminalState;
    return waiters;
  });
  const finished = vi.fn();
  const promise = hook.current.waitForTerminalState("long-book").then(finished);
  await act(async () => { await vi.advanceTimersByTimeAsync(3 * 60 * 60 * 1000); });
  expect(finished).not.toHaveBeenCalled();
  getActiveConversion.mockResolvedValue({ active: false, job: { id: "long-book", status: "completed" } });
  await act(async () => { await vi.advanceTimersByTimeAsync(1500); await promise; });
  expect(finished).toHaveBeenCalledWith(expect.objectContaining({ status: "completed" }));
});

it.each(["completed", "cancelled"])("reconciles an unknown result and waits for backend %s", async (terminalStatus) => {
  const first = createQueueItem("first.txt", "audiobook");
  const second = createQueueItem("second.txt", "audiobook");
  first.status = second.status = "ready";
  const getActiveConversion = vi.fn(async () => ({ active: true, job: { id: "job", status: "running" } }));
  const client = { getActiveConversion } as unknown as ApiClient;
  const job = jobController({ waitForTerminalState: vi.fn(async (jobId) => ({ jobId, status: "unknown" as const, message: "" })) });
  const hook = await mount(() => useConversionQueueRunner({ runtimeOptionsReady: true, apiClient: client, conversionJob: job,
    queueItems: [first, second], selectedQueueItem: first, buildStartPayload: payload, addQueueItems: vi.fn(),
    updateQueueItem: vi.fn(), syncSelectedQueueEdits: vi.fn(), setQueueStatus: vi.fn() }));
  let running: Promise<void>;
  await act(async () => { running = hook.current.onRunQueue(); });
  await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
  expect(job.startConversion).toHaveBeenCalledTimes(1);
  getActiveConversion.mockResolvedValue({ active: false, job: { id: "job", status: terminalStatus } });
  await act(async () => { await vi.advanceTimersByTimeAsync(1500); await running; });
  // A cancelled book stops only that book; the queue goes on to the next one.
  expect(job.startConversion).toHaveBeenCalledTimes(2);
});

it("pauses the queue if reconciliation cannot reach the backend", async () => {
  const items = ["first.txt", "second.txt"].map((path) => ({ ...createQueueItem(path, "audiobook"), status: "ready" as const }));
  const job = jobController({ waitForTerminalState: vi.fn(async (jobId) => ({ jobId, status: "unknown" as const, message: "" })) });
  const status = vi.fn();
  const hook = await mount(() => useConversionQueueRunner({ runtimeOptionsReady: true,
    apiClient: { getActiveConversion: vi.fn(async () => { throw new Error("offline"); }) } as unknown as ApiClient,
    conversionJob: job, queueItems: items, selectedQueueItem: items[0], buildStartPayload: payload, addQueueItems: vi.fn(),
    updateQueueItem: vi.fn(), syncSelectedQueueEdits: vi.fn(), setQueueStatus: status,
  }));
  await act(async () => hook.current.onRunQueue());
  expect(job.startConversion).toHaveBeenCalledTimes(1);
  expect(status).toHaveBeenLastCalledWith(expect.stringContaining("Queue paused"));
});

it("releases queue commands when reconciliation times out and the final refresh hangs", async () => {
  vi.spyOn(globalThis, "fetch").mockImplementation((_input, init) => new Promise((_resolve, reject) => {
    init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  }));
  let items = ["first.txt", "second.txt"].map((path) => ({ ...createQueueItem(path, "audiobook"), status: "ready" as const }));
  const job = jobController({
    waitForTerminalState: vi.fn(async (jobId) => ({ jobId, status: "unknown" as const, message: "" })),
    refreshActiveConversion: vi.fn(() => new Promise<void>(() => {})),
  });
  const status = vi.fn();
  const client = createApiClient({ baseUrl: "http://desktop.test" });
  const hook = await mount(() => useConversionQueueRunner({ runtimeOptionsReady: true,
    apiClient: client, conversionJob: job, queueItems: items, selectedQueueItem: items[0],
    buildStartPayload: payload, addQueueItems: vi.fn(), updateQueueItem: vi.fn(),
    syncSelectedQueueEdits: vi.fn(), setQueueStatus: status,
  }));
  const settled = vi.fn();
  await act(async () => { void hook.current.onRunQueue().then(settled); });
  expect(hook.current.queueRunning).toBe(true);
  await act(async () => { await vi.advanceTimersByTimeAsync(16_000); });
  expect(job.refreshActiveConversion).toHaveBeenCalledOnce();
  expect(status).toHaveBeenLastCalledWith(expect.stringContaining("Queue paused"));
  expect(hook.current.queueRunning).toBe(false);
  expect(settled).toHaveBeenCalledOnce();
  expect(job.startConversion).toHaveBeenCalledOnce();

  // A new command must reach the empty-queue check after the user clears it.
  items = [];
  await hook.rerender();
  await act(async () => hook.current.onRunQueue());
  expect(status).toHaveBeenLastCalledWith("No queued items to convert.");
  expect(job.startConversion).toHaveBeenCalledOnce();
});

it("removes a waiting upload during a run and does not submit the removed row", async () => {
  const deleteUpload = vi.fn(async () => ({ deleted: true }));
  const client = { deleteUpload } as unknown as ApiClient;
  let finish!: (value: { jobId: string; status: "completed"; message: string }) => void;
  const job = jobController({ waitForTerminalState: vi.fn(() => new Promise<ConversionJobTerminalEvent>((resolve) => { finish = resolve; })) });
  const hook = await mount(() => {
    const queue = useConversionQueueController({ apiClient: client });
    const runner = useConversionQueueRunner({ runtimeOptionsReady: true, apiClient: client, conversionJob: job,
      queueItems: queue.queueItems, selectedQueueItem: queue.selectedQueueItem, buildStartPayload: payload, addQueueItems: vi.fn(),
      updateQueueItem: queue.updateQueueItem, syncSelectedQueueEdits: queue.syncSelectedQueueEdits, setQueueStatus: queue.setQueueStatus });
    return { queue, runner };
  });
  const first = { ...createQueueItem("first.txt", "audiobook"), status: "ready" as const };
  const second = { ...createQueueItem("upload:second.txt", "audiobook"), status: "ready" as const };
  await act(async () => { hook.current.queue.addPastedSource(first); hook.current.queue.addPastedSource(second); });
  let running: Promise<void>;
  await act(async () => { running = hook.current.runner.onRunQueue(); });
  await act(async () => { hook.current.queue.removeQueueItem(first.id); hook.current.queue.removeQueueItem(second.id); });
  expect(hook.current.queue.queueItems.map((item) => item.id)).toEqual([first.id]);
  expect(deleteUpload).toHaveBeenCalledWith("upload:second.txt");
  await act(async () => { finish({ jobId: "job", status: "completed", message: "" }); await running; });
  expect(job.startConversion).toHaveBeenCalledTimes(1);
});

it("Clear finished removes only finished rows", async () => {
  const removeQueueItem = vi.fn();
  const rows = ["done", "failed", "cancelled", "warning", "ready", "converting"].map((status) => ({
    id: status, title: status, statusText: status, statusClassName: "", selected: false,
    converting: status === "converting", finished: ["done", "failed", "cancelled", "warning"].includes(status),
    canMoveUp: status !== "converting", canMoveDown: status !== "converting", canRemove: status !== "converting",
  }));
  const sourceQueue = { sourcePath: "", preflightCard: null, pasteStatus: "Idle", queueItems: rows, queueCount: rows.length } as SourceQueueViewModel;
  await act(async () => root.render(<SourceQueueView sourceQueue={sourceQueue} capabilities={{} as never}
    actions={{ removeQueueItem } as unknown as ConversionPanelActions} />));
  const clear = [...container.querySelectorAll("button")].find((button) => button.textContent === "Clear finished")!;
  await act(async () => clear.click());
  expect(removeQueueItem.mock.calls.flat()).toEqual(["done", "failed", "cancelled", "warning"]);
  const removeButtons = [...container.querySelectorAll<HTMLButtonElement>('button[aria-label="Remove"]')];
  expect(removeButtons).toHaveLength(rows.length);
  expect(removeButtons.every((button) => button.dataset.tip === "Remove")).toBe(true);
  expect(removeButtons[4].disabled).toBe(false);
  expect(removeButtons[5].disabled).toBe(true);
});

it("uses the selected model and session key for recommendation, and explains unavailable configuration", async () => {
  const requestJson = vi.fn(async () => ({ voice_suggestion: { recommended_voice_id: "voice" } }));
  const client = createConversionApi(requestJson as never) as unknown as ApiClient;
  let options = { cleanerBackend: "basic", cleanerApiKey: "", cleanerModel: "", selectedEngine: "kokoro", setSelectedVoice: vi.fn() } as unknown as ReturnType<typeof useConversionOptions>;
  const hook = await mount(() => useConversionPreview({ apiClient: client, optionState: options, sourcePath: "book.txt", selectedPreflight: null, sampleText: null }));
  expect(hook.current.recommendationUnavailableReason).toContain("Settings");
  await act(async () => hook.current.onRecommendVoice());
  expect(requestJson).not.toHaveBeenCalled();
  options = { ...options, cleanerBackend: "api", cleanerApiKey: "session-key", cleanerModel: "chosen-model" };
  await hook.rerender();
  await act(async () => hook.current.onRecommendVoice());
  expect(requestJson).toHaveBeenCalledWith("/api/voices/suggest", expect.objectContaining({
    body: JSON.stringify({ source_path: "book.txt", tts_engine: "kokoro", cleaner_backend: "api", cleaner_api_model: "chosen-model", cleaner_api_key: "session-key" }),
  }));
});


it("mounts Basic cleanup as ready without a runtime probe, model or key", async () => {
  const client = {} as ApiClient;
  const models = { tts_engines: [], cleaner_models: [], api_cleaner_models: [], cleaner_backend_options: [
    { name: "basic", display_name: "Basic cleanup", is_local: true, requires_model: false, requires_api_key: false, models: [] },
  ] };
  const hook = await mount(() => {
    const persistedSettingsRef = useRef<PersistedSettings>({ cleaner_backend: "basic", cleaner_backend_configured: true });
    return useCleanerOptions({ apiClient: client, modelOptions: models, systemCapabilities: null, persistedSettingsRef });
  });
  expect(hook.current.cleanerBackend).toBe("basic");
  expect(hook.current.cleanerRuntimeError).toBe(false);
  expect(hook.current.cleanerModelOptions).toEqual([]);
  expect(resolveConversionBlocker({ apiClientAvailable: true, sourcePath: "book.txt", hasPastedText: false,
    selectedEngine: "kokoro", selectedVoice: "voice", cleanerRuntimeError: hook.current.cleanerRuntimeError,
    cleanerRequiresApiKey: hook.current.selectedCleanerBackend!.requires_api_key, cleanerApiKey: "",
    cleanerDisplayName: "Basic cleanup", cleanerModelOptionsCount: 0, cleanerModel: "",
  }).blockingReason).toBeNull();
});


it("observes reordering of waiting rows while keeping the running row fixed", async () => {
  const client = {} as ApiClient;
  let finish!: (value: ConversionJobTerminalEvent) => void;
  let waits = 0;
  const job = jobController({ waitForTerminalState: vi.fn((jobId) => ++waits === 1
    ? new Promise<ConversionJobTerminalEvent>((resolve) => { finish = resolve; })
    : Promise.resolve({ jobId, status: "completed" as const, message: "" })) });
  const hook = await mount(() => {
    const queue = useConversionQueueController({ apiClient: client });
    const runner = useConversionQueueRunner({ runtimeOptionsReady: true, apiClient: client, conversionJob: job,
      queueItems: queue.queueItems, selectedQueueItem: queue.selectedQueueItem, buildStartPayload: payload, addQueueItems: vi.fn(),
      updateQueueItem: queue.updateQueueItem, syncSelectedQueueEdits: queue.syncSelectedQueueEdits, setQueueStatus: queue.setQueueStatus });
    return { queue, runner };
  });
  const items = ["first.txt", "second.txt", "third.txt"].map((path) => ({ ...createQueueItem(path, "audiobook"), status: "ready" as const }));
  await act(async () => { items.forEach((item) => hook.current.queue.addPastedSource(item)); });
  let running: Promise<void>;
  await act(async () => { running = hook.current.runner.onRunQueue(); });
  await act(async () => { hook.current.queue.moveQueueItem(items[0].id, 1); hook.current.queue.moveQueueItem(items[2].id, -1); });
  expect(hook.current.queue.queueItems.map((item) => item.sourcePath)).toEqual(["first.txt", "third.txt", "second.txt"]);
  await act(async () => { finish({ jobId: "job", status: "completed", message: "" }); await running; });
  expect(vi.mocked(job.startConversion).mock.calls.map(([request]) => request.source_path)).toEqual(["first.txt", "third.txt", "second.txt"]);
});

it("reports unknown immediately when no backend client can observe the job", async () => {
  const hook = await mount(() => useTerminalStateWaiters({ apiClient: null, onTerminalStatus: vi.fn() }));
  await expect(hook.current.waitForTerminalState("job")).resolves.toMatchObject({ status: "unknown" });
});

it.each(["success", "failure"])("inspection %s cannot release an active row for removal", async (outcome) => {
  let finishInspection!: (value: unknown) => void;
  let failInspection!: (error: Error) => void;
  const client = { deleteUpload: vi.fn(), preflightSource: vi.fn(() => new Promise((resolve, reject) => { finishInspection = resolve; failInspection = reject; })) } as unknown as ApiClient;
  const hook = await mount(() => useConversionQueueController({ apiClient: client }));
  const item = { ...createQueueItem("upload:active.txt", "audiobook"), status: "converting" as const, jobId: "active" };
  await act(async () => hook.current.addPastedSource(item, { selectItem: true }));
  let inspection: Promise<void>;
  await act(async () => { inspection = hook.current.runPreflightForItem(item); });
  expect(hook.current.queueItems[0].status).toBe("converting");
  await act(async () => hook.current.removeQueueItem(item.id));
  expect(hook.current.queueItems).toHaveLength(1);
  expect(client.deleteUpload).not.toHaveBeenCalled();
  await act(async () => {
    if (outcome === "success") finishInspection({ chapter_count: 1, warnings: [] });
    else failInspection(new Error("Inspection failed"));
    await inspection;
  });
  expect(hook.current.queueItems[0].status).toBe("converting");
});

it.each(["blank", "edited", "preflighting", "failed"] as const)("skips a waiting row made %s during conversion", async (change) => {
  const client = {} as ApiClient;
  let finish!: (value: ConversionJobTerminalEvent) => void;
  const job = jobController({ waitForTerminalState: vi.fn().mockImplementationOnce(() => new Promise<ConversionJobTerminalEvent>((resolve) => { finish = resolve; }))
    .mockResolvedValue({ jobId: "job", status: "completed", message: "" }) });
  const hook = await mount(() => {
    const queue = useConversionQueueController({ apiClient: client });
    const runner = useConversionQueueRunner({ runtimeOptionsReady: true, apiClient: client, conversionJob: job, queueItems: queue.queueItems,
      selectedQueueItem: queue.selectedQueueItem, buildStartPayload: payload, addQueueItems: vi.fn(), updateQueueItem: queue.updateQueueItem,
      syncSelectedQueueEdits: queue.syncSelectedQueueEdits, setQueueStatus: queue.setQueueStatus });
    return { queue, runner };
  });
  const items = ["a.txt", "b.txt", "c.txt"].map((path) => ({ ...createQueueItem(path, "audiobook"), status: "ready" as const }));
  await act(async () => items.forEach((item) => hook.current.queue.addPastedSource(item)));
  let running: Promise<void>;
  await act(async () => { running = hook.current.runner.onRunQueue(); });
  await act(async () => hook.current.queue.applySelectedQueueItem(items[1]));
  await act(async () => {
    if (change === "blank" || change === "edited") hook.current.queue.syncSelectedQueueEdits({ sourcePath: change === "blank" ? "" : "new.txt" });
    else hook.current.queue.updateQueueItem(items[1].id, (item) => ({ ...item, status: change }));
  });
  await act(async () => { finish({ jobId: "job", status: "completed", message: "" }); await running; });
  expect(vi.mocked(job.startConversion).mock.calls.map(([request]) => request.source_path)).toEqual(["a.txt", "c.txt"]);
  expect(hook.current.queue.queueItems[1].jobId).toBeNull();
  expect(hook.current.queue.queueItems[2].status).toBe("done");
});

it("retires the previous queue on client replacement and accepts a fresh command", async () => {
  let finish!: (value: ConversionJobTerminalEvent) => void;
  const oldJob = jobController({ waitForTerminalState: vi.fn(() => new Promise<ConversionJobTerminalEvent>((resolve) => { finish = resolve; })) });
  const newJob = jobController();
  let job = oldJob;
  let client = {} as ApiClient;
  const items = ["a.txt", "b.txt"].map((path) => ({ ...createQueueItem(path, "audiobook"), status: "ready" as const }));
  const hook = await mount(() => {
    const queue = useConversionQueueController({ apiClient: client });
    const runner = useConversionQueueRunner({ runtimeOptionsReady: true, apiClient: client, conversionJob: job,
      queueItems: queue.queueItems, selectedQueueItem: queue.selectedQueueItem, buildStartPayload: payload,
      addQueueItems: vi.fn(), updateQueueItem: queue.updateQueueItem, syncSelectedQueueEdits: queue.syncSelectedQueueEdits,
      setQueueStatus: queue.setQueueStatus });
    return { queue, runner };
  });
  await act(async () => items.forEach((item) => hook.current.queue.addPastedSource(item)));
  let running!: Promise<void>;
  await act(async () => { running = hook.current.runner.onRunQueue(); });
  job = newJob;
  client = { getActiveConversion: vi.fn(async () => ({ active: false, job: null })) } as unknown as ApiClient;
  await hook.rerender();
  expect(hook.current.runner.queueRunning).toBe(false);
  expect(hook.current.queue.queueItems[0].status).toBe("warning");
  await act(async () => { finish({ jobId: "job", status: "unknown", message: "" }); await running; });
  expect(client.getActiveConversion).not.toHaveBeenCalled();
  expect(oldJob.startConversion).toHaveBeenCalledTimes(1);
  expect(newJob.startConversion).not.toHaveBeenCalled();
  await act(async () => hook.current.runner.onStartConversion(hook.current.queue.queueItems[1]));
  expect(newJob.startConversion).toHaveBeenCalledWith(expect.objectContaining({ source_path: "b.txt" }), expect.objectContaining({ signal: expect.any(AbortSignal) }));
  expect(hook.current.queue.queueItems[1].status).toBe("done");
});
