import { renderToString } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { StartConversionRequest } from "../../types/api";
import type { ConversionTerminalJobStatus } from "../../types/conversion";
import { createQueueItem } from "./conversionPanelModel";
import { useConversionQueueRunner } from "./useConversionQueueRunner";
import type { ConversionJobController, StartConversionResult } from "./useConversionJob";

function payload(sourcePath = "/tmp/book.epub"): StartConversionRequest {
  return {
    source_path: sourcePath,
    conversion_mode: "audiobook",
    runtime_options: {
      voice: "voice",
      speed: 1,
      tts_engine: "engine",
    },
  };
}

function createConversionJobMock(
  overrides: Partial<ConversionJobController> = {},
): ConversionJobController {
  return {
    activeJobId: null,
    activeJobStatus: "idle",
    uiState: "idle",
    events: [],
    isCancelling: false,
    cancelStatus: "",
    cancelActiveConversion: vi.fn(),
    refreshActiveConversionState: vi.fn(),
    isStarting: false,
    formError: "",
    clearFormError: vi.fn(),
    startConversion: vi.fn(async () => ({ ok: true, jobId: "job-1" })),
    refreshActiveConversion: vi.fn(),
    waitForTerminalState: vi.fn(async (jobId: string) => ({
      jobId,
      status: "completed",
      message: "",
    })),
    ...overrides,
  } as ConversionJobController;
}

