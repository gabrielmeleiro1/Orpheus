// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import type { UseLibraryResult } from "../library/useLibrary";
import { readPlaybackCheckpoint, writePlaybackCheckpoint } from "./playbackCheckpoint";
import { usePlaybackPersistence } from "./playbackPersistence";

function checkpointIdentity(bookId: number): string {
  return JSON.stringify([
    bookId,
    `created-${bookId}`,
    `/source/${bookId}.epub`,
  ]);
}

function createLibrary(bookId = 1): UseLibraryResult {
  return {
    selectedBookId: bookId,
    selectedBookDetail: {
      id: bookId,
      created_at: `created-${bookId}`,
      source_path: `/source/${bookId}.epub`,
      playback_path: `/library/${bookId}.m4b`,
      playback_position_sec: 0,
    },
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
    window.localStorage.clear();
    container = document.createElement("div");
    document.body.append(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    window.localStorage.clear();
    container.remove();
    vi.restoreAllMocks();
  });

  async function mount(
    apiClient: ApiClient,
    library = createLibrary(),
    audio = createAudio(),
  ): Promise<void> {
    function Probe() {
      result = usePlaybackPersistence({
        apiClient,
        library,
        audioRef: { current: audio },
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

  async function mountWithClientSwap(
    initialApiClient: ApiClient,
    library = createLibrary(),
    onSaveFailed = vi.fn(),
  ): Promise<{ swapClient: (apiClient: ApiClient) => Promise<void> }> {
    let activeApiClient = initialApiClient;
    function Probe() {
      result = usePlaybackPersistence({
        apiClient: activeApiClient,
        library,
        audioRef: { current: createAudio() },
        loadedBookId: library.selectedBookId,
        onSaveFailed,
      });
      return null;
    }
    await act(async () => {
      root = createRoot(container);
      root.render(<Probe />);
    });
    return {
      swapClient: async (apiClient: ApiClient) => {
        activeApiClient = apiClient;
        await act(async () => root.render(<Probe />));
      },
    };
  }

  it("restores a book from its local checkpoint instead of an older server position", async () => {
    const apiClient = {
      updateBookPlaybackPosition: vi.fn(),
    } as unknown as ApiClient;
    await mount(apiClient);
    writePlaybackCheckpoint(1, 183.75, checkpointIdentity(1));
    const audio = createAudio();
    Object.assign(audio, { duration: 300 });

    result.setPendingResume(1, 120, checkpointIdentity(1));
    result.restorePendingResumePosition(audio, 1);

    expect(audio.currentTime).toBe(183.75);
  });

  it("checkpoints the exact loaded position synchronously during app close", async () => {
    const apiClient = {
      updateBookPlaybackPosition: vi.fn(
        () => new Promise<{ position_sec: number }>(() => {}),
      ),
    } as unknown as ApiClient;
    const audio = createAudio();
    Object.assign(audio, {
      src: "http://desktop.test/book",
      currentTime: 183.75,
    });
    await mount(apiClient, createLibrary(1), audio);
    result.setPendingResume(1, 0, checkpointIdentity(1));

    window.dispatchEvent(new Event("beforeunload"));

    expect(readPlaybackCheckpoint(1, checkpointIdentity(1))).toBe(183.75);
  });

  it("does not erase an unplayed book when selection moves elsewhere", async () => {
    const updateBookPlaybackPosition = vi.fn();
    writePlaybackCheckpoint(1, 180, checkpointIdentity(1));
    await mount(
      { updateBookPlaybackPosition } as unknown as ApiClient,
      createLibrary(2),
      createAudio(),
    );

    await result.persistLoadedBookPlaybackPosition({
      bookId: 1,
      force: true,
      silent: true,
    });

    expect(readPlaybackCheckpoint(1, checkpointIdentity(1))).toBe(180);
    expect(updateBookPlaybackPosition).not.toHaveBeenCalled();
  });

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

  it("ignores a stale success from an earlier API client generation", async () => {
    let resolveOldSave!: (value: { position_sec: number }) => void;
    const oldClient = {
      updateBookPlaybackPosition: vi.fn(
        () => new Promise<{ position_sec: number }>((resolve) => { resolveOldSave = resolve; }),
      ),
    } as unknown as ApiClient;
    const newClient = {
      updateBookPlaybackPosition: vi.fn().mockResolvedValue({ position_sec: 90 }),
    } as unknown as ApiClient;
    const library = createLibrary(1);
    const mounted = await mountWithClientSwap(oldClient, library);

    let staleSave!: Promise<number | null>;
    await act(async () => {
      staleSave = result.persistBookPlaybackPosition(1, 60, { force: true });
    });
    await vi.waitFor(() => expect(oldClient.updateBookPlaybackPosition).toHaveBeenCalledTimes(1));

    await mounted.swapClient(newClient);
    await act(async () => resolveOldSave({ position_sec: 60 }));

    await expect(staleSave).resolves.toBeNull();
    expect(library.setSelectedPlaybackPosition).not.toHaveBeenCalled();

    await expect(result.persistBookPlaybackPosition(1, 90, { force: true })).resolves.toBe(90);
    expect(newClient.updateBookPlaybackPosition).toHaveBeenCalledWith(1, 90);
    expect(library.setSelectedPlaybackPosition).toHaveBeenCalledWith(90);
  });

  it("starts non-force saves on a new client without old tails or markers", async () => {
    let resolveHungSave!: (value: { position_sec: number }) => void;
    const oldClient = {
      updateBookPlaybackPosition: vi
        .fn()
        .mockResolvedValueOnce({ position_sec: 60 })
        .mockImplementationOnce(
          () => new Promise<{ position_sec: number }>((resolve) => { resolveHungSave = resolve; }),
        ),
    } as unknown as ApiClient;
    const newClient = {
      updateBookPlaybackPosition: vi.fn().mockResolvedValue({ position_sec: 65 }),
    } as unknown as ApiClient;
    const library = createLibrary(1);
    const mounted = await mountWithClientSwap(oldClient, library);

    await expect(result.persistBookPlaybackPosition(1, 60, { force: true })).resolves.toBe(60);
    let hungSave!: Promise<number | null>;
    await act(async () => {
      hungSave = result.persistBookPlaybackPosition(1, 61, { force: true });
    });
    await vi.waitFor(() => expect(oldClient.updateBookPlaybackPosition).toHaveBeenCalledTimes(2));

    await mounted.swapClient(newClient);
    const currentSave = result.persistBookPlaybackPosition(1, 65, { silent: true });
    await vi.waitFor(() => expect(newClient.updateBookPlaybackPosition).toHaveBeenCalledWith(1, 65));
    await expect(currentSave).resolves.toBe(65);

    resolveHungSave({ position_sec: 61 });
    await expect(hungSave).resolves.toBeNull();
    expect(library.setSelectedPlaybackPosition).toHaveBeenCalledWith(65);
  });

  it("does not let an old client callback overwrite the current checkpoint", async () => {
    const oldClient = {
      updateBookPlaybackPosition: vi.fn().mockResolvedValue({ position_sec: 30 }),
    } as unknown as ApiClient;
    const newClient = {
      updateBookPlaybackPosition: vi.fn().mockResolvedValue({ position_sec: 90 }),
    } as unknown as ApiClient;
    const library = createLibrary(1);
    const mounted = await mountWithClientSwap(oldClient, library);
    const stalePersist = result.persistBookPlaybackPosition;

    await mounted.swapClient(newClient);
    await result.persistBookPlaybackPosition(1, 90, { force: true });
    await stalePersist(1, 30, { force: true });

    expect(readPlaybackCheckpoint(1, checkpointIdentity(1))).toBe(90);
    expect(oldClient.updateBookPlaybackPosition).not.toHaveBeenCalled();
  });

  it("ignores a stale failure from an earlier API client generation", async () => {
    let rejectOldSave!: (error: Error) => void;
    const oldClient = {
      updateBookPlaybackPosition: vi.fn(
        () => new Promise<{ position_sec: number }>((_, reject) => { rejectOldSave = reject; }),
      ),
    } as unknown as ApiClient;
    const newClient = {
      updateBookPlaybackPosition: vi.fn().mockResolvedValue({ position_sec: 90 }),
    } as unknown as ApiClient;
    const onSaveFailed = vi.fn();
    const mounted = await mountWithClientSwap(oldClient, createLibrary(1), onSaveFailed);

    let staleSave!: Promise<number | null>;
    await act(async () => {
      staleSave = result.persistBookPlaybackPosition(1, 60, { force: true });
    });
    await vi.waitFor(() => expect(oldClient.updateBookPlaybackPosition).toHaveBeenCalledTimes(1));

    await mounted.swapClient(newClient);
    await act(async () => rejectOldSave(new Error("backend replaced")));

    await expect(staleSave).resolves.toBeNull();
    expect(onSaveFailed).not.toHaveBeenCalled();
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
