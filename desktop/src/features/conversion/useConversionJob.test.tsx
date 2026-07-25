// @vitest-environment happy-dom

import { act, useEffect as reactUseEffect } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useConversionJob } from "./useConversionJob";

const { refreshActiveConversionState, markConversionStarted } = vi.hoisted(() => ({
  refreshActiveConversionState: vi.fn(),
  markConversionStarted: vi.fn(),
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
          onActiveConversionReconciled?.(response);
        }
      })();
    }, [onActiveConversionReconciled]);
    return {
      activeJobId: "job-1",
      refreshActiveConversionState,
      markConversionStarted,
      uiState: "running",
    };
  },
}));

function Probe(): null {
  const result = useConversionJob({
    apiClient: (globalThis as typeof globalThis & { conversionApi?: never }).conversionApi ?? ({} as never),
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
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    delete (globalThis as typeof globalThis & { conversionJobProbe?: unknown }).conversionJobProbe;
    delete (globalThis as typeof globalThis & { conversionApi?: unknown }).conversionApi;
    vi.clearAllMocks();
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

    expect(refreshActiveConversionState).toHaveBeenCalled();
    expect(getProbe().activeJobTitle).toBe("Reloaded title");
    expect(getProbe().activeJobAuthor).toBe("Reloaded author");
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
