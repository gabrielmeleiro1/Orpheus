// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import type { UseLibraryResult } from "../library/useLibrary";
import { usePlaybackPersistence } from "./playbackPersistence";

function createLibrary(bookId = 1): UseLibraryResult {
  return {
    selectedBookId: bookId,
    selectedBookDetail: { playback_position_sec: 0 },
    setSelectedPlaybackPosition: vi.fn(),
  } as unknown as UseLibraryResult;
}

function createAudio(): HTMLAudioElement {
  return { src: "", currentTime: 0 } as unknown as HTMLAudioElement;
}

describe("playback persistence serialization", () => {
  let root: Root;
  let container: HTMLDivElement;
  let result: ReturnType<typeof usePlaybackPersistence>;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.restoreAllMocks();
  });

  async function mount(apiClient: ApiClient, library = createLibrary()): Promise<void> {
    function Probe() {
      result = usePlaybackPersistence({
        apiClient,
        library,
        audioRef: { current: createAudio() },
        loadedBookId: library.selectedBookId,
        onSaveFailed: vi.fn(),
      });
      return null;
    }
    await act(async () => {
      root = createRoot(container);
      root.render(<Probe />);
    });
  }

  async function mountWithSelection(
    apiClient: ApiClient,
    library: UseLibraryResult,
    onSaveFailed: () => void,
  ): Promise<{ selectBook: (bookId: number) => Promise<void> }> {
    let activeLoadedBookId = library.selectedBookId;
    function Probe() {
      result = usePlaybackPersistence({
        apiClient,
        library,
        audioRef: { current: createAudio() },
        loadedBookId: activeLoadedBookId,
        onSaveFailed,
      });
      return null;
    }
    await act(async () => {
      root = createRoot(container);
      root.render(<Probe />);
    });
    return {
      selectBook: async (bookId: number) => {
        library.selectedBookId = bookId;
        activeLoadedBookId = bookId;
        await act(async () => root.render(<Probe />));
      },
    };
  }

  it("waits for an earlier save before sending a newer position", async () => {
    let resolveFirst!: (value: { position_sec: number }) => void;
    const updateBookPlaybackPosition = vi
      .fn()
      .mockImplementationOnce(
        () => new Promise<{ position_sec: number }>((resolve) => { resolveFirst = resolve; }),
      )
      .mockResolvedValueOnce({ position_sec: 120 });
    await mount({ updateBookPlaybackPosition } as unknown as ApiClient);

    let first!: Promise<number | null>;
    let second!: Promise<number | null>;
    await act(async () => {
      first = result.persistBookPlaybackPosition(1, 60, { force: true });
      second = result.persistBookPlaybackPosition(1, 120, { force: true });
    });
    expect(updateBookPlaybackPosition).toHaveBeenCalledTimes(1);
    expect(updateBookPlaybackPosition).toHaveBeenLastCalledWith(1, 60);

    await act(async () => resolveFirst({ position_sec: 60 }));
    await vi.waitFor(() => expect(updateBookPlaybackPosition).toHaveBeenCalledTimes(2));
    expect(updateBookPlaybackPosition).toHaveBeenLastCalledWith(1, 120);
    await expect(first).resolves.toBe(60);
    await expect(second).resolves.toBe(120);
  });

  it("continues the queue after a failed save", async () => {
    let rejectFirst!: (error: Error) => void;
    const updateBookPlaybackPosition = vi
      .fn()
      .mockImplementationOnce(
        () => new Promise<{ position_sec: number }>((_, reject) => { rejectFirst = reject; }),
      )
      .mockResolvedValueOnce({ position_sec: 120 });
    await mount({ updateBookPlaybackPosition } as unknown as ApiClient);

    await act(async () => {
      void result.persistBookPlaybackPosition(1, 60, { force: true });
      void result.persistBookPlaybackPosition(1, 120, { force: true });
    });
    expect(updateBookPlaybackPosition).toHaveBeenCalledTimes(1);

    await act(async () => rejectFirst(new Error("offline")));
    await vi.waitFor(() => expect(updateBookPlaybackPosition).toHaveBeenCalledTimes(2));
    expect(updateBookPlaybackPosition).toHaveBeenLastCalledWith(1, 120);
  });

  it("does not throttle against an optimistic marker after queued failures", async () => {
    const updateBookPlaybackPosition = vi.fn()
      .mockRejectedValueOnce(new Error("offline"))
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce({ position_sec: 70 });
    await mount({ updateBookPlaybackPosition } as unknown as ApiClient);
    const first = result.persistBookPlaybackPosition(1, 60, { force: true, silent: true });
    const second = result.persistBookPlaybackPosition(1, 120, { force: true, silent: true });
    await Promise.all([first, second]);
    await result.persistBookPlaybackPosition(1, 70, { silent: true });
    expect(updateBookPlaybackPosition).toHaveBeenNthCalledWith(3, 1, 70);
  });

  it("serializes each book independently", async () => {
    let resolveBookOne!: (value: { position_sec: number }) => void;
    const updateBookPlaybackPosition = vi.fn((bookId: number) => {
      if (bookId === 1) {
        return new Promise<{ position_sec: number }>((resolve) => { resolveBookOne = resolve; });
      }
      return Promise.resolve({ position_sec: 20 });
    });
    await mount({ updateBookPlaybackPosition } as unknown as ApiClient);

    await act(async () => {
      void result.persistBookPlaybackPosition(1, 10, { force: true });
      void result.persistBookPlaybackPosition(2, 20, { force: true });
    });
    await vi.waitFor(() => expect(updateBookPlaybackPosition).toHaveBeenCalledWith(2, 20));
    expect(updateBookPlaybackPosition).toHaveBeenCalledTimes(2);
    expect(updateBookPlaybackPosition).toHaveBeenNthCalledWith(1, 1, 10);
    resolveBookOne({ position_sec: 10 });
  });

  it("ignores deferred silent failures from a book that is no longer selected", async () => {
    const failureResolvers: Array<(error: Error) => void> = [];
    const updateBookPlaybackPosition = vi.fn(
      () => new Promise<{ position_sec: number }>((_, reject) => {
        failureResolvers.push(reject);
      }),
    );
    const library = createLibrary(1);
    const onSaveFailed = vi.fn();
    const mounted = await mountWithSelection({ updateBookPlaybackPosition } as unknown as ApiClient, library, onSaveFailed);

    let saves!: Array<Promise<number | null>>;
    await act(async () => {
      saves = Array.from({ length: 5 }, (_, index) =>
        result.persistBookPlaybackPosition(1, 60 + index, { force: true, silent: true }),
      );
    });
    await vi.waitFor(() => expect(failureResolvers).toHaveLength(1));
    await mounted.selectBook(2);

    for (let index = 0; index < 5; index += 1) {
      failureResolvers[index](new Error("offline"));
      if (index < 4) {
        await vi.waitFor(() => expect(failureResolvers).toHaveLength(index + 2));
      }
    }
    await Promise.all(saves);

    expect(onSaveFailed).not.toHaveBeenCalled();
    expect(library.setSelectedPlaybackPosition).not.toHaveBeenCalled();
  });

  it("keeps silent failure thresholds independent for each book", async () => {
    const updateBookPlaybackPosition = vi.fn().mockRejectedValue(new Error("offline"));
    const library = createLibrary(1);
    const onSaveFailed = vi.fn();
    const mounted = await mountWithSelection({ updateBookPlaybackPosition } as unknown as ApiClient, library, onSaveFailed);

    await Promise.all(
      Array.from({ length: 4 }, (_, index) =>
        result.persistBookPlaybackPosition(1, 60 + index, { force: true, silent: true }),
      ),
    );
    await mounted.selectBook(2);
    await result.persistBookPlaybackPosition(2, 60, { force: true, silent: true });

    expect(onSaveFailed).not.toHaveBeenCalled();
  });

  it("notifies when the currently loaded book reaches the silent failure threshold", async () => {
    const updateBookPlaybackPosition = vi.fn().mockRejectedValue(new Error("offline"));
    const onSaveFailed = vi.fn();
    await mountWithSelection({ updateBookPlaybackPosition } as unknown as ApiClient, createLibrary(1), onSaveFailed);

    await Promise.all(
      Array.from({ length: 5 }, (_, index) =>
        result.persistBookPlaybackPosition(1, 60 + index, { force: true, silent: true }),
      ),
    );

    expect(onSaveFailed).toHaveBeenCalledTimes(1);
  });

  it("ignores non-silent failures from an old loaded book", async () => {
    let rejectSave!: (error: Error) => void;
    const updateBookPlaybackPosition = vi.fn(
      () => new Promise<{ position_sec: number }>((_, reject) => { rejectSave = reject; }),
    );
    const library = createLibrary(1);
    const onSaveFailed = vi.fn();
    const mounted = await mountWithSelection({ updateBookPlaybackPosition } as unknown as ApiClient, library, onSaveFailed);

    let save!: Promise<number | null>;
    await act(async () => {
      save = result.persistBookPlaybackPosition(1, 60, { force: true });
    });
    await vi.waitFor(() => expect(updateBookPlaybackPosition).toHaveBeenCalledTimes(1));
    await mounted.selectBook(2);
    rejectSave(new Error("offline"));
    await save;

    expect(onSaveFailed).not.toHaveBeenCalled();
  });
});
