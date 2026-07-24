// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CleaningPreviewResponse, LibraryBookDetail } from "../../types/api";
import type { UseLibraryResult } from "./useLibrary";
import { useLibraryPanelState, type UseLibraryPanelStateResult } from "./useLibraryPanelState";

const bookA = { id: 1, title: "A" } as LibraryBookDetail;
const bookB = { id: 2, title: "B" } as LibraryBookDetail;

function Probe({ library }: { library: UseLibraryResult }): null {
  const state = useLibraryPanelState({ library });
  (globalThis as typeof globalThis & { panelStateProbe?: UseLibraryPanelStateResult }).panelStateProbe = state;
  return null;
}

function getProbe(): UseLibraryPanelStateResult {
  return (globalThis as typeof globalThis & { panelStateProbe: UseLibraryPanelStateResult }).panelStateProbe;
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((promiseResolve) => {
    resolve = promiseResolve;
  });
  return { promise, resolve };
}

function libraryModel(selectedBookDetail: LibraryBookDetail, loadCleaningPreview: UseLibraryResult["loadCleaningPreview"]): UseLibraryResult {
  return {
    selectedBookDetail,
    selectedBookId: selectedBookDetail.id,
    loadCleaningPreview,
  } as UseLibraryResult;
}

describe("useLibraryPanelState cleaning preview ownership", () => {
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
    delete (globalThis as typeof globalThis & { panelStateProbe?: unknown }).panelStateProbe;
  });

  it("does not let an older preview replace a newer selected book or clear its spinner", async () => {
    const previewA = deferred<CleaningPreviewResponse>();
    const previewB = deferred<CleaningPreviewResponse>();
    const responseA: CleaningPreviewResponse = {
      book_id: 1,
      chapter_number: 1,
      title: "A",
      available: true,
      before_text: "A before",
      llm_input_text: "",
      cleaned_text: "A cleaned",
      trace_rows: [],
    };
    const responseB: CleaningPreviewResponse = {
      book_id: 2,
      chapter_number: 1,
      title: "B",
      available: true,
      before_text: "B before",
      llm_input_text: "",
      cleaned_text: "B cleaned",
      trace_rows: [],
    };
    const loadA = vi.fn(() => previewA.promise);
    const loadB = vi.fn(() => previewB.promise);
    const modelA = libraryModel(bookA, loadA);
    const modelB = libraryModel(bookB, loadB);

    await act(async () => {
      root = createRoot(container);
      root.render(<Probe library={modelA} />);
    });
    await act(async () => {
      void getProbe().loadCleaningPreview(1);
      await Promise.resolve();
    });
    expect(getProbe().cleaningPreviewLoading).toBe(true);

    await act(async () => {
      root.render(<Probe library={modelB} />);
    });
    await act(async () => {
      void getProbe().loadCleaningPreview(1);
      await Promise.resolve();
    });
    expect(getProbe().cleaningPreviewLoading).toBe(true);

    await act(async () => {
      previewA.resolve(responseA);
      await Promise.resolve();
    });
    expect(getProbe().cleaningPreview).toBeNull();
    expect(getProbe().cleaningPreviewLoading).toBe(true);

    await act(async () => {
      previewB.resolve(responseB);
      await Promise.resolve();
    });
    expect(getProbe().cleaningPreview).toEqual(responseB);
    expect(getProbe().cleaningPreviewLoading).toBe(false);
  });
});
