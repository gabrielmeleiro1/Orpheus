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
});
