// @vitest-environment happy-dom

import { act, StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useConversionQueueController } from "./useConversionQueueController";
import { useConversionSourceQueue } from "./useConversionSourceQueue";

const { invokeMock, listenMock } = vi.hoisted(() => ({
  invokeMock: vi.fn(),
  listenMock: vi.fn(async () => vi.fn()),
}));

vi.mock("@tauri-apps/api/core", () => ({ invoke: invokeMock }));
vi.mock("@tauri-apps/api/event", () => ({ listen: listenMock }));

interface ProbeProps {
  apiClient?: unknown;
  conversionMode?: "audiobook" | "document_audio";
  conversionJob?: unknown;
  queueController?: unknown;
  setPreflightStatus?: (value: string) => void;
}

function Probe({
  apiClient = null,
  conversionMode = "audiobook",
  conversionJob,
  queueController,
  setPreflightStatus = vi.fn(),
}: ProbeProps): null {
  const result = useConversionSourceQueue({
    apiClient: apiClient as never,
    conversionMode,
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
    invokeMock.mockReset();
    listenMock.mockReset();
    listenMock.mockImplementation(async () => vi.fn());
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

  it("bounds concurrent uploads and keeps input order after out-of-order completion", async () => {
    type Response = { source_handle: string; original_name: string; preflight: null };
    const resolvers = new Map<string, (value: Response) => void>();
    const uploadSourceFile = vi.fn((file: File) => new Promise<Response>((resolve) => {
      resolvers.set(file.name, resolve);
    }));
    const addQueueItems = vi.fn();
    await act(async () => {
      root = createRoot(container);
      root.render(<Probe apiClient={{ uploadSourceFile }} queueController={{
        sourcePath: "", selectedQueueId: null, addQueueItems, setPreflightStatus: vi.fn(),
      }} />);
    });
    const getProbe = () => (globalThis as typeof globalThis & {
      sourceQueueProbe: ReturnType<typeof useConversionSourceQueue>;
    }).sourceQueueProbe;
    const event = (names: string[]) => ({ target: {
      files: names.map((name) => new File([name], name)), value: "selected",
    } }) as never;
    const first = getProbe().onBrowserFilesSelected(event(["a.md", "b.md", "c.md"]));
    const second = getProbe().onBrowserFilesSelected(event(["d.md"]));
    await act(async () => {});
    expect(uploadSourceFile).toHaveBeenCalledTimes(2);
    const finish = async (name: string) => act(async () => {
      resolvers.get(name)!({ source_handle: `upload:${name}`, original_name: name, preflight: null });
    });
    await finish("b.md");
    expect(uploadSourceFile).toHaveBeenCalledTimes(3);
    await finish("c.md");
    expect(uploadSourceFile).toHaveBeenCalledTimes(4);
    await finish("d.md");
    await finish("a.md");
    await act(async () => { await Promise.all([first, second]); });
    expect(addQueueItems).toHaveBeenLastCalledWith([
      expect.objectContaining({ label: "a.md" }),
      expect.objectContaining({ label: "b.md" }),
      expect.objectContaining({ label: "c.md" }),
    ], { selectFirst: false });
  });

  it("does not start waiting uploads after their source context changes", async () => {
    const releases: Array<() => void> = [];
    const uploadSourceFile = vi.fn((file: File) => new Promise((resolve) => {
      releases.push(() => resolve({ source_handle: `upload:${file.name}`, original_name: file.name, preflight: null }));
    }));
    const apiClient = { uploadSourceFile };
    const addQueueItems = vi.fn();
    const queueController = { sourcePath: "", selectedQueueId: null, addQueueItems, setPreflightStatus: vi.fn() };
    await act(async () => {
      root = createRoot(container);
      root.render(<Probe apiClient={apiClient} queueController={queueController} />);
    });
    const probe = (globalThis as typeof globalThis & {
      sourceQueueProbe: ReturnType<typeof useConversionSourceQueue>;
    }).sourceQueueProbe;
    const batch = probe.onBrowserFilesSelected({ target: {
      files: ["a.md", "b.md", "c.md"].map((name) => new File([name], name)), value: "selected",
    } } as never);
    expect(uploadSourceFile).toHaveBeenCalledTimes(2);
    await act(async () => {
      root.render(<Probe apiClient={{ uploadSourceFile: vi.fn() }} queueController={queueController} />);
    });
    await act(async () => {
      releases.forEach((release) => release());
      await batch;
    });
    expect(uploadSourceFile).toHaveBeenCalledTimes(2);
    expect(addQueueItems).not.toHaveBeenCalled();
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

  it("uploads normal browser file drops through the same source intake as the picker", async () => {
    Reflect.deleteProperty(window, "__TAURI_INTERNALS__");
    const file = new File(["A dropped document"], "dropped.md", { type: "text/markdown" });
    const uploadSourceFile = vi.fn(async () => ({ source_handle: "upload:dropped", original_name: file.name, preflight: null }));
    const addQueueItems = vi.fn();
    const setPreflightStatus = vi.fn();
    await act(async () => {
      root = createRoot(container);
      root.render(<Probe apiClient={{ uploadSourceFile }} queueController={{
        sourcePath: "", selectedQueueId: null, addQueueItems, setPreflightStatus,
      }} />);
    });
    const preventDefault = vi.fn();
    await act(async () => {
      (globalThis as typeof globalThis & { sourceQueueProbe: ReturnType<typeof useConversionSourceQueue> })
        .sourceQueueProbe.onDropZoneDrop({ preventDefault, dataTransfer: { getData: () => "", files: [file] } } as never);
    });
    expect(preventDefault).toHaveBeenCalled();
    expect(uploadSourceFile).toHaveBeenCalledWith(file);
    expect(addQueueItems).toHaveBeenCalledWith([
      expect.objectContaining({ sourcePath: "upload:dropped", label: "dropped.md" }),
    ], { selectFirst: true });
    expect(setPreflightStatus).toHaveBeenLastCalledWith("");
  });

  it("keeps current source callbacks live through StrictMode effect replay", async () => {
    invokeMock.mockResolvedValueOnce(["/strict-mode.md"]);
    const addQueueItems = vi.fn();
    await act(async () => {
      root = createRoot(container);
      root.render(
        <StrictMode>
          <Probe
            conversionJob={{ clearFormError: vi.fn() }}
            queueController={{
              selectedQueueId: null,
              sourcePath: "",
              addQueueItems,
              setPreflightStatus: vi.fn(),
            }}
          />
        </StrictMode>,
      );
    });

    await act(async () => {
      await (
        globalThis as typeof globalThis & {
          sourceQueueProbe: ReturnType<typeof useConversionSourceQueue>;
        }
      ).sourceQueueProbe.onBrowse();
    });

    expect(addQueueItems).toHaveBeenCalledWith(
      [expect.objectContaining({ sourcePath: "/strict-mode.md" })],
      { selectFirst: true },
    );
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
    // The file that was not added is named, with its reason.
    expect(setPreflightStatus).toHaveBeenLastCalledWith(`Added 2 sources. Not added: ${secondFile.name}: second upload failed`);
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
    expect(setPreflightStatus).toHaveBeenLastCalledWith("Adding stale.md…");
  });

  it("queues concurrent browser file selections and uses the latest selection for final status", async () => {
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

    // Let the failed older upload release its slot to the newer batch.
    await act(async () => {});
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
    expect(setPreflightStatus).toHaveBeenLastCalledWith("");
  });

  it.each(["api client", "conversion mode"] as const)(
    "keeps a browser upload only when the %s change still belongs to the same backend",
    async (changedContext) => {
      type UploadResponse = { source_handle: string; original_name: string; preflight: null };
      let resolveUpload!: (value: UploadResponse) => void;
      const uploadSourceFile = vi.fn(
        () =>
          new Promise<UploadResponse>((resolve) => {
            resolveUpload = resolve;
          }),
      );
      const originalApiClient = { uploadSourceFile };
      const replacementApiClient = { uploadSourceFile: vi.fn() };
      const addQueueItems = vi.fn();
      const setPreflightStatus = vi.fn();
      const clearFormError = vi.fn();
      const queueController = {
        selectedQueueId: null,
        sourcePath: "",
        addQueueItems,
        setPreflightStatus,
      };

      await act(async () => {
        root = createRoot(container);
        root.render(
          <Probe
            apiClient={originalApiClient}
            conversionJob={{ clearFormError }}
            queueController={queueController}
          />,
        );
      });
      const uploadPromise = (
        globalThis as typeof globalThis & {
          sourceQueueProbe: ReturnType<typeof useConversionSourceQueue>;
        }
      ).sourceQueueProbe.onBrowserFilesSelected({
        target: { files: [new File(["old"], "old.md")], value: "old" },
      } as never);

      await act(async () => {
        root.render(
          <Probe
            apiClient={changedContext === "api client" ? replacementApiClient : originalApiClient}
            conversionMode={changedContext === "conversion mode" ? "document_audio" : "audiobook"}
            conversionJob={{ clearFormError }}
            queueController={queueController}
          />,
        );
      });
      resolveUpload({ source_handle: "upload:old", original_name: "old.md", preflight: null });
      await act(async () => {
        await uploadPromise;
      });

      if (changedContext === "api client") {
        expect(addQueueItems).not.toHaveBeenCalled();
        expect(clearFormError).not.toHaveBeenCalled();
        expect(setPreflightStatus).toHaveBeenCalledTimes(1);
      } else {
        expect(addQueueItems).toHaveBeenCalledWith([
          expect.objectContaining({ sourcePath: "upload:old", conversionMode: "document_audio" }),
        ], { selectFirst: true });
        expect(setPreflightStatus).toHaveBeenLastCalledWith("");
      }
    },
  );

  it.each(["api client", "conversion mode", "unmount"] as const)(
    "accepts a deferred native browse result only in its original mounted backend after %s",
    async (staleReason) => {
      let resolvePicker!: (paths: string[]) => void;
      invokeMock.mockImplementationOnce(
        () =>
          new Promise<string[]>((resolve) => {
            resolvePicker = resolve;
          }),
      );
      const originalApiClient = {};
      const replacementApiClient = {};
      const addQueueItems = vi.fn();
      const clearFormError = vi.fn();
      const queueController = {
        selectedQueueId: null,
        sourcePath: "",
        addQueueItems,
        setPreflightStatus: vi.fn(),
      };

      await act(async () => {
        root = createRoot(container);
        root.render(
          <Probe
            apiClient={originalApiClient}
            conversionJob={{ clearFormError }}
            queueController={queueController}
          />,
        );
      });
      const browsePromise = (
        globalThis as typeof globalThis & {
          sourceQueueProbe: ReturnType<typeof useConversionSourceQueue>;
        }
      ).sourceQueueProbe.onBrowse();

      await act(async () => {
        if (staleReason === "unmount") {
          root.unmount();
          return;
        }
        root.render(
          <Probe
            apiClient={staleReason === "api client" ? replacementApiClient : originalApiClient}
            conversionMode={staleReason === "conversion mode" ? "document_audio" : "audiobook"}
            conversionJob={{ clearFormError }}
            queueController={queueController}
          />,
        );
      });
      resolvePicker(["/old.md"]);
      await act(async () => {
        await browsePromise;
      });

      if (staleReason === "conversion mode") {
        expect(addQueueItems).toHaveBeenCalledWith([
          expect.objectContaining({ sourcePath: "/old.md", conversionMode: "document_audio" }),
        ], { selectFirst: true });
      } else {
        expect(addQueueItems).not.toHaveBeenCalled();
        expect(clearFormError).not.toHaveBeenCalled();
      }
    },
  );

  it("claims first selection synchronously across native browse and drop paths", async () => {
    invokeMock.mockResolvedValueOnce(["/native.md"]);
    const addQueueItems = vi.fn();
    await act(async () => {
      root = createRoot(container);
      root.render(
        <Probe
          conversionJob={{ clearFormError: vi.fn() }}
          queueController={{
            selectedQueueId: null,
            sourcePath: "",
            addQueueItems,
            setPreflightStatus: vi.fn(),
          }}
        />,
      );
    });
    const getProbe = () =>
      (globalThis as typeof globalThis & { sourceQueueProbe: ReturnType<typeof useConversionSourceQueue> })
        .sourceQueueProbe;

    await act(async () => {
      await getProbe().onBrowse();
    });
    act(() => {
      getProbe().onDropZoneDrop({
        preventDefault: vi.fn(),
        dataTransfer: {
          getData: () => "file:///dropped.md",
          files: [],
        },
      } as never);
    });

    expect(addQueueItems).toHaveBeenNthCalledWith(
      1,
      [expect.objectContaining({ sourcePath: "/native.md" })],
      { selectFirst: true },
    );
    expect(addQueueItems).toHaveBeenNthCalledWith(
      2,
      [expect.objectContaining({ sourcePath: "/dropped.md" })],
      { selectFirst: false },
    );
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

  it.each(["all fields", "text", "title", "author"])("preserves edits to %s made while a paste upload is pending", async (edited) => {
    let resolveUpload!: (value: { source_handle: string; original_name: string; preflight: null }) => void;
    const uploadSourceText = vi.fn(() => new Promise<{ source_handle: string; original_name: string; preflight: null }>((resolve) => {
      resolveUpload = resolve;
    }));
    const addPastedSource = vi.fn();
    await act(async () => {
      root = createRoot(container);
      root.render(<Probe apiClient={{ uploadSourceText }} queueController={{
        sourcePath: "", selectedQueueId: null, addPastedSource,
      }} />);
    });
    const getProbe = () => (globalThis as typeof globalThis & {
      sourceQueueProbe: ReturnType<typeof useConversionSourceQueue>;
    }).sourceQueueProbe;
    await act(async () => {
      getProbe().setPastedText("  Submitted text  ");
      getProbe().setPastedTitle("  Submitted title  ");
      getProbe().setPastedAuthor("  Submitted author  ");
    });
    let pending!: ReturnType<ReturnType<typeof useConversionSourceQueue>["onAddPastedText"]>;
    await act(async () => { pending = getProbe().onAddPastedText(); });
    await act(async () => {
      if (edited === "all fields" || edited === "text") getProbe().setPastedText("New text");
      if (edited === "all fields" || edited === "title") getProbe().setPastedTitle("New title");
      if (edited === "all fields" || edited === "author") getProbe().setPastedAuthor("New author");
    });
    await act(async () => {
      resolveUpload({ source_handle: "upload:submitted.md", original_name: "submitted.md", preflight: null });
      await pending;
    });
    expect(uploadSourceText).toHaveBeenCalledWith({ text: "Submitted text", title: "Submitted title" });
    expect(addPastedSource).toHaveBeenCalledWith(expect.objectContaining({ title: "Submitted title", author: "Submitted author" }), { selectItem: true });
    expect(getProbe().pastedText).toBe(edited === "all fields" || edited === "text" ? "New text" : "");
    expect(getProbe().pastedTitle).toBe(edited === "all fields" || edited === "title" ? "New title" : "");
    expect(getProbe().pastedAuthor).toBe(edited === "all fields" || edited === "author" ? "New author" : "");
  });

  it("selects a manually inspected source so its preflight can gate Start", async () => {
    const addQueueItems = vi.fn();
    const queueController = {
      sourcePath: "/tmp/invalid.pdf",
      selectedQueueId: null,
      selectedQueueItem: null,
      titleOverride: "",
      authorOverride: "",
      addQueueItems,
      setPreflightStatus: vi.fn(),
    };
    await act(async () => {
      root = createRoot(container);
      root.render(<Probe apiClient={{}} queueController={queueController} />);
    });

    const getProbe = () =>
      (globalThis as typeof globalThis & { sourceQueueProbe: ReturnType<typeof useConversionSourceQueue> })
        .sourceQueueProbe;
    await act(async () => getProbe().onInspectCurrentSource());

    expect(addQueueItems).toHaveBeenCalledWith(
      [expect.objectContaining({ sourcePath: "/tmp/invalid.pdf", status: "preflighting" })],
      { selectFirst: true },
    );
  });

  it.each(["api client", "conversion mode"] as const)(
    "keeps a pasted-source completion only when the %s change still belongs to the same backend",
    async (changedContext) => {
      type UploadResponse = { source_handle: string; original_name: string; preflight: null };
      let resolveUpload!: (value: UploadResponse) => void;
      const uploadSourceText = vi.fn(
        () =>
          new Promise<UploadResponse>((resolve) => {
            resolveUpload = resolve;
          }),
      );
      const originalApiClient = { uploadSourceText };
      const replacementApiClient = { uploadSourceText: vi.fn() };
      const addPastedSource = vi.fn();
      const clearFormError = vi.fn();
      const queueController = {
        selectedQueueId: null,
        sourcePath: "",
        selectedQueueItem: null,
        titleOverride: "",
        authorOverride: "",
        addPastedSource,
      };

      await act(async () => {
        root = createRoot(container);
        root.render(
          <Probe
            apiClient={originalApiClient}
            conversionJob={{ clearFormError }}
            queueController={queueController}
          />,
        );
      });
      const getProbe = () =>
        (globalThis as typeof globalThis & { sourceQueueProbe: ReturnType<typeof useConversionSourceQueue> })
          .sourceQueueProbe;
      await act(async () => {
        getProbe().setPastedText("Context-owned draft");
      });
      let pastePromise!: ReturnType<ReturnType<typeof useConversionSourceQueue>["onAddPastedText"]>;
      act(() => {
        pastePromise = getProbe().onAddPastedText();
      });

      await act(async () => {
        root.render(
          <Probe
            apiClient={changedContext === "api client" ? replacementApiClient : originalApiClient}
            conversionMode={changedContext === "conversion mode" ? "document_audio" : "audiobook"}
            conversionJob={{ clearFormError }}
            queueController={queueController}
          />,
        );
      });
      resolveUpload({ source_handle: "upload:old-paste", original_name: "old.md", preflight: null });
      let returnedItem: Awaited<typeof pastePromise>;
      await act(async () => {
        returnedItem = await pastePromise;
      });

      if (changedContext === "api client") {
        expect(returnedItem!).toBeNull();
        expect(addPastedSource).not.toHaveBeenCalled();
        expect(clearFormError).not.toHaveBeenCalled();
        expect(getProbe().pastedText).toBe("Context-owned draft");
        expect(getProbe().pasteStatus).toBe("Idle");
      } else {
        expect(returnedItem!).toMatchObject({ sourcePath: "upload:old-paste", conversionMode: "document_audio" });
        expect(addPastedSource).toHaveBeenCalledWith(expect.objectContaining({ conversionMode: "document_audio" }), { selectItem: true });
        expect(getProbe().pastedText).toBe("");
        expect(getProbe().pasteStatus).toBe("");
      }
      expect(getProbe().pasteBusy).toBe(false);
    },
  );

  it("ignores a pasted-source completion after unmount", async () => {
    type UploadResponse = { source_handle: string; original_name: string; preflight: null };
    let resolveUpload!: (value: UploadResponse) => void;
    const uploadSourceText = vi.fn(
      () =>
        new Promise<UploadResponse>((resolve) => {
          resolveUpload = resolve;
        }),
    );
    const addPastedSource = vi.fn();
    const clearFormError = vi.fn();

    await act(async () => {
      root = createRoot(container);
      root.render(
        <Probe
          apiClient={{ uploadSourceText }}
          conversionJob={{ clearFormError }}
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
      getProbe().setPastedText("Unmounted draft");
    });
    let pastePromise!: ReturnType<ReturnType<typeof useConversionSourceQueue>["onAddPastedText"]>;
    act(() => {
      pastePromise = getProbe().onAddPastedText();
    });

    await act(async () => {
      root.unmount();
    });
    resolveUpload({ source_handle: "upload:stale-paste", original_name: "stale.md", preflight: null });
    let returnedItem: Awaited<typeof pastePromise>;
    await act(async () => {
      returnedItem = await pastePromise;
    });

    expect(returnedItem!).toBeNull();
    expect(addPastedSource).not.toHaveBeenCalled();
    expect(clearFormError).not.toHaveBeenCalled();
  });

  it("keeps a current paste request guarded when an older context request settles", async () => {
    type UploadResponse = { source_handle: string; original_name: string; preflight: null };
    let resolveOldUpload!: (value: UploadResponse) => void;
    let resolveCurrentUpload!: (value: UploadResponse) => void;
    const oldUploadSourceText = vi.fn(
      () =>
        new Promise<UploadResponse>((resolve) => {
          resolveOldUpload = resolve;
        }),
    );
    const currentUploadSourceText = vi.fn(
      () =>
        new Promise<UploadResponse>((resolve) => {
          resolveCurrentUpload = resolve;
        }),
    );
    const addPastedSource = vi.fn();
    const clearFormError = vi.fn();
    const queueController = {
      selectedQueueId: null,
      sourcePath: "",
      selectedQueueItem: null,
      titleOverride: "",
      authorOverride: "",
      addPastedSource,
    };

    await act(async () => {
      root = createRoot(container);
      root.render(
        <Probe
          apiClient={{ uploadSourceText: oldUploadSourceText }}
          conversionJob={{ clearFormError }}
          queueController={queueController}
        />,
      );
    });
    const getProbe = () =>
      (globalThis as typeof globalThis & { sourceQueueProbe: ReturnType<typeof useConversionSourceQueue> })
        .sourceQueueProbe;
    await act(async () => {
      getProbe().setPastedText("Reusable draft");
    });
    let oldPastePromise!: ReturnType<ReturnType<typeof useConversionSourceQueue>["onAddPastedText"]>;
    act(() => {
      oldPastePromise = getProbe().onAddPastedText();
    });

    await act(async () => {
      root.render(
        <Probe
          apiClient={{ uploadSourceText: currentUploadSourceText }}
          conversionJob={{ clearFormError }}
          queueController={queueController}
        />,
      );
    });
    let currentPastePromise!: ReturnType<ReturnType<typeof useConversionSourceQueue>["onAddPastedText"]>;
    act(() => {
      currentPastePromise = getProbe().onAddPastedText();
    });
    expect(currentUploadSourceText).toHaveBeenCalledTimes(1);

    resolveOldUpload({ source_handle: "upload:old", original_name: "old.md", preflight: null });
    await act(async () => {
      await oldPastePromise;
    });
    let duplicateResult!: Awaited<ReturnType<ReturnType<typeof useConversionSourceQueue>["onAddPastedText"]>>;
    await act(async () => {
      duplicateResult = await getProbe().onAddPastedText();
    });
    expect(duplicateResult).toBeNull();
    expect(currentUploadSourceText).toHaveBeenCalledTimes(1);

    resolveCurrentUpload({ source_handle: "upload:current", original_name: "current.md", preflight: null });
    let currentResult: Awaited<typeof currentPastePromise>;
    await act(async () => {
      currentResult = await currentPastePromise;
    });

    expect(currentResult!).toEqual(expect.objectContaining({ sourcePath: "upload:current" }));
    expect(addPastedSource).toHaveBeenCalledTimes(1);
    expect(addPastedSource).toHaveBeenCalledWith(
      expect.objectContaining({ sourcePath: "upload:current" }),
      { selectItem: true },
    );
    expect(clearFormError).toHaveBeenCalledTimes(1);
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

describe("adding files that cannot all be queued", () => {
  it("names the file that was not added and the one already queued", async () => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    const unsupported = new Error("This file type is not supported. Choose a PDF, EPUB, TXT or Markdown file.");
    const uploadSourceFile = vi.fn(async (file: File) => {
      if (file.name.endsWith(".docx")) throw unsupported;
      return { source_handle: `upload:${file.name}`, original_name: file.name, preflight: null };
    });
    const addQueueItems = vi.fn();
    const setPreflightStatus = vi.fn();
    const queued = new File(["same"], "notes.md");
    const root = createRoot(document.createElement("div"));
    await act(async () => root.render(<Probe apiClient={{ uploadSourceFile }} queueController={{
      sourcePath: "", selectedQueueId: null, addQueueItems, setPreflightStatus,
      queueItems: [{ label: "notes.md", preflight: { size_bytes: queued.size } }],
    }} />));
    const probe = (globalThis as typeof globalThis & { sourceQueueProbe: ReturnType<typeof useConversionSourceQueue> }).sourceQueueProbe;
    await act(async () => {
      await probe.onBrowserFilesSelected({ target: {
        files: [queued, new File(["x"], "story.txt"), new File(["y"], "report.docx")], value: "selected",
      } } as never);
    });
    expect(uploadSourceFile).toHaveBeenCalledTimes(2);
    expect(addQueueItems).toHaveBeenCalledWith([expect.objectContaining({ label: "story.txt" })], expect.anything());
    expect(setPreflightStatus).toHaveBeenLastCalledWith(
      "Added 1 source. Not added: report.docx: This file type is not supported. Choose a PDF, EPUB, TXT or Markdown file. notes.md is already in the queue.",
    );
    await act(async () => root.unmount());
  });

  /** The source intake wired to the real queue, as the page mounts them. */
  function mountIntake(uploadSourceFile: (file: File) => Promise<unknown>) {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    const apiClient = { uploadSourceFile } as never;
    const view = {} as {
      queue: ReturnType<typeof useConversionQueueController>;
      intake: ReturnType<typeof useConversionSourceQueue>;
    };
    function Intake(): null {
      view.queue = useConversionQueueController({ apiClient });
      view.intake = useConversionSourceQueue({
        apiClient, conversionMode: "audiobook", conversionJob: { clearFormError: vi.fn() } as never, queueController: view.queue,
      });
      return null;
    }
    const root = createRoot(document.createElement("div"));
    const select = (names: string[]) => act(async () => {
      await view.intake.onBrowserFilesSelected({ target: {
        files: names.map((name) => new File([name], name)), value: "selected",
      } } as never);
    });
    return { view, root, select, Intake };
  }

  const uploadedPreflight = (title: string) => ({
    title, author: null, chapter_count: 1, source_type: "md", sample: "s", warnings: [], size_bytes: title.length,
    requires_ocr: false, pdf_text_layer: null,
  });

  it("still names the skipped files when two or more others were added", async () => {
    const unsupported = "This file type is not supported. Choose a PDF, EPUB, TXT or Markdown file.";
    const { view, root, select, Intake } = mountIntake(async (file) => {
      if (file.name.endsWith(".docx")) throw new Error(unsupported);
      if (file.name.endsWith(".txt")) throw new Error("The file is empty.");
      return { source_handle: `upload:${file.name}`, original_name: file.name, preflight: uploadedPreflight(file.name) };
    });
    await act(async () => root.render(<Intake />));
    await select(["a.md", "report.docx", "empty.txt", "b.md"]);
    expect(view.queue.queueItems.map((item) => item.label)).toEqual(["a.md", "b.md"]);
    expect(view.queue.preflightStatus).toBe(
      `Added 2 sources. Not added: report.docx: ${unsupported}; empty.txt: The file is empty.`,
    );
    await act(async () => root.unmount());
  });

  it("selects a newly added book when the selected row has already finished", async () => {
    const { view, root, select, Intake } = mountIntake(async (file) => (
      { source_handle: `upload:${file.name}`, original_name: file.name, preflight: uploadedPreflight(file.name) }
    ));
    await act(async () => root.render(<Intake />));
    await select(["first.md"]);
    const first = view.queue.queueItems[0];
    expect(view.queue.selectedQueueId).toBe(first.id);
    await select(["waiting.md"]);
    // An unfinished selection is the book being edited; it keeps the selection.
    expect(view.queue.selectedQueueId).toBe(first.id);

    await act(async () => view.queue.updateQueueItem(first.id, (item) => ({ ...item, status: "done", jobId: "job-1" })));
    await select(["next.md"]);
    const next = view.queue.queueItems.find((item) => item.label === "next.md")!;
    expect(view.queue.selectedQueueId).toBe(next.id);
    expect(view.queue.sourcePath).toBe("upload:next.md");
    await act(async () => root.unmount());
  });
});
