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

function Probe({ setPreflightStatus }: { setPreflightStatus: (value: string) => void }): null {
  const result = useConversionSourceQueue({
    apiClient: null,
    conversionMode: "audiobook",
    conversionJob: {
      clearFormError: vi.fn(),
    } as never,
    queueController: {
      selectedQueueId: null,
      sourcePath: "",
      addQueueItems: vi.fn(),
      setPreflightStatus,
    } as never,
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
});
