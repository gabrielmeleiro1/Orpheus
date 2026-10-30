// @vitest-environment happy-dom

import { act, StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SourcePreflight } from "../../types/api";
import { createQueueItem } from "./conversionPanelModel";
import { useConversionQueueController } from "./useConversionQueueController";
import { createApiClient } from "../../api/client";
import { SOURCE_PREFLIGHT_TIMEOUT_MS } from "../../api/resources/conversion";

interface ProbeProps {
  apiClient: unknown;
}

function Probe({ apiClient }: ProbeProps): null {
  const controller = useConversionQueueController({ apiClient: apiClient as never });
  (
    globalThis as typeof globalThis & {
      conversionQueueControllerProbe?: typeof controller;
    }
  ).conversionQueueControllerProbe = controller;
  return null;
}

function getController(): ReturnType<typeof useConversionQueueController> {
  return (
    globalThis as typeof globalThis & {
      conversionQueueControllerProbe: ReturnType<typeof useConversionQueueController>;
    }
  ).conversionQueueControllerProbe;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function preflight(overrides: Partial<SourcePreflight> = {}): SourcePreflight {
  return {
    title: null,
    author: null,
    chapter_count: 1,
    source_type: "epub",
    sample: "",
    warnings: [],
    size_bytes: 10,
    requires_ocr: false,
    pdf_text_layer: null,
    ...overrides,
  };
}

describe("useConversionQueueController", () => {
  let container: HTMLDivElement;
  let root: Root | null;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    if (root) {
      await act(async () => root?.unmount());
    }
    container.remove();
    delete (
      globalThis as typeof globalThis & {
        conversionQueueControllerProbe?: unknown;
      }
    ).conversionQueueControllerProbe;
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("brings the queue back after a restart and re-checks only the rows that never started", async () => {
    const client = { preflightSource: vi.fn(async (sourcePath: string) => {
      if (sourcePath === "/books/moved.epub") throw new Error("Source file not found");
      return preflight({ title: "Waiting", chapter_count: 4 });
    }) };
    await act(async () => root?.render(<Probe apiClient={client} />));
    const waiting = createQueueItem("/books/waiting.epub", "audiobook", undefined, preflight({ title: "Waiting" }));
    const moved = createQueueItem("/books/moved.epub", "audiobook", undefined, preflight({ title: "Moved" }));
    const converting = { ...createQueueItem("/books/running.epub", "audiobook", undefined, preflight()),
      title: "Running", status: "converting" as const, jobId: "job-running" };
    const done = { ...createQueueItem("/books/done.epub", "audiobook", undefined, preflight()),
      title: "Done", status: "done" as const, jobId: "job-done", bookId: 7 };
    await act(async () => getController().addQueueItems([waiting, moved, converting, done], { skipPreflight: true }));
    await act(async () => getController().applySelectedQueueItem(waiting));
    await act(async () => getController().moveQueueItem(moved.id, -1));
    expect(client.preflightSource).not.toHaveBeenCalled();

    // Restart: a fresh controller reads what the previous one saved.
    await act(async () => root?.unmount());
    root = createRoot(container);
    await act(async () => root?.render(<Probe apiClient={client} />));
    const restored = getController();
    expect(restored.queueItems.map((item) => item.id)).toEqual([moved.id, waiting.id, converting.id, done.id]);
    expect(restored.selectedQueueId).toBe(waiting.id);
    expect(restored.sourcePath).toBe("/books/waiting.epub");
    expect(restored.queueItems[0]).toMatchObject({ status: "failed", error: "Source file not found" });
    expect(restored.queueItems[1]).toMatchObject({ status: "ready", preflight: { chapter_count: 4 } });
    // A conversion the app was running when it closed is not claimed as finished.
    expect(restored.queueItems[2]).toMatchObject({ status: "warning", jobId: "job-running" });
    expect(restored.queueItems[2].error).toContain("Check Library");
    expect(restored.queueItems[3]).toMatchObject({ status: "done", jobId: "job-done", bookId: 7 });
    expect(client.preflightSource.mock.calls.map(([sourcePath]) => sourcePath)).toEqual(["/books/moved.epub", "/books/waiting.epub"]);

    // An emptied queue stays empty after the next restart.
    for (const item of restored.queueItems) await act(async () => getController().removeQueueItem(item.id));
    await act(async () => root?.unmount());
    root = createRoot(container);
    await act(async () => root?.render(<Probe apiClient={client} />));
    expect(getController().queueItems).toEqual([]);
  });

  it("releases a stalled inspection and can inspect the source again", async () => {
    vi.useFakeTimers();
    const fetch = vi.spyOn(globalThis, "fetch").mockImplementationOnce((_input, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    })).mockImplementation(async () => new Response(JSON.stringify(preflight())));
    const client = createApiClient({ baseUrl: "http://desktop.test", token: "test" });
    await act(async () => root?.render(<Probe apiClient={client} />));
    await act(async () => getController().addQueueItems([createQueueItem("/tmp/book.epub", "audiobook")], { selectFirst: true }));
    expect(getController().queueItems[0].status).toBe("preflighting");
    await act(async () => vi.advanceTimersByTimeAsync(SOURCE_PREFLIGHT_TIMEOUT_MS));
    expect(getController().queueItems[0].status).toBe("failed");
    await act(async () => getController().runPreflightForItem(getController().queueItems[0], { updateSelected: true }));
    expect(getController().queueItems[0].status).toBe("ready");
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it.each([false, true])("keeps a completed conversion out of the runnable queue after reinspection (fails: %s)", async (fails) => {
    const request = deferred<SourcePreflight>();
    const client = { preflightSource: vi.fn(() => request.promise) };
    await act(async () => root?.render(<Probe apiClient={client} />));
    const item = { ...createQueueItem("/tmp/finished.epub", "audiobook", undefined, preflight()),
      status: "done" as const, jobId: "completed-job" };
    await act(async () => getController().addQueueItems([item], { selectFirst: true, skipPreflight: true }));
    let inspection!: Promise<void>;
    await act(async () => {
      inspection = getController().runPreflightForItem({ ...item, preflight: null }, { updateSelected: true });
    });
    expect(getController().queueItems[0].status).toBe("done");
    await act(async () => {
      if (fails) request.reject(new Error("Source was moved"));
      else request.resolve(preflight({ chapter_count: 3 }));
      await inspection;
    });
    expect(getController().queueItems[0]).toMatchObject({ status: "done", jobId: "completed-job" });
    if (fails) expect(getController().preflightStatus).toBe("Source was moved");
    else expect(getController().queueItems[0].preflight?.chapter_count).toBe(3);
  });

  it.each(["file", "paste"])("edits the selected %s metadata independently of the paste draft", async (kind) => {
    const client = { preflightSource: vi.fn(async () => preflight()) };
    await act(async () => root?.render(<Probe apiClient={client} />));
    const item = createQueueItem("upload:document.md", "document_audio", "Document", preflight());
    await act(async () => {
      if (kind === "paste") getController().addPastedSource(item, { selectItem: true });
      else getController().addQueueItems([item], { selectFirst: true });
    });
    await act(async () => getController().syncSelectedQueueEdits({ title: "Edited title", author: "Edited author" }));
    expect(getController().selectedQueueItem).toMatchObject({ title: "Edited title", author: "Edited author" });
  });

  it("keeps the selected source and preview when another source finishes inspection", async () => {
    const request = deferred<SourcePreflight>();
    const client = { preflightSource: vi.fn(() => request.promise) };
    const first = createQueueItem("/tmp/first.epub", "audiobook");
    const second = createQueueItem("/tmp/second.epub", "audiobook", undefined,
      preflight({ title: "Second title", author: "Second author", sample: "Second preview" }));
    await act(async () => root?.render(<Probe apiClient={client} />));
    await act(async () => getController().addQueueItems([first, second], { selectFirst: true }));
    expect(getController().queueItems[0].status).toBe("preflighting");
    await act(async () => getController().applySelectedQueueItem(second));

    const result = preflight({ title: "First title", author: "First author", sample: "First preview" });
    await act(async () => request.resolve(result));

    expect(getController()).toMatchObject({
      selectedQueueId: second.id,
      sourcePath: second.sourcePath,
      titleOverride: "Second title",
      authorOverride: "Second author",
      previewText: "Second preview",
    });
    expect(getController().queueItems[0]).toMatchObject({
      title: "First title", author: "First author", preflight: result, status: "ready",
    });
  });

  it.each(["resolve", "reject"] as const)(
    "ignores client A preflight %s while automatically resuming inspection on client B",
    async (oldOutcome) => {
      const clientARequest = deferred<SourcePreflight>();
      const clientBRequest = deferred<SourcePreflight>();
      let clientASignal: AbortSignal | undefined;
      let clientBSignal: AbortSignal | undefined;
      const clientA = {
        preflightSource: vi.fn(
          (_sourcePath: string, options?: { signal?: AbortSignal }) => {
            clientASignal = options?.signal;
            return clientARequest.promise;
          },
        ),
      };
      const clientB = {
        preflightSource: vi.fn(
          (_sourcePath: string, options?: { signal?: AbortSignal }) => {
            clientBSignal = options?.signal;
            return clientBRequest.promise;
          },
        ),
      };
      const item = createQueueItem("/tmp/book.epub", "audiobook");

      await act(async () => {
        root?.render(<Probe apiClient={clientA} />);
      });
      act(() => {
        getController().addQueueItems([item], { selectFirst: true });
      });
      expect(clientA.preflightSource).toHaveBeenCalledTimes(1);
      expect(clientASignal?.aborted).toBe(false);

      await act(async () => {
        root?.render(<Probe apiClient={clientB} />);
      });
      expect(clientASignal?.aborted).toBe(true);

      expect(clientB.preflightSource).toHaveBeenCalledTimes(1);
      expect(clientBSignal?.aborted).toBe(false);

      await act(async () => {
        if (oldOutcome === "resolve") {
          clientARequest.resolve(preflight({ title: "Stale A", sample: "old sample" }));
        } else {
          clientARequest.reject(new Error("stale A failed"));
        }
        await Promise.resolve();
        await Promise.resolve();
      });

      expect(getController().queueItems[0]).toMatchObject({
        status: "preflighting",
        error: "",
        preflight: null,
        title: "",
      });
      // The row's "Inspecting…" status is the only progress shown.
      expect(getController().preflightStatus).toBe("");

      const currentPreflight = preflight({ title: "Current B", sample: "new sample" });
      clientBRequest.resolve(currentPreflight);
      await act(async () => {
        await clientBRequest.promise;
      });

      expect(getController().queueItems[0]).toMatchObject({
        status: "ready",
        error: "",
        preflight: currentPreflight,
        title: "Current B",
      });
      expect(getController().preflightStatus).toBe("");
    },
  );

  it("aborts a disconnected inspection and resumes the remaining queue after reconnect", async () => {
    const request = deferred<SourcePreflight>();
    let requestSignal: AbortSignal | undefined;
    const client = {
      preflightSource: vi.fn(
        (_sourcePath: string, options?: { signal?: AbortSignal }) => {
          requestSignal = options?.signal;
          return request.promise;
        },
      ),
    };
    const item = createQueueItem("/tmp/disconnect.epub", "audiobook");
    const nextItem = createQueueItem("/tmp/next.epub", "audiobook");

    await act(async () => {
      root?.render(<Probe apiClient={client} />);
    });
    act(() => {
      getController().addQueueItems([item, nextItem], { selectFirst: true });
    });

    await act(async () => {
      root?.render(<Probe apiClient={null} />);
    });
    expect(requestSignal?.aborted).toBe(true);

    request.resolve(preflight({ title: "Disconnected result" }));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(getController().queueItems[0]).toMatchObject({
      status: "preflighting",
      error: "",
      preflight: null,
      title: "",
    });
    expect(getController().preflightStatus).toBe("");

    const reconnectedClient = { preflightSource: vi.fn(async (_sourcePath: string) => preflight({ title: "Reconnected source" })) };
    await act(async () => root?.render(<Probe apiClient={reconnectedClient} />));

    expect(reconnectedClient.preflightSource.mock.calls.map(([path]) => path))
      .toEqual([item.sourcePath, nextItem.sourcePath]);
    expect(getController().queueItems.map((queued) => queued.status)).toEqual(["ready", "ready"]);
    expect(getController().titleOverride).toBe("Reconnected source");
  });

  it("aborts and invalidates preflight when the selected source path is cleared", async () => {
    const request = deferred<SourcePreflight>();
    let requestSignal: AbortSignal | undefined;
    const client = {
      preflightSource: vi.fn(
        (_sourcePath: string, options?: { signal?: AbortSignal }) => {
          requestSignal = options?.signal;
          return request.promise;
        },
      ),
    };
    const item = createQueueItem("/tmp/clear.epub", "audiobook");

    await act(async () => {
      root?.render(<Probe apiClient={client} />);
    });
    act(() => {
      getController().addQueueItems([item], { selectFirst: true });
    });
    expect(requestSignal?.aborted).toBe(false);

    act(() => {
      getController().syncSelectedQueueEdits({ sourcePath: "" });
    });

    expect(requestSignal?.aborted).toBe(true);
    expect(getController().sourcePath).toBe("");
    expect(getController().queueItems[0]).toMatchObject({
      sourcePath: "",
      preflight: null,
      status: "ready",
      error: "",
    });

    request.resolve(preflight({ title: "Stale result" }));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(getController().queueItems[0].preflight).toBeNull();
  });

  it("aborts an in-flight preflight and safely ignores its completion after unmount", async () => {
    const request = deferred<SourcePreflight>();
    let requestSignal: AbortSignal | undefined;
    const client = {
      preflightSource: vi.fn(
        (_sourcePath: string, options?: { signal?: AbortSignal }) => {
          requestSignal = options?.signal;
          return request.promise;
        },
      ),
    };

    await act(async () => {
      root?.render(<Probe apiClient={client} />);
    });
    act(() => {
      getController().addQueueItems(
        [createQueueItem("/tmp/unmount.epub", "audiobook")],
        { selectFirst: true },
      );
    });

    await act(async () => {
      root?.unmount();
      root = null;
    });
    expect(requestSignal?.aborted).toBe(true);

    request.resolve(preflight({ title: "Unmounted result" }));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
  });

  it("keeps current preflight callbacks live through StrictMode effect replay", async () => {
    const currentPreflight = preflight({ title: "Strict mode" });
    const client = {
      preflightSource: vi.fn(async () => currentPreflight),
    };

    await act(async () => {
      root?.render(
        <StrictMode>
          <Probe apiClient={client} />
        </StrictMode>,
      );
    });
    act(() => {
      getController().addQueueItems(
        [createQueueItem("/tmp/strict.epub", "audiobook")],
        { selectFirst: true },
      );
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(getController().queueItems[0]).toMatchObject({
      status: "ready",
      preflight: currentPreflight,
      title: "Strict mode",
    });
  });
});
