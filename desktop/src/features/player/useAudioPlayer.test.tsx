// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import type { UseLibraryResult } from "../library/useLibrary";
import type { LibraryBookDetail } from "../../types/api";
import {
  playbackCheckpointBookIdentity,
  readPlaybackCheckpoint,
  writePlaybackCheckpoint,
} from "./playbackCheckpoint";
import { useAudioPlayer } from "./useAudioPlayer";

function createBookDetail(): LibraryBookDetail {
  return {
    id: 7,
    title: "Book",
    author: null,
    source_path: null,
    source_type: "epub",
    conversion_mode: "audiobook",
    tags: "",
    favorite: false,
    source_available: true,
    total_chapters: 1,
    playback_path: "/library/7.m4b",
    playback_status: "ready",
    playback_position_sec: 0,
    chapters: [],
    playback_url: "/api/library/7/audio",
  };
}

function createLibraryMock(detail: LibraryBookDetail): UseLibraryResult {
  return {
    items: [{ id: detail.id, title: detail.title }],
    selectedBookId: detail.id,
    selectedBookIndex: 0,
    selectedBookDetail: detail,
    selectedBookPlayable: true,
    refreshLibrary: vi.fn(async () => {}),
    selectBook: vi.fn(async () => detail),
    setSelectedPlaybackPosition: vi.fn(),
  } as unknown as UseLibraryResult;
}

function createFakeAudio(): HTMLAudioElement {
  return {
    src: "",
    paused: true,
    ended: false,
    currentTime: 0,
    duration: Number.NaN,
    readyState: 1,
    volume: 1,
    muted: false,
    crossOrigin: null,
    pause: vi.fn(),
    play: vi.fn(async () => {}),
    load: vi.fn(),
    removeAttribute: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  } as unknown as HTMLAudioElement;
}

function renderPlayer(args: Parameters<typeof useAudioPlayer>[0]): {
  current: ReturnType<typeof useAudioPlayer> | null;
} {
  const result: { current: ReturnType<typeof useAudioPlayer> | null } = {
    current: null,
  };

  function TestComponent() {
    result.current = useAudioPlayer(args);
    return null;
  }

  renderToString(<TestComponent />);
  return result;
}

afterEach(() => {
  window.localStorage.clear();
  vi.restoreAllMocks();
});