function renderRunner(
  args: Parameters<typeof useConversionQueueRunner>[0],
): {
  result: { current: ReturnType<typeof useConversionQueueRunner> | null };
  cleanup: () => void;
} {
  const result: { current: ReturnType<typeof useConversionQueueRunner> | null } = {
    current: null,
  };

  function TestComponent() {
    result.current = useConversionQueueRunner(args);
    return null;
  }

  renderToString(<TestComponent />);

  return {
    result,
    cleanup: () => {},
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useConversionQueueRunner", () => {
  it("does not submit a ready queue row whose source path is blank", async () => {
    const item = createQueueItem("", "audiobook");
    item.status = "ready";
    const conversionJob = createConversionJobMock();
    const setQueueStatus = vi.fn();
    const { result, cleanup } = renderRunner({
      apiClient: {} as never,
      conversionJob,
      queueItems: [item],
      selectedQueueItem: item,
      buildStartPayload: () => payload(""),
      updateQueueItem: vi.fn(),
      syncSelectedQueueEdits: vi.fn(),
      setQueueStatus,
    });

    await result.current?.onRunQueue();

    expect(conversionJob.startConversion).not.toHaveBeenCalled();
    expect(setQueueStatus).toHaveBeenLastCalledWith("No queued items to convert.");
    cleanup();
  });

  it("uses startConversion error result for queued item failures", async () => {
    const item = createQueueItem("/tmp/book.epub", "audiobook");
    item.status = "ready";
    const conversionJob = createConversionJobMock({
      formError: "stale form error",
      startConversion: vi.fn(async (): Promise<StartConversionResult> => ({
        ok: false,
        error: "fresh start error",
      })),
    });
    const updateQueueItem = vi.fn((itemId, updater) => {
      expect(itemId).toBe(item.id);
      updater(item);
    });
    const setQueueStatus = vi.fn();
    const { result, cleanup } = renderRunner({
      apiClient: {} as never,
      conversionJob,
      queueItems: [item],
      selectedQueueItem: item,
      buildStartPayload: () => payload(),
      updateQueueItem,
      syncSelectedQueueEdits: vi.fn(),
      setQueueStatus,
    });

    await result.current?.onRunQueue();

    expect(setQueueStatus).toHaveBeenLastCalledWith("fresh start error");
    expect(conversionJob.waitForTerminalState).not.toHaveBeenCalled();
    cleanup();
  });

  it("transitions the selected queue item to done on a successful single start", async () => {
    const item = createQueueItem("/tmp/book.epub", "audiobook");
    item.status = "ready";
    const conversionJob = createConversionJobMock({
      startConversion: vi.fn(async (): Promise<StartConversionResult> => ({
        ok: true,
        jobId: "job-7",
      })),
      waitForTerminalState: vi.fn(async (jobId: string) => ({
        jobId,
        status: "completed" as const,
        message: "",
      })),
    });
    const statuses: string[] = [];
    const updateQueueItem = vi.fn((itemId, updater) => {
      expect(itemId).toBe(item.id);
      const nextItem = updater(item);
      statuses.push(nextItem.status);
    });
    const { result, cleanup } = renderRunner({
      apiClient: {} as never,
      conversionJob,
      queueItems: [item],
      selectedQueueItem: item,
      buildStartPayload: () => payload(),
      updateQueueItem,
      syncSelectedQueueEdits: vi.fn(),
      setQueueStatus: vi.fn(),
    });

    await result.current?.onStartConversion();

    expect(conversionJob.waitForTerminalState).toHaveBeenCalledWith("job-7");
    expect(statuses).toEqual(["converting", "converting", "done"]);
    cleanup();
  });

  it("starts a passed item (just-queued paste) even when nothing is selected", async () => {
    const pastedItem = createQueueItem("upload:1_hash_paste.md", "document_audio");
    pastedItem.status = "ready";
    const conversionJob = createConversionJobMock();
    const buildStartPayload = vi.fn((item?: typeof pastedItem) => payload(item?.sourcePath));
    const statuses: string[] = [];
    const updateQueueItem = vi.fn((itemId, updater) => {
      expect(itemId).toBe(pastedItem.id);
      statuses.push(updater(pastedItem).status);
    });
    const syncSelectedQueueEdits = vi.fn();
    const { result, cleanup } = renderRunner({
      apiClient: {} as never,
      conversionJob,
      queueItems: [pastedItem],
      selectedQueueItem: null,
      buildStartPayload,
      updateQueueItem,
      syncSelectedQueueEdits,
      setQueueStatus: vi.fn(),
    });

    await result.current?.onStartConversion(pastedItem);

    expect(buildStartPayload).toHaveBeenCalledWith(pastedItem);
    expect(conversionJob.startConversion).toHaveBeenCalledWith(
      expect.objectContaining({ source_path: "upload:1_hash_paste.md" }),
    );
    expect(syncSelectedQueueEdits).not.toHaveBeenCalled();
    expect(statuses).toEqual(["converting", "converting", "done"]);
    cleanup();
  });

  it("marks the selected queue item failed when a single start fails to launch", async () => {
    const item = createQueueItem("/tmp/book.epub", "audiobook");
    item.status = "ready";
    const conversionJob = createConversionJobMock({
      startConversion: vi.fn(async (): Promise<StartConversionResult> => ({
        ok: false,
        error: "backend rejected start",
      })),
    });
    const statuses: string[] = [];
    const errors: string[] = [];
    const updateQueueItem = vi.fn((itemId, updater) => {
      expect(itemId).toBe(item.id);
      const nextItem = updater(item);
      statuses.push(nextItem.status);
      errors.push(nextItem.error);
    });
    const { result, cleanup } = renderRunner({
      apiClient: {} as never,
      conversionJob,
      queueItems: [item],
      selectedQueueItem: item,
      buildStartPayload: () => payload(),
      updateQueueItem,
      syncSelectedQueueEdits: vi.fn(),
      setQueueStatus: vi.fn(),
    });

    await result.current?.onStartConversion();

    expect(conversionJob.waitForTerminalState).not.toHaveBeenCalled();
    expect(statuses).toEqual(["converting", "failed"]);
    expect(errors[errors.length - 1]).toBe("backend rejected start");
    cleanup();
  });

  it("prevents concurrent direct start commands", async () => {
    const item = createQueueItem("/tmp/book.epub", "audiobook");
    let resolveStart!: (value: StartConversionResult) => void;
    const conversionJob = createConversionJobMock({
      startConversion: vi.fn(
        (): Promise<StartConversionResult> =>
          new Promise((resolve: (value: StartConversionResult) => void) => {
            resolveStart = resolve;
          }),
      ),
    });
    const { result, cleanup } = renderRunner({
      apiClient: {} as never,
      conversionJob,
      queueItems: [item],
      selectedQueueItem: item,
      buildStartPayload: () => payload(),
      updateQueueItem: vi.fn(),
      syncSelectedQueueEdits: vi.fn(),
      setQueueStatus: vi.fn(),
    });

    const first = result.current?.onStartConversion();
    const second = result.current?.onStartConversion();
    resolveStart?.({ ok: true, jobId: "job-1" });
    await Promise.all([first, second]);

    expect(conversionJob.startConversion).toHaveBeenCalledTimes(1);
    cleanup();
  });

  it("prevents two same-task queue runs from starting any item twice", async () => {
    const first = createQueueItem("/tmp/first.epub", "audiobook");
    const second = createQueueItem("/tmp/second.epub", "audiobook");
    first.status = "ready";
    second.status = "ready";
    let resolveFirstStart!: (value: StartConversionResult) => void;
    let startCount = 0;
    const conversionJob = createConversionJobMock({
      startConversion: vi.fn(() => {
        startCount += 1;
        if (startCount === 1) {
          return new Promise<StartConversionResult>((resolve) => {
            resolveFirstStart = resolve;
          });
        }
        return Promise.resolve<StartConversionResult>({ ok: true, jobId: `job-${startCount}` });
      }),
    });
    const items = [first, second];
    const updateQueueItem = vi.fn((itemId, updater) => {
      const target = items.find((item) => item.id === itemId);
      if (target) {
        Object.assign(target, updater(target));
      }
    });
    const { result, cleanup } = renderRunner({
      apiClient: {} as never,
      conversionJob,
      queueItems: items,
      selectedQueueItem: first,
      buildStartPayload: (item) => payload(item?.sourcePath),
      updateQueueItem,
      syncSelectedQueueEdits: vi.fn(),
      setQueueStatus: vi.fn(),
    });

    const firstRun = result.current!.onRunQueue();
    const secondRun = result.current!.onRunQueue();

    expect(conversionJob.startConversion).toHaveBeenCalledTimes(1);
    resolveFirstStart({ ok: true, jobId: "job-1" });
    await Promise.all([firstRun, secondRun]);

    expect(conversionJob.startConversion).toHaveBeenCalledTimes(2);
    expect(conversionJob.startConversion).toHaveBeenNthCalledWith(
      1,
      expect.objectContaining({ source_path: "/tmp/first.epub" }),
    );
    expect(conversionJob.startConversion).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({ source_path: "/tmp/second.epub" }),
    );
    cleanup();
  });

  it("blocks a queue run while a direct start owns the command", async () => {
    const item = createQueueItem("/tmp/book.epub", "audiobook");
    item.status = "ready";
    let resolveStart!: (value: StartConversionResult) => void;
    const conversionJob = createConversionJobMock({
      startConversion: vi.fn(
        () => new Promise<StartConversionResult>((resolve) => { resolveStart = resolve; }),
      ),
    });
    const { result, cleanup } = renderRunner({
      apiClient: {} as never,
      conversionJob,
      queueItems: [item],
      selectedQueueItem: item,
      buildStartPayload: () => payload(),
      updateQueueItem: vi.fn(),
      syncSelectedQueueEdits: vi.fn(),
      setQueueStatus: vi.fn(),
    });

    const directStart = result.current!.onStartConversion();
    const queueRun = result.current!.onRunQueue();

    expect(conversionJob.startConversion).toHaveBeenCalledTimes(1);
    resolveStart({ ok: true, jobId: "job-1" });
    await Promise.all([directStart, queueRun]);
    cleanup();
  });

  it("blocks a direct start while a queue run owns the command", async () => {
    const item = createQueueItem("/tmp/book.epub", "audiobook");
    item.status = "ready";
    let resolveStart!: (value: StartConversionResult) => void;
    const conversionJob = createConversionJobMock({
      startConversion: vi.fn(
        () => new Promise<StartConversionResult>((resolve) => { resolveStart = resolve; }),
      ),
    });
    const { result, cleanup } = renderRunner({
      apiClient: {} as never,
      conversionJob,
      queueItems: [item],
      selectedQueueItem: item,
      buildStartPayload: () => payload(),
      updateQueueItem: vi.fn(),
      syncSelectedQueueEdits: vi.fn(),
      setQueueStatus: vi.fn(),
    });

    const queueRun = result.current!.onRunQueue();
    const directStart = result.current!.onStartConversion();

    expect(conversionJob.startConversion).toHaveBeenCalledTimes(1);
    resolveStart({ ok: true, jobId: "job-1" });
    await Promise.all([queueRun, directStart]);
    cleanup();
  });

  it("releases command ownership so a later queue run can start", async () => {
    const item = createQueueItem("/tmp/book.epub", "audiobook");
    item.status = "ready";
    const conversionJob = createConversionJobMock();
    const { result, cleanup } = renderRunner({
      apiClient: {} as never,
      conversionJob,
      queueItems: [item],
      selectedQueueItem: item,
      buildStartPayload: () => payload(),
      updateQueueItem: vi.fn(),
      syncSelectedQueueEdits: vi.fn(),
      setQueueStatus: vi.fn(),
    });

    await result.current!.onStartConversion();
    await result.current!.onRunQueue();

    expect(conversionJob.startConversion).toHaveBeenCalledTimes(2);
    cleanup();
  });
});

