// @vitest-environment happy-dom

import { act, useEffect as reactUseEffect } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../api/client";
import { useConversionJob } from "./useConversionJob";

const { refreshActiveConversionState, markConversionStarted, eventState, defaultClient } = vi.hoisted(() => ({
  refreshActiveConversionState: vi.fn(),
  markConversionStarted: vi.fn(),
  eventState: { activeJobId: "job-1" as string | null, uiState: "running" },
  defaultClient: {},
}));

vi.mock("../events/useEventStream", () => ({
  useEventStream: ({
    onActiveConversionReconciled,
  }: {
    onActiveConversionReconciled?: (response: unknown) => void;
  }) => {
    reactUseEffect(() => {
      void (async () => {
        const response = await refreshActiveConversionState();
        if (response) {
          eventState.activeJobId = response.active ? response.job?.id ?? null : null;
          eventState.uiState = response.job?.status ?? "idle";
          onActiveConversionReconciled?.(response);
        }
      })();
    }, [onActiveConversionReconciled]);
    return {
      activeJobId: eventState.activeJobId,
      refreshActiveConversionState,
      markConversionStarted,
      uiState: eventState.uiState,
    };
  },
}));

function Probe(): null {
  const result = useConversionJob({
    apiClient: (globalThis as typeof globalThis & { conversionApi?: never }).conversionApi ?? (defaultClient as never),
    baseUrl: "http://desktop.test",
  });
  (globalThis as typeof globalThis & { conversionJobProbe?: typeof result }).conversionJobProbe = result;
  return null;
}

function getProbe(): ReturnType<typeof useConversionJob> {
  return (globalThis as typeof globalThis & { conversionJobProbe: ReturnType<typeof useConversionJob> })
    .conversionJobProbe;
}

