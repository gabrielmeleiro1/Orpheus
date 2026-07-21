// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useConversionJob } from "./useConversionJob";

const { refreshActiveConversionState } = vi.hoisted(() => ({
  refreshActiveConversionState: vi.fn(),
}));

vi.mock("../events/useEventStream", () => ({
  useEventStream: () => ({
    activeJobId: "job-1",
    refreshActiveConversionState,
    uiState: "running",
  }),
}));

function Probe(): null {
  const result = useConversionJob({ apiClient: {} as never, baseUrl: "http://desktop.test" });
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
});
