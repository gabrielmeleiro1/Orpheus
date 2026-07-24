// @vitest-environment happy-dom

import { act, StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SourcePreflight } from "../../types/api";
import { createQueueItem } from "./conversionPanelModel";
import { useConversionQueueController } from "./useConversionQueueController";

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
  });

  it.each(["resolve", "reject"] as const)(
    "ignores client A preflight %s after switching to client B while current B still completes",
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

      let clientBPromise!: Promise<void>;
      act(() => {
        clientBPromise = getController().runPreflightForItem(
          getController().queueItems[0],
          { updateSelected: true },
        );
      });
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
      expect(getController().preflightStatus).toBe("Inspecting book.epub...");

      const currentPreflight = preflight({ title: "Current B", sample: "new sample" });
      clientBRequest.resolve(currentPreflight);
      await act(async () => {
        await clientBPromise;
      });

      expect(getController().queueItems[0]).toMatchObject({
        status: "ready",
        error: "",
        preflight: currentPreflight,
        title: "Current B",
      });
      expect(getController().preflightStatus).toBe("Preflight completed.");
    },
  );

  it("aborts and ignores an in-flight preflight when the client disconnects", async () => {
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

    await act(async () => {
      root?.render(<Probe apiClient={client} />);
    });
    act(() => {
      getController().addQueueItems([item], { selectFirst: true });
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
    expect(getController().preflightStatus).toBe("Inspecting disconnect.epub...");
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
