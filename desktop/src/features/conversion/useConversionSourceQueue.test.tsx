// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useConversionSourceQueue } from "./useConversionSourceQueue";

const { invokeMock, listenMock } = vi.hoisted(() => ({
  invokeMock: vi.fn(),
  listenMock: vi.fn(async () => vi.fn()),
}));

vi.mock("@tauri-apps/api/core", () => ({ invoke: invokeMock }));
vi.mock("@tauri-apps/api/event", () => ({ listen: listenMock }));

interface ProbeProps {
  apiClient?: unknown;
  conversionJob?: unknown;
  queueController?: unknown;
  setPreflightStatus?: (value: string) => void;
}

function Probe({
  apiClient = null,
  conversionJob,
  queueController,
  setPreflightStatus = vi.fn(),
}: ProbeProps): null {
  const result = useConversionSourceQueue({
    apiClient: apiClient as never,
    conversionMode: "audiobook",
    conversionJob: (conversionJob ?? { clearFormError: vi.fn() }) as never,
    queueController: (queueController ?? {
        selectedQueueId: null,
        sourcePath: "",
        addQueueItems: vi.fn(),
        setPreflightStatus,
      }) as never,
  });
  (globalThis as typeof globalThis & { sourceQueueProbe?: typeof result }).sourceQueueProbe = result;
  return null;
}