describe("useAudioPlayer audio load failures", () => {
  it("plays chapter files with local seeking and whole-book checkpoints", async () => {
    const apiClient = {
      getLibraryAudioUrl: vi.fn(async () => "http://desktop.test/book?token=secret"),
      updateBookPlaybackPosition: vi.fn(async (_id: number, position: number) => ({ position_sec: position })),
    } as unknown as ApiClient;
    const detail = createBookDetail();
    detail.playback_status = "chapter_audio";
    detail.chapters = [
      { id: 1, number: 1, title: "one", duration_ms: 10000, mp3_path: "1.mp3" },
      { id: 2, number: 2, title: "missing", duration_ms: 0, mp3_path: null },
      { id: 3, number: 3, title: "three", duration_ms: 20000, mp3_path: "3.mp3" },
      { id: 4, number: 4, title: "four", duration_ms: 20000, mp3_path: "4.mp3" },
    ];
    detail.playback_position_sec = 13;
    const library = createLibraryMock(detail);
    let current!: ReturnType<typeof useAudioPlayer>;
    function Probe() { current = useAudioPlayer({ apiClient, library }); return null; }
    const root = createRoot(document.createElement("div"));
    await act(async () => root.render(<Probe />));
    const audio = createFakeAudio();
    Object.assign(audio, { duration: 20 });
    current.audioRef.current = audio;
    try {
      await act(async () => current.togglePlay());
      expect(new URL(audio.src).searchParams.get("chapter_number")).toBe("3");
      expect(new URL(audio.src).searchParams.get("token")).toBe("secret");
      expect(audio.currentTime).toBe(3);
      expect(current.currentChapter?.number).toBe(3);
      expect(current.durationSec).toBe(20);
      await act(async () => current.seekTo(7));
      expect(audio.currentTime).toBe(7);
      expect(readPlaybackCheckpoint(detail.id, playbackCheckpointBookIdentity(detail))).toBe(17);
      await act(async () => current.playRelative(-1));
      expect(current.currentChapter?.number).toBe(1);
      expect(audio.currentTime).toBe(0);
      expect(current.canGoPrevious).toBe(false);
      await act(async () => current.audioProps.onEnded());
      expect(current.currentChapter?.number).toBe(3);
      expect(new URL(audio.src).searchParams.get("chapter_number")).toBe("3");
      await act(async () => current.playRelative(1));
      expect(current.currentChapter?.number).toBe(4);
      expect(current.canGoNext).toBe(false);
      expect(library.selectBook).not.toHaveBeenCalled();
    } finally {
      await act(async () => root.unmount());
    }
  });

  it("shows chapter playback rejection visibly", async () => {
    const apiClient = { getLibraryAudioUrl: vi.fn(async () => "http://desktop.test/book") } as unknown as ApiClient;
    const detail = createBookDetail();
    const library = createLibraryMock(detail);
    let current!: ReturnType<typeof useAudioPlayer>;
    function Probe() { current = useAudioPlayer({ apiClient, library }); return null; }
    const root = createRoot(document.createElement("div"));
    await act(async () => root.render(<Probe />));
    const audio = createFakeAudio();
    audio.play = vi.fn(async () => { throw new Error("Blocked"); });
    current.audioRef.current = audio;
    await act(async () => current.jumpToChapter({ id: 1, number: 1, title: "one", duration_ms: 1000, mp3_path: null }));
    expect(current.title).toContain("Playback failed");
    await act(async () => root.unmount());
  });
  it("fails softly when the backend returns a malformed audio URL", async () => {
    const apiClient = {
      getLibraryAudioUrl: vi.fn(async () => "not a URL"),
      updateBookPlaybackPosition: vi.fn(async (_id: number, position: number) => ({
        position_sec: position,
      })),
    } as unknown as ApiClient;
    const detail = createBookDetail();
    const library = createLibraryMock(detail);
    let current!: ReturnType<typeof useAudioPlayer>;
    function Probe() {
      current = useAudioPlayer({ apiClient, library });
      return null;
    }
    const container = document.createElement("div");
    const root = createRoot(container);
    await act(async () => {
      root.render(<Probe />);
    });
    current.audioRef.current = createFakeAudio();

    await act(async () => {
      await current.togglePlay();
    });

    expect(current.title).toBe("Playback failed");
    expect(current.audioRef.current?.load).not.toHaveBeenCalled();
    await act(async () => root.unmount());
  });

  it("restores a book checkpoint only when Play loads the audio", async () => {
    const apiClient = {
      getLibraryAudioUrl: vi.fn(async () => "http://desktop.test/book"),
      updateBookPlaybackPosition: vi.fn(async (_id: number, position: number) => ({
        position_sec: position,
      })),
    } as unknown as ApiClient;
    const detail = createBookDetail();
    detail.playback_position_sec = 120;
    writePlaybackCheckpoint(detail.id, 183.75, playbackCheckpointBookIdentity(detail));
    const library = createLibraryMock(detail);
    let current!: ReturnType<typeof useAudioPlayer>;
    function Probe() {
      current = useAudioPlayer({ apiClient, library });
      return null;
    }
    const container = document.createElement("div");
    const root = createRoot(container);
    await act(async () => {
      root.render(<Probe />);
    });
    const audio = createFakeAudio();
    Object.assign(audio, { duration: 300 });
    current.audioRef.current = audio;

    expect(current.elapsedSec).toBe(0);
    await act(async () => {
      await current.togglePlay();
    });

    expect(audio.currentTime).toBe(183.75);
    expect(current.elapsedSec).toBe(183.75);
    await act(async () => root.unmount());
  });

  it("highlights a chapter immediately when its start has a fractional second", async () => {
    const apiClient = {
      getLibraryAudioUrl: vi.fn(async () => "http://desktop.test/book"),
      updateBookPlaybackPosition: vi.fn(async (_id: number, position: number) => ({
        position_sec: position,
      })),
    } as unknown as ApiClient;
    const detail = createBookDetail();
    detail.chapters = [
      { id: 1, number: 1, title: "one", duration_ms: 1500, mp3_path: null },
      { id: 2, number: 2, title: "two", duration_ms: 2000, mp3_path: null },
    ];
    const library = createLibraryMock(detail);
    let current!: ReturnType<typeof useAudioPlayer>;
    function Probe() {
      current = useAudioPlayer({ apiClient, library });
      return null;
    }
    const container = document.createElement("div");
    const root = createRoot(container);
    await act(async () => {
      root.render(<Probe />);
    });
    const audio = createFakeAudio();
    current.audioRef.current = audio;

    await act(async () => {
      await current.jumpToChapter(detail.chapters[1]);
    });

    expect(audio.currentTime).toBe(1.5);
    expect(audio.play).toHaveBeenCalledOnce();
    expect(current.elapsedSec).toBe(1.5);
    expect(current.currentChapter?.number).toBe(2);
    await act(async () => root.unmount());
  });

  it("checkpoints every media position update even when the UI second is unchanged", async () => {
    const apiClient = {
      getLibraryAudioUrl: vi.fn(async () => "http://desktop.test/book"),
      updateBookPlaybackPosition: vi.fn(async (_id: number, position: number) => ({
        position_sec: position,
      })),
    } as unknown as ApiClient;
    const detail = createBookDetail();
    const identity = playbackCheckpointBookIdentity(detail);
    const library = createLibraryMock(detail);
    let current!: ReturnType<typeof useAudioPlayer>;
    function Probe() {
      current = useAudioPlayer({ apiClient, library });
      return null;
    }
    const container = document.createElement("div");
    const root = createRoot(container);
    await act(async () => {
      root.render(<Probe />);
    });
    const audio = createFakeAudio();
    current.audioRef.current = audio;
    await act(async () => {
      await current.togglePlay();
    });
    Object.assign(audio, { paused: false, currentTime: 12.25 });
    current.audioProps.onTimeUpdate({} as never);
    Object.assign(audio, { currentTime: 12.5 });
    current.audioProps.onTimeUpdate({} as never);

    expect(readPlaybackCheckpoint(detail.id, identity)).toBe(12.5);
    await act(async () => root.unmount());
  });

  it("checkpoints the exact position when playback is paused natively", async () => {
    const apiClient = {
      getLibraryAudioUrl: vi.fn(async () => "http://desktop.test/book"),
      updateBookPlaybackPosition: vi.fn(async (_id: number, position: number) => ({
        position_sec: position,
      })),
    } as unknown as ApiClient;
    const detail = createBookDetail();
    const identity = playbackCheckpointBookIdentity(detail);
    const library = createLibraryMock(detail);
    let current!: ReturnType<typeof useAudioPlayer>;
    function Probe() {
      current = useAudioPlayer({ apiClient, library });
      return null;
    }
    const container = document.createElement("div");
    const root = createRoot(container);
    await act(async () => {
      root.render(<Probe />);
    });
    const audio = createFakeAudio();
    current.audioRef.current = audio;
    await act(async () => {
      await current.togglePlay();
    });
    Object.assign(audio, { paused: true, currentTime: 25.5 });

    current.audioProps.onPause();

    expect(readPlaybackCheckpoint(detail.id, identity)).toBe(25.5);
    await act(async () => root.unmount());
  });

  it("reloads audio when the API client changes even if the book id is reused", async () => {
    const oldClient = {
      getLibraryAudioUrl: vi.fn(async () => "http://desktop.test/old-book"),
      updateBookPlaybackPosition: vi.fn(async (_id: number, position: number) => ({
        position_sec: position,
      })),
    } as unknown as ApiClient;
    const newClient = {
      getLibraryAudioUrl: vi.fn(async () => "http://desktop.test/new-book"),
      updateBookPlaybackPosition: vi.fn(async (_id: number, position: number) => ({
        position_sec: position,
      })),
    } as unknown as ApiClient;
    const oldDetail = createBookDetail();
    oldDetail.created_at = "old-library";
    const newDetail = { ...createBookDetail(), created_at: "new-library" };
    let activeApiClient = oldClient;
    let activeLibrary = createLibraryMock(oldDetail);
    let current!: ReturnType<typeof useAudioPlayer>;
    function Probe() {
      current = useAudioPlayer({ apiClient: activeApiClient, library: activeLibrary });
      return null;
    }
    const container = document.createElement("div");
    const root = createRoot(container);
    await act(async () => {
      root.render(<Probe />);
    });
    const audio = createFakeAudio();
    current.audioRef.current = audio;
    await act(async () => {
      await current.togglePlay();
    });
    expect(audio.src).toContain("old-book");

    activeApiClient = newClient;
    activeLibrary = createLibraryMock(newDetail);
    await act(async () => {
      root.render(<Probe />);
    });
    await act(async () => {
      await current.togglePlay();
    });

    expect(newClient.getLibraryAudioUrl).toHaveBeenCalledWith(newDetail.id);
    expect(audio.src).toContain("new-book");
    await act(async () => root.unmount());
  });

  it("ignores an in-flight audio URL from a replaced API client", async () => {
    let resolveOldUrl!: (url: string) => void;
    const oldClient = {
      getLibraryAudioUrl: vi.fn(
        () => new Promise<string>((resolve) => { resolveOldUrl = resolve; }),
      ),
    } as unknown as ApiClient;
    const newClient = {
      getLibraryAudioUrl: vi.fn(async () => "http://desktop.test/new-book"),
      updateBookPlaybackPosition: vi.fn(async (_id: number, position: number) => ({
        position_sec: position,
      })),
    } as unknown as ApiClient;
    const oldDetail = { ...createBookDetail(), created_at: "old-library" };
    const newDetail = { ...createBookDetail(), created_at: "new-library" };
    let activeApiClient = oldClient;
    let activeLibrary = createLibraryMock(oldDetail);
    let current!: ReturnType<typeof useAudioPlayer>;
    function Probe() {
      current = useAudioPlayer({ apiClient: activeApiClient, library: activeLibrary });
      return null;
    }
    const container = document.createElement("div");
    const root = createRoot(container);
    await act(async () => {
      root.render(<Probe />);
    });
    const audio = createFakeAudio();
    current.audioRef.current = audio;
    const oldLoad = current.togglePlay();
    await vi.waitFor(() => expect(oldClient.getLibraryAudioUrl).toHaveBeenCalledTimes(1));

    activeApiClient = newClient;
    activeLibrary = createLibraryMock(newDetail);
    await act(async () => {
      root.render(<Probe />);
    });
    await act(async () => {
      await current.togglePlay();
    });
    resolveOldUrl("http://desktop.test/old-book");
    await oldLoad;

    expect(audio.src).toContain("new-book");
    expect(audio.src).not.toContain("old-book");
    await act(async () => root.unmount());
  });

  it("resolves togglePlay instead of rejecting when the audio url request fails", async () => {
    const apiClient = {
      getLibraryAudioUrl: vi.fn(async () => {
        throw new Error("backend gone");
      }),
    } as unknown as ApiClient;
    const detail = createBookDetail();
    const player = renderPlayer({ apiClient, library: createLibraryMock(detail) });
    player.current!.audioRef.current = createFakeAudio();

    // Every caller invokes playback actions as fire-and-forget (`void ...`), so
    // a rejection here would surface as an unhandled promise rejection and a
    // silently dead play button.
    await expect(player.current!.togglePlay()).resolves.toBeUndefined();
    expect(apiClient.getLibraryAudioUrl).toHaveBeenCalledWith(detail.id);
  });

  it("resolves jumpToChapter instead of rejecting when the audio url request fails", async () => {
    const apiClient = {
      getLibraryAudioUrl: vi.fn(async () => {
        throw new Error("backend gone");
      }),
    } as unknown as ApiClient;
    const detail = createBookDetail();
    const player = renderPlayer({ apiClient, library: createLibraryMock(detail) });
    player.current!.audioRef.current = createFakeAudio();

    await expect(
      player.current!.jumpToChapter({
        id: 1,
        number: 1,
        title: "chapter one",
        duration_ms: 1000,
        mp3_path: null,
      }),
    ).resolves.toBeUndefined();
  });

  it("mints a fresh audio URL after an expired media request fails", async () => {
    const apiClient = {
      getLibraryAudioUrl: vi.fn(async () => "http://127.0.0.1:43123/api/library/7/audio?token=fresh"),
    } as unknown as ApiClient;
    const detail = createBookDetail();
    const player = renderPlayer({ apiClient, library: createLibraryMock(detail) });
    const audio = createFakeAudio();
    audio.currentTime = 123;
    player.current!.audioRef.current = audio;

    player.current!.audioProps.onError();
    await vi.waitFor(() => expect(apiClient.getLibraryAudioUrl).toHaveBeenCalledWith(detail.id));

    expect(audio.src).toContain("token=fresh");
    expect(audio.currentTime).toBe(123);
  });

  it("retries recovery after an initial recovery attempt fails", async () => {
    const apiClient = {
      getLibraryAudioUrl: vi
        .fn()
        .mockRejectedValueOnce(new Error("endpoint unavailable"))
        .mockResolvedValueOnce("http://127.0.0.1:43123/api/library/7/audio?token=recovered"),
    } as unknown as ApiClient;
    const detail = createBookDetail();
    const player = renderPlayer({ apiClient, library: createLibraryMock(detail) });
    const audio = createFakeAudio();
    player.current!.audioRef.current = audio;

    player.current!.audioProps.onError();
    await vi.waitFor(() => expect(apiClient.getLibraryAudioUrl).toHaveBeenCalledTimes(1));
    await new Promise((resolve) => setTimeout(resolve, 0));

    player.current!.audioProps.onError();
    await vi.waitFor(() => expect(apiClient.getLibraryAudioUrl).toHaveBeenCalledTimes(2));
    expect(audio.src).toContain("token=recovered");
  });

  it("does not let an older recovery settle clear a newer attempt", async () => {
    let resolveFirst!: (url: string) => void;
    let resolveSecond!: (url: string) => void;
    const apiClient = {
      getLibraryAudioUrl: vi
        .fn()
        .mockImplementationOnce(() => new Promise<string>((resolve) => { resolveFirst = resolve; }))
        .mockImplementationOnce(() => new Promise<string>((resolve) => { resolveSecond = resolve; })),
    } as unknown as ApiClient;
    const detail = createBookDetail();
    const player = renderPlayer({ apiClient, library: createLibraryMock(detail) });
    const audio = createFakeAudio();
    player.current!.audioRef.current = audio;

    player.current!.audioProps.onError();
    await vi.waitFor(() => expect(apiClient.getLibraryAudioUrl).toHaveBeenCalledTimes(1));
    player.current!.audioProps.onTimeUpdate({} as never);
    player.current!.audioProps.onError();
    await vi.waitFor(() => expect(apiClient.getLibraryAudioUrl).toHaveBeenCalledTimes(2));

    resolveFirst("http://127.0.0.1:43123/api/library/7/audio?token=first");
    await Promise.resolve();
    player.current!.audioProps.onError();
    expect(apiClient.getLibraryAudioUrl).toHaveBeenCalledTimes(2);
    resolveSecond("http://127.0.0.1:43123/api/library/7/audio?token=second");
  });

  it("does not seek the current book after a stale chapter load resolves", async () => {
    let resolveA!: (url: string) => void;
    const updateBookPlaybackPosition = vi.fn(async (_id: number, position: number) => ({ position_sec: position }));
    const apiClient = {
      getLibraryAudioUrl: vi.fn((id: number) => id === 7
        ? new Promise<string>((resolve) => { resolveA = resolve; })
        : Promise.resolve("http://desktop.test/book-b")),
      updateBookPlaybackPosition,
    } as unknown as ApiClient;
    const detailA = createBookDetail();
    detailA.chapters = [{ id: 1, number: 1, title: "one", duration_ms: 10000, mp3_path: null }];
    const detailB = { ...createBookDetail(), id: 8, title: "B", playback_url: "/api/library/8/audio" };
    const library = createLibraryMock(detailA);
    let current!: ReturnType<typeof useAudioPlayer>;
    function Probe() { current = useAudioPlayer({ apiClient, library }); return null; }
    const container = document.createElement("div");
    const root = createRoot(container);
    await act(async () => { root.render(<Probe />); });
    const audio = createFakeAudio();
    current.audioRef.current = audio;
    const staleJump = current.jumpToChapter({ id: 2, number: 2, title: "two", duration_ms: 1000, mp3_path: null });
    Object.assign(library, { selectedBookId: 8, selectedBookDetail: detailB });
    await act(async () => { root.render(<Probe />); });
    await current.togglePlay();
    updateBookPlaybackPosition.mockClear();
    audio.currentTime = 42;
    resolveA("http://desktop.test/book-a");
    await staleJump;
    expect(audio.currentTime).toBe(42);
    expect(updateBookPlaybackPosition).not.toHaveBeenCalled();
    await act(async () => root.unmount());
  });
});