describe("useConversionJob metadata reconciliation", () => {
  let root: Root;
  let container: HTMLDivElement;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
    eventState.activeJobId = "job-1";
    eventState.uiState = "running";
    markConversionStarted.mockImplementation((jobId: string) => {
      eventState.activeJobId = jobId;
      eventState.uiState = "running";
    });
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    delete (globalThis as typeof globalThis & { conversionJobProbe?: unknown }).conversionJobProbe;
    delete (globalThis as typeof globalThis & { conversionApi?: unknown }).conversionApi;
    vi.clearAllMocks();
  });

  it.each(["success", "error"])("ignores an obsolete start %s while a replacement start is pending", async (outcome) => {
    let resolveOld!: (value: { job_id: string }) => void;
    let rejectOld!: (reason: Error) => void;
    let resolveNew!: (value: { job_id: string }) => void;
    const oldClient = { startConversion: vi.fn(() => new Promise<{ job_id: string }>((resolve, reject) => {
      resolveOld = resolve;
      rejectOld = reject;
    })) };
    const newClient = { startConversion: vi.fn(() => new Promise<{ job_id: string }>((resolve) => {
      resolveNew = resolve;
    })) };
    const globals = globalThis as typeof globalThis & { conversionApi: typeof oldClient };
    globals.conversionApi = oldClient;
    refreshActiveConversionState.mockResolvedValue(null);
    await act(async () => {
      root = createRoot(container);
      root.render(<Probe />);
    });
    const payload = {
      source_path: "/tmp/book.epub",
      conversion_mode: "audiobook" as const,
      title: "Current book",
      runtime_options: { tts_engine: "engine", voice: "voice", speed: 1 },
    };
    let oldStart!: ReturnType<ReturnType<typeof useConversionJob>["startConversion"]>;
    await act(async () => { oldStart = getProbe().startConversion({ ...payload, title: "Old book" }); });
    globals.conversionApi = newClient;
    await act(async () => { root.render(<Probe />); });
    expect(getProbe().isStarting).toBe(false);
    let newStart!: typeof oldStart;
    await act(async () => { newStart = getProbe().startConversion(payload); });
    expect(newClient.startConversion).toHaveBeenCalledTimes(1);
    await act(async () => {
      if (outcome === "success") resolveOld({ job_id: "old-job" });
      else rejectOld(new Error("old failure"));
      expect((await oldStart).ok).toBe(false);
    });
    expect(markConversionStarted).not.toHaveBeenCalled();
    expect(getProbe().activeJobTitle).toBe("");
    expect(getProbe().formError).toBe("");
    expect(getProbe().isStarting).toBe(true);
    await act(async () => {
      resolveNew({ job_id: "new-job" });
      expect(await newStart).toEqual({ ok: true, jobId: "new-job" });
    });
    expect(markConversionStarted).toHaveBeenCalledExactlyOnceWith("new-job");
    expect(getProbe().activeJobTitle).toBe("Current book");
    expect(getProbe().isStarting).toBe(false);
  });

  it("preserves the explicit cleanup admission rejection for queue retry", async () => {
    refreshActiveConversionState.mockResolvedValue(null);
    const error = new ApiError({ message: "Previous conversion cleanup pending", status: 409, code: "http",
      detail: "Previous conversion cleanup pending", serverCode: "conversion_cleanup_pending", retriable: false });
    const client = { startConversion: vi.fn().mockRejectedValue(error) };
    (globalThis as typeof globalThis & { conversionApi: typeof client }).conversionApi = client;
    await act(async () => { root = createRoot(container); root.render(<Probe />); });
    await act(async () => {
      expect(await getProbe().startConversion({ source_path: "/tmp/book.epub", conversion_mode: "audiobook",
        runtime_options: { tts_engine: "engine", voice: "voice", speed: 1 } })).toEqual({
        ok: false, error: "Previous conversion cleanup pending", status: 409, serverCode: "conversion_cleanup_pending",
      });
    });
    expect(markConversionStarted).not.toHaveBeenCalled();
  });

  it("restores the active job title and author during mount reconciliation", async () => {
    refreshActiveConversionState.mockResolvedValue({
      active: true,
      job: {
        id: "job-1",
        book_id: null,
        status: "running",
        error: null,
        title: "Reloaded title",
        author: "Reloaded author",
      },
    });

    await act(async () => {
      root = createRoot(container);
      root.render(<Probe />);
    });

    expect(getProbe().activeJobTitle).toBe("Reloaded title");
    expect(getProbe().activeJobAuthor).toBe("Reloaded author");
  });

  it.each([[7, { jobId: "repair_7_1", bookId: 7 }], [null, null]])(
    "reports which book the reconciled job belongs to (book %s)",
    async (bookId, expected) => {
      refreshActiveConversionState.mockResolvedValue({
        active: true,
        job: { id: "repair_7_1", book_id: bookId, status: "running", error: null, title: "Lantern", author: null },
      });

      await act(async () => {
        root = createRoot(container);
        root.render(<Probe />);
      });

      expect(getProbe().activeJobBook).toEqual(expected);
    },
  );

  it("reads the book of a job cancelled before any poll reported it", async () => {
    refreshActiveConversionState.mockResolvedValue(null);
    const ended = (bookId: number | null) => ({ active: false,
      job: { id: "job-early", book_id: bookId, status: "cancelled", error: null, title: "Glass Hills", author: null } });
    const client = {
      startConversion: vi.fn().mockResolvedValue({ job_id: "job-early" }),
      getActiveConversion: vi.fn().mockResolvedValue(ended(12)),
    };
    (globalThis as typeof globalThis & { conversionApi: typeof client }).conversionApi = client;
    await act(async () => { root = createRoot(container); root.render(<Probe />); });
    await act(async () => {
      await getProbe().startConversion({ source_path: "/tmp/glass.md", conversion_mode: "audiobook", title: "Glass Hills",
        runtime_options: { tts_engine: "engine", voice: "voice", speed: 1 } });
    });
    expect(getProbe().activeJobBook).toBeNull();
    expect(client.getActiveConversion).not.toHaveBeenCalled();

    // The stream reports the cancel; no reconcile poll ran while the job lived.
    eventState.activeJobId = null;
    eventState.uiState = "cancelled";
    await act(async () => { root.render(<Probe />); });

    expect(client.getActiveConversion).toHaveBeenCalledTimes(1);
    expect(getProbe().activeJobBook).toEqual({ jobId: "job-early", bookId: 12 });
    await act(async () => { root.render(<Probe />); });
    expect(client.getActiveConversion).toHaveBeenCalledTimes(1);
  });

  it("does not link an ended job to another job's book", async () => {
    refreshActiveConversionState.mockResolvedValue(null);
    const client = {
      startConversion: vi.fn().mockResolvedValue({ job_id: "job-early" }),
      getActiveConversion: vi.fn().mockResolvedValue({ active: true,
        job: { id: "repair_9_1", book_id: 9, status: "running", error: null, title: "Other", author: null } }),
    };
    (globalThis as typeof globalThis & { conversionApi: typeof client }).conversionApi = client;
    await act(async () => { root = createRoot(container); root.render(<Probe />); });
    await act(async () => {
      await getProbe().startConversion({ source_path: "/tmp/glass.md", conversion_mode: "audiobook", title: "Glass Hills",
        runtime_options: { tts_engine: "engine", voice: "voice", speed: 1 } });
    });
    eventState.activeJobId = null;
    eventState.uiState = "cancelled";
    await act(async () => { root.render(<Probe />); });

    expect(client.getActiveConversion).toHaveBeenCalledTimes(1);
    expect(getProbe().activeJobBook).toBeNull();
  });

  it("fences a stale active lookup after a newer conversion starts", async () => {
    const apiClient = {
      startConversion: vi.fn(async () => ({ job_id: "job-b" })),
    };
    (globalThis as typeof globalThis & { conversionApi: typeof apiClient }).conversionApi = apiClient;
    refreshActiveConversionState.mockResolvedValue({
      active: false,
      job: {
        id: "job-a",
        book_id: null,
        status: "running",
        error: null,
        title: "stale",
        author: "stale",
      },
    });

    await act(async () => {
      root = createRoot(container);
      root.render(<Probe />);
    });

    await act(async () => {
      const result = await getProbe().startConversion({
        source_path: "/tmp/book.epub",
        conversion_mode: "audiobook",
        title: "Book B",
        author: "Author B",
        runtime_options: { tts_engine: "engine", voice: "voice", speed: 1 },
      });
      expect(result).toEqual({ ok: true, jobId: "job-b" });
    });

    expect(markConversionStarted).toHaveBeenCalledWith("job-b");
    expect(getProbe().activeJobTitle).toBe("Book B");
    expect(getProbe().activeJobAuthor).toBe("Author B");
  });
});