describe("useConversionSourceQueue", () => {
  let root: Root;
  let container: HTMLDivElement;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    Object.defineProperty(window, "__TAURI_INTERNALS__", { configurable: true, value: {} });
    container = document.createElement("div");
    document.body.append(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    Reflect.deleteProperty(window, "__TAURI_INTERNALS__");
    delete (globalThis as typeof globalThis & { sourceQueueProbe?: unknown }).sourceQueueProbe;
    vi.clearAllMocks();
  });

  it("surfaces a native file-picker failure", async () => {
    const setPreflightStatus = vi.fn();
    invokeMock.mockRejectedValueOnce(new Error("dialog unavailable"));
    await act(async () => {
      root = createRoot(container);
      root.render(<Probe setPreflightStatus={setPreflightStatus} />);
    });

    await act(async () => {
      await (globalThis as typeof globalThis & { sourceQueueProbe: ReturnType<typeof useConversionSourceQueue> })
        .sourceQueueProbe.onBrowse();
    });

    expect(setPreflightStatus).toHaveBeenCalledWith("Could not open file picker: dialog unavailable");
  });

  it("keeps successful browser uploads queued in input order when another selected file fails", async () => {
    const firstFile = new File(["first"], "first.md", { type: "text/markdown" });
    const secondFile = new File(["second"], "second.md", { type: "text/markdown" });
    const thirdFile = new File(["third"], "third.md", { type: "text/markdown" });
    let resolveFirst!: (value: { source_handle: string; original_name: string; preflight: null }) => void;
    const uploadSourceFile = vi.fn((file: File) => {
      if (file.name === "first.md") {
        return new Promise<{ source_handle: string; original_name: string; preflight: null }>((resolve) => {
          resolveFirst = resolve;
        });
      }
      if (file.name === "second.md") {
        return Promise.reject(new Error("second upload failed"));
      }
      return Promise.resolve({ source_handle: "upload:third", original_name: "third.md", preflight: null });
    });
    const addQueueItems = vi.fn();
    const setPreflightStatus = vi.fn();

    await act(async () => {
      root = createRoot(container);
      root.render(
        <Probe
          apiClient={{ uploadSourceFile }}
          conversionJob={{ clearFormError: vi.fn() }}
          queueController={{
            selectedQueueId: null,
            sourcePath: "",
            addQueueItems,
            setPreflightStatus,
          }}
        />,
      );
    });

    const getProbe = () =>
      (globalThis as typeof globalThis & { sourceQueueProbe: ReturnType<typeof useConversionSourceQueue> })
        .sourceQueueProbe;
    const event = { target: { files: [firstFile, secondFile, thirdFile], value: "selected" } } as never;
    const uploadPromise = getProbe().onBrowserFilesSelected(event);
    resolveFirst({ source_handle: "upload:first", original_name: "first.md", preflight: null });
    await act(async () => {
      await uploadPromise;
    });

    expect(addQueueItems).toHaveBeenCalledWith(
      [
        expect.objectContaining({ sourcePath: "upload:first", label: "first.md" }),
        expect.objectContaining({ sourcePath: "upload:third", label: "third.md" }),
      ],
      { selectFirst: true },
    );
    expect(setPreflightStatus).toHaveBeenLastCalledWith("Added 2 sources; 1 upload failed.");
  });

  it("does not apply a browser upload completion after unmount", async () => {
    let resolveUpload!: (value: { source_handle: string; original_name: string; preflight: null }) => void;
    const uploadSourceFile = vi.fn(
      () =>
        new Promise<{ source_handle: string; original_name: string; preflight: null }>((resolve) => {
          resolveUpload = resolve;
        }),
    );
    const addQueueItems = vi.fn();
    const setPreflightStatus = vi.fn();
    const clearFormError = vi.fn();

    await act(async () => {
      root = createRoot(container);
      root.render(
        <Probe
          apiClient={{ uploadSourceFile }}
          conversionJob={{ clearFormError }}
          queueController={{
            selectedQueueId: null,
            sourcePath: "",
            addQueueItems,
            setPreflightStatus,
          }}
        />,
      );
    });

    const event = {
      target: { files: [new File(["stale"], "stale.md")], value: "selected" },
    } as never;
    const uploadPromise = (
      globalThis as typeof globalThis & {
        sourceQueueProbe: ReturnType<typeof useConversionSourceQueue>;
      }
    ).sourceQueueProbe.onBrowserFilesSelected(event);

    await act(async () => {
      root.unmount();
    });
    resolveUpload({ source_handle: "upload:stale", original_name: "stale.md", preflight: null });
    await act(async () => {
      await uploadPromise;
    });

    expect(addQueueItems).not.toHaveBeenCalled();
    expect(clearFormError).not.toHaveBeenCalled();
    expect(setPreflightStatus).toHaveBeenCalledTimes(1);
    expect(setPreflightStatus).toHaveBeenLastCalledWith("Uploading stale.md...");
  });

  it("queues concurrent browser batches but lets only the latest batch own final status and selection", async () => {
    type UploadResponse = { source_handle: string; original_name: string; preflight: null };
    const uploadResolvers = new Map<string, (value: UploadResponse) => void>();
    const uploadSourceFile = vi.fn((file: File) => {
      if (file.name === "old-fail.md") {
        return Promise.reject(new Error("old upload failed"));
      }
      return new Promise<UploadResponse>((resolve) => {
        uploadResolvers.set(file.name, resolve);
      });
    });
    const addQueueItems = vi.fn();
    const setPreflightStatus = vi.fn();

    await act(async () => {
      root = createRoot(container);
      root.render(
        <Probe
          apiClient={{ uploadSourceFile }}
          conversionJob={{ clearFormError: vi.fn() }}
          queueController={{
            selectedQueueId: null,
            sourcePath: "",
            addQueueItems,
            setPreflightStatus,
          }}
        />,
      );
    });

    const getProbe = () =>
      (globalThis as typeof globalThis & { sourceQueueProbe: ReturnType<typeof useConversionSourceQueue> })
        .sourceQueueProbe;
    const olderBatch = getProbe().onBrowserFilesSelected({
      target: {
        files: [new File(["old"], "old.md"), new File(["fail"], "old-fail.md")],
        value: "older",
      },
    } as never);
    const newerBatch = getProbe().onBrowserFilesSelected({
      target: { files: [new File(["new"], "new.md")], value: "newer" },
    } as never);

    uploadResolvers.get("new.md")?.({
      source_handle: "upload:new",
      original_name: "new.md",
      preflight: null,
    });
    await act(async () => {
      await newerBatch;
    });
    uploadResolvers.get("old.md")?.({
      source_handle: "upload:old",
      original_name: "old.md",
      preflight: null,
    });
    await act(async () => {
      await olderBatch;
    });

    expect(addQueueItems).toHaveBeenNthCalledWith(
      1,
      [expect.objectContaining({ sourcePath: "upload:new" })],
      { selectFirst: true },
    );
    expect(addQueueItems).toHaveBeenNthCalledWith(
      2,
      [expect.objectContaining({ sourcePath: "upload:old" })],
      { selectFirst: false },
    );
    expect(setPreflightStatus).toHaveBeenLastCalledWith("Source uploaded.");
  });

  it("cleans up every Tauri listener exactly once when registration resolves around unmount", async () => {
    type Registration = {
      resolve: (unlisten: () => void) => void;
      reject: (error: unknown) => void;
    };
    const registrations: Registration[] = [];
    listenMock.mockImplementation(
      (() =>
        new Promise<() => void>((resolve, reject) => {
          registrations.push({ resolve, reject });
        })) as never,
    );
    const unlisteners = Array.from({ length: 5 }, () => vi.fn());

    await act(async () => {
      root = createRoot(container);
      root.render(<Probe />);
      await Promise.resolve();
    });
    expect(registrations).toHaveLength(5);

    registrations[0].resolve(unlisteners[0]);
    registrations[1].resolve(unlisteners[1]);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    await act(async () => {
      root.unmount();
    });
    registrations[2].resolve(unlisteners[2]);
    registrations[3].resolve(unlisteners[3]);
    registrations[4].reject(new Error("listener unavailable"));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(unlisteners[0]).toHaveBeenCalledTimes(1);
    expect(unlisteners[1]).toHaveBeenCalledTimes(1);
    expect(unlisteners[2]).toHaveBeenCalledTimes(1);
    expect(unlisteners[3]).toHaveBeenCalledTimes(1);
    expect(unlisteners[4]).not.toHaveBeenCalled();
    listenMock.mockImplementation(async () => vi.fn());
  });

  it("uses dedicated paste metadata even when it matches the selected editable row", async () => {
    const uploadSourceText = vi.fn(async () => ({
      source_handle: "upload:1_hash_paste.md",
      original_name: "paste.md",
      preflight: null,
    }));
    const addPastedSource = vi.fn();
    const clearFormError = vi.fn();
    const selectedQueueItem = {
      id: "selected",
      sourcePath: "/tmp/selected.md",
      title: "Shared title",
      author: "Shared author",
      status: "ready",
    };
    await act(async () => {
      root = createRoot(container);
      root.render(
        <Probe
          apiClient={{ uploadSourceText }}
          conversionJob={{ clearFormError }}
          queueController={{
            selectedQueueId: selectedQueueItem.id,
            sourcePath: selectedQueueItem.sourcePath,
            selectedQueueItem,
            titleOverride: selectedQueueItem.title,
            authorOverride: selectedQueueItem.author,
            addPastedSource,
          }}
        />,
      );
    });
    const getProbe = () =>
      (globalThis as typeof globalThis & { sourceQueueProbe: ReturnType<typeof useConversionSourceQueue> })
        .sourceQueueProbe;

    await act(async () => {
      getProbe().setPastedText("  Body text  ");
      getProbe().setPastedTitle("  Shared title  ");
      getProbe().setPastedAuthor("  Shared author  ");
    });
    let returnedItem: Awaited<ReturnType<ReturnType<typeof useConversionSourceQueue>["onAddPastedText"]>> = null;
    await act(async () => {
      returnedItem = await getProbe().onAddPastedText();
    });

    expect(uploadSourceText).toHaveBeenCalledWith({ text: "Body text", title: "Shared title" });
    expect(returnedItem).toEqual(
      expect.objectContaining({ sourcePath: "upload:1_hash_paste.md", title: "Shared title" }),
    );
    expect(addPastedSource).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Shared title", author: "Shared author" }),
      { selectItem: true },
    );
    expect(getProbe().pastedText).toBe("");
    expect(getProbe().pastedTitle).toBe("");
    expect(getProbe().pastedAuthor).toBe("");
  });

  it("guards against a same-frame double 'Add to queue' click", async () => {
    let resolveUpload!: (value: { source_handle: string; original_name: string; preflight: null }) => void;
    const uploadSourceText = vi.fn(
      () =>
        new Promise<{ source_handle: string; original_name: string; preflight: null }>((resolve) => {
          resolveUpload = resolve;
        }),
    );
    const addPastedSource = vi.fn();
    await act(async () => {
      root = createRoot(container);
      root.render(
        <Probe
          apiClient={{ uploadSourceText }}
          conversionJob={{ clearFormError: vi.fn() }}
          queueController={{
            selectedQueueId: null,
            sourcePath: "",
            selectedQueueItem: null,
            titleOverride: "",
            authorOverride: "",
            addPastedSource,
          }}
        />,
      );
    });
    const getProbe = () =>
      (globalThis as typeof globalThis & { sourceQueueProbe: ReturnType<typeof useConversionSourceQueue> })
        .sourceQueueProbe;

    await act(async () => {
      getProbe().setPastedText("Body text");
    });

    // Two clicks dispatched before the first upload resolves (same frame).
    await act(async () => {
      const first = getProbe().onAddPastedText();
      const second = getProbe().onAddPastedText();
      resolveUpload({ source_handle: "upload:1_hash_paste.md", original_name: "paste.md", preflight: null });
      await Promise.all([first, second]);
    });

    expect(uploadSourceText).toHaveBeenCalledTimes(1);
    expect(addPastedSource).toHaveBeenCalledTimes(1);
  });

  it("preserves the complete paste draft when upload fails", async () => {
    const uploadSourceText = vi.fn(async () => {
      throw new Error("upload unavailable");
    });
    await act(async () => {
      root = createRoot(container);
      root.render(
        <Probe
          apiClient={{ uploadSourceText }}
          queueController={{
            selectedQueueId: null,
            sourcePath: "",
            selectedQueueItem: null,
            titleOverride: "",
            authorOverride: "",
            addPastedSource: vi.fn(),
          }}
        />,
      );
    });
    const getProbe = () =>
      (globalThis as typeof globalThis & { sourceQueueProbe: ReturnType<typeof useConversionSourceQueue> })
        .sourceQueueProbe;

    await act(async () => {
      getProbe().setPastedText("Body text");
      getProbe().setPastedTitle("Draft title");
      getProbe().setPastedAuthor("Draft author");
    });
    let returnedItem: Awaited<ReturnType<ReturnType<typeof useConversionSourceQueue>["onAddPastedText"]>> = null;
    await act(async () => {
      returnedItem = await getProbe().onAddPastedText();
    });

    expect(returnedItem).toBeNull();
    expect(getProbe().pastedText).toBe("Body text");
    expect(getProbe().pastedTitle).toBe("Draft title");
    expect(getProbe().pastedAuthor).toBe("Draft author");
    expect(getProbe().pasteStatus).toBe("upload unavailable");
  });
});