describe("useConversionQueueRunner indeterminate outcomes", () => {
  it("keeps running the queue when one item's terminal state is unknown", async () => {
    // A backend health blip swaps the apiClient, which flushes pending waiters
    // as "unknown". That says nothing about the conversion, so it must not
    // cancel the books queued behind it.
    const first = createQueueItem("/tmp/first.epub", "audiobook");
    const second = createQueueItem("/tmp/second.epub", "audiobook");
    first.status = "ready";
    second.status = "ready";

    let startCount = 0;
    const conversionJob = createConversionJobMock({
      startConversion: vi.fn(async (): Promise<StartConversionResult> => {
        startCount += 1;
        return { ok: true, jobId: `job-${startCount}` };
      }),
      waitForTerminalState: vi.fn(async (jobId: string) => ({
        jobId,
        status: (jobId === "job-1" ? "unknown" : "completed") as ConversionTerminalJobStatus,
        message: jobId === "job-1" ? "terminal wait cancelled" : "",
      })),
    });

    const items = [first, second];
    const updateQueueItem = vi.fn((itemId, updater) => {
      const target = items.find((candidate) => candidate.id === itemId);
      if (target) Object.assign(target, updater(target));
    });

    const { result, cleanup } = renderRunner({
      apiClient: {} as never,
      conversionJob,
      queueItems: items,
      selectedQueueItem: first,
      buildStartPayload: () => payload(),
      updateQueueItem,
      syncSelectedQueueEdits: vi.fn(),
      setQueueStatus: vi.fn(),
    });

    await result.current?.onRunQueue();

    expect(startCount).toBe(2);
    expect(second.status).toBe("done");
    expect(first.status).not.toBe("done");
    cleanup();
  });
});
