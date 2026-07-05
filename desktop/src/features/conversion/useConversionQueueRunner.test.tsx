import { renderToString } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { StartConversionRequest } from "../../types/api";
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
});
