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
    await act(async () => {
      await getProbe().onAddPastedText();
    });

    expect(uploadSourceText).toHaveBeenCalledWith({ text: "Body text", title: "Shared title" });
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
    await act(async () => {
      await getProbe().onAddPastedText();
    });

    expect(getProbe().pastedText).toBe("Body text");
    expect(getProbe().pastedTitle).toBe("Draft title");
    expect(getProbe().pastedAuthor).toBe("Draft author");
    expect(getProbe().pasteStatus).toBe("upload unavailable");
  });
});
