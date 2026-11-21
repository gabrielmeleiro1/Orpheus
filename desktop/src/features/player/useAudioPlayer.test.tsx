// @vitest-environment happy-dom

import { act, memo, StrictMode, useCallback } from "react";
import { createRoot } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient, type ApiClient } from "../../api/client";
import { equalWhenInvisible } from "../../app/visibilityMemo";
import type { UseLibraryResult } from "../library/useLibrary";
import { useLibrary } from "../library/useLibrary";
import type { LibraryBookDetail } from "../../types/api";
import {
  playbackCheckpointBookIdentity,
  readPlaybackCheckpoint,
  writePlaybackCheckpoint,
} from "./playbackCheckpoint";
import { useAudioPlayer } from "./useAudioPlayer";
import { PlayerPanel } from "./PlayerPanel";
import { NowPlayingView } from "./NowPlayingView";

const StablePlayerPanel = memo(PlayerPanel, equalWhenInvisible);

function createBookDetail(): LibraryBookDetail {
  return {
    id: 7,
    library_id: "library",
    library_epoch: "epoch",
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
    selectRelative: vi.fn(async () => null),
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

beforeEach(() => { vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true); });
afterEach(() => {
  window.localStorage.clear();
  vi.restoreAllMocks();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("useAudioPlayer audio load failures", () => {
  it.each(["metadata", "timeout"] as const)("starts a cold packaged chapter from Now Playing after %s readiness", async (readiness) => {
    vi.useFakeTimers();
    const detail = { ...createBookDetail(), chapters: [
      { id: 1, number: 1, title: "First chapter", duration_ms: 16000, packaged_start_ms: 0, mp3_path: "1.mp3" },
      { id: 2, number: 2, title: "Second chapter", duration_ms: 15000, packaged_start_ms: 16000, mp3_path: "2.mp3" },
    ] };
    let authorize!: (url: string) => void;
    const apiClient = {
      getLibrary: vi.fn(async () => ({ items: [detail], count: 1, library_id: detail.library_id, library_epoch: detail.library_epoch })),
      getLibraryBook: vi.fn(async () => detail),
      getLibraryAudioUrl: vi.fn(() => new Promise<string>((resolve) => { authorize = resolve; })),
      updateBookPlaybackPosition: vi.fn(async (_id: number, position: number) => ({ position_sec: position })),
    } as unknown as ApiClient;
    let current!: ReturnType<typeof useAudioPlayer>;
    function Probe() {
      const library = useLibrary({ apiClient });
      current = useAudioPlayer({ apiClient, library });
      return <>
        <NowPlayingView open={current.playerVisible} bookTitle={detail.title ?? "Book"}
          chapters={library.selectedBookDetail?.chapters ?? []} currentChapterNumber={current.currentChapter?.number ?? null}
          playing={current.playing} elapsedSec={current.elapsedSec} durationSec={current.durationSec} canSeek={current.canSeek}
          playbackRate={1} sleepTimer={current.sleepTimer} sleepRemainingSec={current.sleepRemainingSec}
          onJumpToChapter={(chapter) => void current.jumpToChapter(chapter)} onSeekTo={current.seekTo}
          onPlaybackRateChange={() => {}} onSleepTimerChange={current.setSleepTimer} onClose={() => {}} />
        <StablePlayerPanel enabled={current.playerEligible} visible={current.playerVisible} playing={current.playing}
          title={current.title} elapsedSec={current.elapsedSec} durationSec={current.durationSec} canSeek={current.canSeek}
          volume={current.volume} audioRef={current.audioRef} audio={current.audioProps} onTogglePlay={current.togglePlay}
          onSeekBy={current.seekBy} onSeekTo={current.seekTo} onPrevious={() => {}} onNext={() => {}}
          onVolumeChange={current.setPlayerVolume} onClose={current.closePlayer} />
      </>;
    }
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    try {
      await act(async () => root.render(<StrictMode><Probe /></StrictMode>));
      await act(async () => vi.advanceTimersByTimeAsync(220));
      const audio = current.audioRef.current!;
      let readyState = 0;
      let paused = true;
      Object.defineProperties(audio, {
        readyState: { configurable: true, get: () => readyState },
        duration: { configurable: true, get: () => readyState ? 31 : Number.NaN },
        paused: { configurable: true, get: () => paused },
      });
      const play = vi.spyOn(audio, "play").mockImplementation(async () => { paused = false; audio.dispatchEvent(new Event("play")); });
      vi.spyOn(audio, "pause").mockImplementation(() => { paused = true; });
      vi.spyOn(audio, "load").mockImplementation(() => { audio.currentTime = 0; audio.dispatchEvent(new Event("emptied")); });
      expect(current.canSeek).toBe(false);
      const second = container.querySelectorAll<HTMLButtonElement>(".np-chapter")[1];
      expect(second.disabled).toBe(false);
      await act(async () => second.click());
      expect(apiClient.getLibraryAudioUrl).toHaveBeenCalledOnce();
      await act(async () => authorize("http://desktop.test/cold-book"));
      expect(audio.src).toContain("cold-book");
      expect(play).not.toHaveBeenCalled();
      await act(async () => {
        if (readiness === "metadata") {
          readyState = 1;
          audio.dispatchEvent(new Event("loadedmetadata"));
        } else {
          await vi.advanceTimersByTimeAsync(2000);
        }
      });
      expect(play).toHaveBeenCalledOnce();
      expect(audio.currentTime).toBe(16);
      expect(current.playing).toBe(true);
      expect(current.currentChapter?.number).toBe(2);
      expect(current.canSeek).toBe(true);
      expect(container.querySelector(".np-view-chapter")?.textContent).toContain("Second chapter");
    } finally {
      await act(async () => root.unmount());
      container.remove();
    }
  });

  it.each([["next", false], ["completion", false], ["next", true], ["completion", true]] as const)("loads and plays book 101 via %s with an existing page request %s", async (action, prefetch) => {
    let resolveNextPage!: () => void;
    const books = Array.from({ length: 101 }, (_, index) => ({ ...createBookDetail(), id: index + 1, title: `Book ${index + 1}` }));
    const apiClient = {
      getLibrary: vi.fn(async (_query: unknown, _limit: number, options: { offset: number }) => {
        if (prefetch && options.offset === 100) await new Promise<void>((resolve) => { resolveNextPage = resolve; });
        return { items: books.slice(options.offset, options.offset + 100), count: books.length };
      }),
      getLibraryCount: vi.fn(async () => ({ count: books.length })),
      getLibraryBook: vi.fn(async (id: number) => books[id - 1]),
      getLibraryAudioUrl: vi.fn(async (id: number) => `http://desktop.test/book/${id}`),
      updateBookPlaybackPosition: vi.fn(async (_id: number, position: number) => ({ position_sec: position })),
    } as unknown as ApiClient;
    let library!: UseLibraryResult;
    let current!: ReturnType<typeof useAudioPlayer>;
    function Probe() { library = useLibrary({ apiClient }); current = useAudioPlayer({ apiClient, library }); return null; }
    const root = createRoot(document.createElement("div"));
    await act(async () => root.render(<Probe />));
    const audio = createFakeAudio();
    Object.assign(audio, { duration: 20 });
    current.audioRef.current = audio;
    try {
      await act(async () => library.selectBook(99));
      await act(async () => current.togglePlay());
      expect(current.canGoNext).toBe(true);
      if (prefetch) await act(async () => { void library.loadMore(); });
      let navigation: Promise<void> | undefined;
      await act(async () => {
        if (action === "next") navigation = current.playRelative(1);
        else current.audioProps.onEnded();
        if (!prefetch) await navigation;
      });
      if (prefetch) await act(async () => { resolveNextPage(); await navigation; });
      expect(library.selectedBookId).toBe(101);
      expect(library.items).toHaveLength(101);
      expect(apiClient.getLibrary).toHaveBeenCalledTimes(2);
      expect(apiClient.getLibrary).toHaveBeenLastCalledWith(undefined, 100, { conversionMode: "", offset: 100 }, expect.any(Object));
      expect(audio.src).toContain("/book/101");
      expect(audio.play).toHaveBeenCalledTimes(2);
    } finally { await act(async () => root.unmount()); }
  });

  it.each([0, 1])("stops chapter-file playback before automatic advancement at chapter index %i", async (index) => {
    const detail = { ...createBookDetail(), playback_status: "chapter_audio" as const, chapters: [
      { id: 1, number: 1, title: "One", duration_ms: 10000, mp3_path: "1.mp3" },
      { id: 2, number: 2, title: "Two", duration_ms: 10000, mp3_path: "2.mp3" },
    ] };
    const apiClient = { getLibraryAudioUrl: vi.fn(async () => "http://desktop.test/book"),
      updateBookPlaybackPosition: vi.fn(async (_id: number, position: number) => ({ position_sec: position })) } as unknown as ApiClient;
    const library = createLibraryMock(detail);
    let current!: ReturnType<typeof useAudioPlayer>;
    function Probe() { current = useAudioPlayer({ apiClient, library }); return null; }
    const root = createRoot(document.createElement("div"));
    await act(async () => root.render(<Probe />));
    const audio = createFakeAudio();
    Object.assign(audio, { duration: 10 });
    current.audioRef.current = audio;
    try {
      await act(async () => current.jumpToChapter(detail.chapters[index]));
      await act(async () => current.setSleepTimer("chapter"));
      Object.assign(audio, { currentTime: 10, ended: true, paused: true });
      await act(async () => current.audioProps.onEnded());
      expect(current.sleepTimer.kind).toBe("off");
      expect(library.selectRelative).not.toHaveBeenCalled();
      expect(apiClient.getLibraryAudioUrl).toHaveBeenCalledOnce();
      expect(audio.play).toHaveBeenCalledOnce();
      await act(async () => current.togglePlay());
      expect(audio.play).toHaveBeenCalledTimes(2);
    } finally { await act(async () => root.unmount()); }
  });

  it("follows manual packaged chapter jumps and stops at the next natural chapter boundary", async () => {
    const detail = { ...createBookDetail(), chapters: [1, 2, 3].map((number) => ({ id: number, number, title: `${number}`, duration_ms: 10000, mp3_path: `${number}.mp3` })) };
    const apiClient = { getLibraryAudioUrl: vi.fn(async () => "http://desktop.test/book"),
      updateBookPlaybackPosition: vi.fn(async (_id: number, position: number) => ({ position_sec: position })) } as unknown as ApiClient;
    const library = createLibraryMock(detail);
    let current!: ReturnType<typeof useAudioPlayer>;
    function Probe() { current = useAudioPlayer({ apiClient, library }); return null; }
    const root = createRoot(document.createElement("div"));
    await act(async () => root.render(<Probe />));
    const audio = createFakeAudio();
    Object.assign(audio, { duration: 30 });
    current.audioRef.current = audio;
    try {
      await act(async () => current.togglePlay());
      await act(async () => current.setSleepTimer("chapter"));
      await act(async () => current.jumpToChapter(detail.chapters[1]));
      expect(current.sleepTimer).toEqual({ kind: "chapter", chapterNumber: 2 });
      Object.assign(audio, { currentTime: 19, paused: false });
      vi.mocked(audio.pause).mockClear();
      await act(async () => current.audioProps.onTimeUpdate({} as never));
      expect(audio.pause).not.toHaveBeenCalled();
      audio.currentTime = 20;
      await act(async () => current.audioProps.onTimeUpdate({} as never));
      expect(audio.pause).toHaveBeenCalledOnce();
      expect(current.sleepTimer.kind).toBe("off");
    } finally { await act(async () => root.unmount()); }
  });
  it("reports stalled audio authorization and allows play to retry", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementationOnce((_input, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    }))
      .mockImplementation(async () => new Response(JSON.stringify({ token: "scoped", expires_at: 1 })));
    const apiClient = createApiClient({ baseUrl: "http://desktop.test", token: "secret" });
    const library = createLibraryMock(createBookDetail());
    let current!: ReturnType<typeof useAudioPlayer>;
    function Probe() { current = useAudioPlayer({ apiClient, library }); return null; }
    const root = createRoot(document.createElement("div"));
    await act(async () => root.render(<Probe />));
    const audio = createFakeAudio();
    current.audioRef.current = audio;
    try {
      await act(async () => { void current.togglePlay(); });
      await act(async () => vi.advanceTimersByTimeAsync(8000));
      expect(current.title).toBe("Playback failed");
      expect(audio.play).not.toHaveBeenCalled();
      await act(async () => current.togglePlay());
      expect(current.title).not.toBe("Playback failed");
      expect(audio.play).toHaveBeenCalledOnce();
      expect(fetchMock).toHaveBeenCalledTimes(2);
    } finally {
      await act(async () => root.unmount());
    }
  });

  it("does not load or autoplay an authorization response after unmount", async () => {
    let resolveUrl!: (url: string) => void;
    const apiClient = {
      getLibraryAudioUrl: vi.fn(() => new Promise<string>((resolve) => { resolveUrl = resolve; })),
    } as unknown as ApiClient;
    const library = createLibraryMock(createBookDetail());
    let current!: ReturnType<typeof useAudioPlayer>;
    function Probe() { current = useAudioPlayer({ apiClient, library }); return null; }
    const root = createRoot(document.createElement("div"));
    await act(async () => root.render(<Probe />));
    const audio = createFakeAudio();
    current.audioRef.current = audio;
    let loading!: Promise<void>;
    await act(async () => { loading = current.togglePlay(); });
    await act(async () => root.unmount());
    resolveUrl("http://desktop.test/book");
    await loading;

    expect(audio.src).toBe("");
    expect(audio.load).not.toHaveBeenCalled();
    expect(audio.play).not.toHaveBeenCalled();
  });

  it("plays an epoch-bearing detail through the desktop audio resource", async () => {
    const detail = { ...createBookDetail(), library_id: "library", library_epoch: "selected" };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      if (String(input).endsWith("/api/library/7")) return new Response(JSON.stringify(detail));
      if (String(input).endsWith("/api/desktop/query-token")) {
        expect(JSON.parse(String(init?.body))).toEqual({ scope: "audio", book_id: 7, expected_epoch: "selected" });
        return new Response(JSON.stringify({ token: "scoped", expires_at: 1 }));
      }
      throw new Error(`Unexpected request: ${input}`);
    });
    const apiClient = createApiClient({ baseUrl: "http://desktop.test", token: "secret" });
    const library = createLibraryMock(await apiClient.getLibraryBook(7));
    let current!: ReturnType<typeof useAudioPlayer>;
    function Probe() { current = useAudioPlayer({ apiClient, library }); return null; }
    const root = createRoot(document.createElement("div"));
    await act(async () => root.render(<Probe />));
    const audio = createFakeAudio();
    current.audioRef.current = audio;
    try {
      await act(async () => current.togglePlay());
      expect(audio.play).toHaveBeenCalledOnce();
      expect(new URL(audio.src).searchParams.get("token")).toBe("scoped");
      expect(fetchMock).toHaveBeenCalledTimes(2);
    } finally {
      await act(async () => root.unmount());
    }
  });

  it.each([
    ["ready", false],
    ["ready", true],
    ["chapter_audio", false],
    ["chapter_audio", true],
  ] as const)("resumes five seconds into chapter three after repair to %s with remount %s", async (status, remount) => {
    const apiClient = {
      getLibraryAudioUrl: vi.fn(async () => "http://desktop.test/book"),
      updateBookPlaybackPosition: vi.fn(async (_id: number, position: number) => ({ position_sec: position })),
    } as unknown as ApiClient;
    const detail = createBookDetail();
    detail.playback_status = "chapter_audio";
    detail.total_chapters = 3;
    detail.chapters = [
      { id: 1, number: 1, title: "one", duration_ms: 10000, mp3_path: "1.mp3" },
      { id: 2, number: 2, title: "two", duration_ms: null, mp3_path: null },
      { id: 3, number: 3, title: "three", duration_ms: 10000, mp3_path: "3.mp3" },
    ];
    const library = createLibraryMock(detail);
    let current!: ReturnType<typeof useAudioPlayer>;
    function Probe() { current = useAudioPlayer({ apiClient, library }); return null; }
    let root = createRoot(document.createElement("div"));
    await act(async () => root.render(<Probe />));
    let audio = createFakeAudio();
    Object.assign(audio, { duration: 10 });
    current.audioRef.current = audio;
    try {
      await act(async () => current.jumpToChapter(detail.chapters[2]));
      audio.currentTime = 5;
      await act(async () => current.audioProps.onPause());
      expect(apiClient.updateBookPlaybackPosition).toHaveBeenLastCalledWith(7, 15, { expectedEpoch: "epoch", timeoutMs: 15_000 });
      expect(readPlaybackCheckpoint(7, playbackCheckpointBookIdentity(detail))).toBe(15);
      if (remount) await act(async () => root.unmount());
      library.selectedBookDetail = {
        ...detail,
        playback_status: status,
        playback_position_sec: 15,
        chapters: detail.chapters.map((chapter) => chapter.number === 2
          ? { ...chapter, duration_ms: 20000, mp3_path: "2.mp3" }
          : chapter),
      };
      if (remount) {
        root = createRoot(document.createElement("div"));
        await act(async () => root.render(<Probe />));
        audio = createFakeAudio();
        current.audioRef.current = audio;
      }
      Object.assign(audio, { duration: status === "ready" ? 40 : 10 });
      await act(async () => root.render(<Probe />));
      if (remount) await act(async () => current.togglePlay());
      expect(audio.currentTime).toBe(status === "ready" ? 35 : 5);
      expect(current.currentChapter?.number).toBe(3);
      expect(new URL(audio.src).searchParams.get("chapter_number")).toBe(status === "ready" ? null : "3");
      await act(async () => current.audioProps.onPause());
      expect(apiClient.updateBookPlaybackPosition).toHaveBeenLastCalledWith(7, 35, { expectedEpoch: "epoch", timeoutMs: 15_000 });
    } finally {
      await act(async () => root.unmount());
    }
  });

  it("keeps absolute progress when the last available chapter ends", async () => {
    const apiClient = {
      getLibraryAudioUrl: vi.fn(async () => "http://desktop.test/book"),
      updateBookPlaybackPosition: vi.fn(async (_id: number, position: number) => ({ position_sec: position })),
    } as unknown as ApiClient;
    const detail = createBookDetail();
    detail.playback_status = "chapter_audio";
    detail.chapters = [
      { id: 1, number: 1, title: "one", duration_ms: 10000, mp3_path: "1.mp3" },
      { id: 2, number: 2, title: "two", duration_ms: 10000, mp3_path: "2.mp3" },
      { id: 3, number: 3, title: "three", duration_ms: null, mp3_path: null },
    ];
    const library = createLibraryMock(detail);
    library.items.push({ ...library.items[0], id: 8, title: "Other book" });
    let current!: ReturnType<typeof useAudioPlayer>;
    function Probe() { current = useAudioPlayer({ apiClient, library }); return null; }
    const root = createRoot(document.createElement("div"));
    await act(async () => root.render(<Probe />));
    const audio = createFakeAudio();
    Object.assign(audio, { duration: 10 });
    current.audioRef.current = audio;
    try {
      await act(async () => current.jumpToChapter(detail.chapters[1]));
      audio.currentTime = 10;
      await act(async () => current.audioProps.onEnded());
      expect(library.selectBook).not.toHaveBeenCalled();
      expect(apiClient.updateBookPlaybackPosition).toHaveBeenLastCalledWith(7, 20, { expectedEpoch: "epoch", timeoutMs: 15_000 });
      expect(readPlaybackCheckpoint(7, playbackCheckpointBookIdentity(detail))).toBe(20);
      expect(library.setSelectedPlaybackPosition).toHaveBeenLastCalledWith(20);
      expect(current.playing).toBe(false);
    } finally {
      await act(async () => root.unmount());
    }
  });

  it("reloads chapter audio as a packaged book while keeping absolute progress", async () => {
    const apiClient = {
      getLibraryAudioUrl: vi.fn(async () => "http://desktop.test/book"),
      updateBookPlaybackPosition: vi.fn(async (_id: number, position: number) => ({ position_sec: position })),
    } as unknown as ApiClient;
    const detail = createBookDetail();
    detail.playback_status = "chapter_audio";
    detail.chapters = [
      { id: 1, number: 1, title: "one", duration_ms: 10000, mp3_path: "1.mp3" },
      { id: 2, number: 2, title: "two", duration_ms: 10000, mp3_path: "2.mp3" },
    ];
    const library = createLibraryMock(detail);
    let current!: ReturnType<typeof useAudioPlayer>;
    function Probe() { current = useAudioPlayer({ apiClient, library }); return null; }
    const root = createRoot(document.createElement("div"));
    await act(async () => root.render(<Probe />));
    const audio = createFakeAudio();
    Object.assign(audio, { duration: 20 });
    current.audioRef.current = audio;
    try {
      await act(async () => current.jumpToChapter(detail.chapters[1]));
      audio.currentTime = 4;
      Object.assign(audio, { paused: false });
      await act(async () => current.audioProps.onPlay());
      library.selectedBookDetail = { ...detail, playback_status: "ready", playback_path: "/library/7.m4b" };
      await act(async () => root.render(<Probe />));
      expect(apiClient.getLibraryAudioUrl).toHaveBeenCalledTimes(2);
      expect(new URL(audio.src).searchParams.has("chapter_number")).toBe(false);
      expect(audio.currentTime).toBe(14);
      expect(readPlaybackCheckpoint(7, playbackCheckpointBookIdentity(detail))).toBe(14);
      expect(audio.play).toHaveBeenCalledTimes(2);
      expect(library.selectBook).not.toHaveBeenCalled();
    } finally {
      await act(async () => root.unmount());
    }
  });

  it.each(["ready", "chapter_audio"] as const)("closing a completed %s book keeps the reset position", async (status) => {
    const apiClient = {
      getLibraryAudioUrl: vi.fn(async () => "http://desktop.test/book"),
      updateBookPlaybackPosition: vi.fn(async (_id: number, position: number) => ({ position_sec: position })),
    } as unknown as ApiClient;
    const detail = createBookDetail();
    detail.playback_status = status;
    detail.chapters = [
      { id: 1, number: 1, title: "one", duration_ms: 10000, mp3_path: "1.mp3" },
      { id: 2, number: 2, title: "two", duration_ms: 10000, mp3_path: "2.mp3" },
    ];
    const library = createLibraryMock(detail);
    let current!: ReturnType<typeof useAudioPlayer>;
    function Probe() { current = useAudioPlayer({ apiClient, library }); return null; }
    const root = createRoot(document.createElement("div"));
    await act(async () => root.render(<Probe />));
    const audio = createFakeAudio();
    Object.assign(audio, { duration: 20 });
    current.audioRef.current = audio;
    try {
      await act(async () => current.jumpToChapter(detail.chapters[1]));
      audio.currentTime = 20;
      await act(async () => current.audioProps.onEnded());
      expect(readPlaybackCheckpoint(7, playbackCheckpointBookIdentity(detail))).toBe(0);
      await act(async () => current.closePlayer());
      expect(apiClient.updateBookPlaybackPosition).toHaveBeenLastCalledWith(7, 0, { expectedEpoch: "epoch", timeoutMs: 15_000 });
      expect(readPlaybackCheckpoint(7, playbackCheckpointBookIdentity(detail))).toBe(0);
    } finally {
      await act(async () => root.unmount());
    }
  });

  it("closing the player invalidates a pending chapter autoplay", async () => {
    let resolveUrl!: (value: string) => void;
    const getLibraryAudioUrl = vi.fn().mockResolvedValueOnce("http://desktop.test/book")
      .mockImplementationOnce(() => new Promise<string>((resolve) => { resolveUrl = resolve; }));
    const apiClient = { getLibraryAudioUrl,
      updateBookPlaybackPosition: vi.fn(async (_id: number, position: number) => ({ position_sec: position })),
    } as unknown as ApiClient;
    const detail = createBookDetail();
    detail.playback_status = "chapter_audio";
    detail.chapters = [
      { id: 1, number: 1, title: "one", duration_ms: 10000, mp3_path: "1.mp3" },
      { id: 2, number: 2, title: "two", duration_ms: 10000, mp3_path: "2.mp3" },
    ];
    const library = createLibraryMock(detail);
    let current!: ReturnType<typeof useAudioPlayer>;
    function Probe() { current = useAudioPlayer({ apiClient, library }); return null; }
    const root = createRoot(document.createElement("div"));
    await act(async () => root.render(<Probe />));
    const audio = createFakeAudio();
    current.audioRef.current = audio;
    try {
      await act(async () => current.togglePlay());
      const originalSrc = audio.src;
      Object.assign(audio, { paused: false });
      vi.mocked(audio.play).mockClear();
      let loading: Promise<void>;
      await act(async () => { loading = current.jumpToChapter(detail.chapters[1]); });
      await act(async () => current.closePlayer());
      expect(current.playerVisible).toBe(false);
      await act(async () => { resolveUrl("http://desktop.test/book"); await loading; });
      expect(audio.src).toBe(originalSrc);
      expect(audio.play).not.toHaveBeenCalled();
      expect(current.playerVisible).toBe(false);
    } finally {
      await act(async () => root.unmount());
    }
  });

  it.each([0, 20000])("resumes chapter files without collapsing a missing chapter's %i ms duration", async (missingDurationMs) => {
    const apiClient = {
      getLibraryAudioUrl: vi.fn(async () => "http://desktop.test/book?token=secret"),
      updateBookPlaybackPosition: vi.fn(async (_id: number, position: number) => ({ position_sec: position })),
    } as unknown as ApiClient;
    const detail = createBookDetail();
    detail.playback_status = "chapter_audio";
    detail.chapters = [
      { id: 1, number: 1, title: "one", duration_ms: 10000, mp3_path: "1.mp3" },
      { id: 2, number: 2, title: "missing", duration_ms: missingDurationMs, mp3_path: null },
      { id: 3, number: 3, title: "three", duration_ms: 20000, mp3_path: "3.mp3" },
      { id: 4, number: 4, title: "four", duration_ms: 20000, mp3_path: "4.mp3" },
    ];
    detail.playback_position_sec = 13 + missingDurationMs / 1000;
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
      expect(readPlaybackCheckpoint(detail.id, playbackCheckpointBookIdentity(detail))).toBe(17 + missingDurationMs / 1000);
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

    // The player shows where the book will resume, but the element is untouched until Play.
    expect(current.elapsedSec).toBe(183.75);
    expect(audio.currentTime).toBe(0);
    expect(audio.load).not.toHaveBeenCalled();
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

  it("keeps the save failure notice on screen while playback ticks on", async () => {
    const apiClient = {
      getLibraryAudioUrl: vi.fn(async () => "http://desktop.test/book"),
      updateBookPlaybackPosition: vi.fn(async () => { throw new Error("disk full"); }),
    } as unknown as ApiClient;
    const library = createLibraryMock(createBookDetail());
    let current!: ReturnType<typeof useAudioPlayer>;
    function Probe() {
      current = useAudioPlayer({ apiClient, library });
      return null;
    }
    const root = createRoot(document.createElement("div"));
    await act(async () => root.render(<Probe />));
    const audio = createFakeAudio();
    current.audioRef.current = audio;
    await act(async () => current.togglePlay());
    try {
      for (const position of [20, 40]) {
        Object.assign(audio, { paused: false, currentTime: position });
        await act(async () => current.audioProps.onTimeUpdate({} as never));
      }
      expect(current.title).toBe("Could not save playback position");

      Object.assign(audio, { currentTime: 41 });
      await act(async () => current.audioProps.onTimeUpdate({} as never));
      expect(current.title).toBe("Could not save playback position");

      vi.mocked(apiClient.updateBookPlaybackPosition).mockResolvedValue({ position_sec: 60 } as never);
      Object.assign(audio, { currentTime: 60 });
      await act(async () => current.audioProps.onTimeUpdate({} as never));
      expect(current.title).toBe("Playing · 01:00");
    } finally {
      await act(async () => root.unmount());
    }
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

    expect(newClient.getLibraryAudioUrl).toHaveBeenCalledWith(newDetail.id, { expectedEpoch: newDetail.library_epoch, timeoutMs: 8_000 });
    expect(audio.src).toContain("new-book");
    await act(async () => root.unmount());
  });

  it.each([false, true])("resumes playback after backend replacement and natural completion through the real player panel (disconnect first: %s)", async (disconnectFirst) => {
    const detail = { ...createBookDetail(), chapters: [
      { id: 1, number: 1, title: "One", duration_ms: 60000, mp3_path: "1.mp3" },
    ] };
    const client = (endpoint: string) => ({
      getLibrary: vi.fn(async () => ({ items: [detail], count: 1, library_id: detail.library_id, library_epoch: detail.library_epoch })),
      getLibraryBook: vi.fn(async () => detail),
      getLibraryAudioUrl: vi.fn(async () => endpoint),
      updateBookPlaybackPosition: vi.fn(async (_id: number, position: number) => ({ position_sec: position })),
    }) as unknown as ApiClient;
    const oldClient = client("http://desktop.test/old-audio");
    const newClient = client("http://desktop.test/new-audio");
    let activeClient: ApiClient | null = oldClient;
    let disconnected = false;
    let library!: UseLibraryResult;
    let current!: ReturnType<typeof useAudioPlayer>;
    function Probe() {
      library = useLibrary({ apiClient: activeClient, backendDisconnected: disconnected });
      current = useAudioPlayer({ apiClient: activeClient, library });
      const togglePlay = current.togglePlay;
      const toggle = useCallback(() => { void togglePlay(); }, [togglePlay]);
      return <StablePlayerPanel enabled={current.playerEligible} visible={current.playerVisible}
        playing={current.playing} title={current.title} elapsedSec={current.elapsedSec} durationSec={current.durationSec}
        canSeek={current.canSeek} volume={current.volume} audioRef={current.audioRef}
        audio={{ ...current.audioProps, src: current.audioProps.src ?? undefined }}
        onTogglePlay={toggle} onSeekBy={current.seekBy} onSeekTo={current.seekTo}
        onPrevious={() => void current.playRelative(-1)} onNext={() => void current.playRelative(1)}
        onVolumeChange={current.setPlayerVolume} onClose={current.closePlayer} />;
    }
    const container = document.createElement("div");
    const root = createRoot(container);
    await act(async () => root.render(<Probe />));
    const audio = current.audioRef.current!;
    Object.defineProperties(audio, { readyState: { configurable: true, get: () => 1 }, duration: { configurable: true, get: () => 60 } });
    const play = vi.spyOn(audio, "play").mockResolvedValue();
    vi.spyOn(audio, "pause").mockImplementation(() => undefined);
    vi.spyOn(audio, "load").mockImplementation(() => undefined);
    try {
      await act(async () => container.querySelector<HTMLButtonElement>("#play-btn")!.click());
      expect(audio.src).toContain("old-audio");
      if (disconnectFirst) {
        activeClient = null;
        disconnected = true;
        await act(async () => root.render(<Probe />));
      }
      activeClient = newClient;
      disconnected = false;
      await act(async () => root.render(<Probe />));
      expect(library.selectedBookId).toBe(detail.id);
      expect(library.selectedBookPlayable).toBe(true);
      await act(async () => container.querySelector<HTMLButtonElement>("#play-btn")!.click());
      expect(newClient.getLibraryAudioUrl).toHaveBeenCalledOnce();
      expect(audio.src).toContain("new-audio");
      expect(current.canSeek).toBe(true);
      expect(play).toHaveBeenCalledTimes(2);
      audio.currentTime = 60;
      await act(async () => audio.dispatchEvent(new Event("ended")));
      expect(current.canSeek).toBe(false);
      expect(container.querySelector<HTMLButtonElement>("#play-btn")!.disabled).toBe(false);
      await act(async () => container.querySelector<HTMLButtonElement>("#play-btn")!.click());
      expect(newClient.getLibraryAudioUrl).toHaveBeenCalledTimes(2);
      expect(current.canSeek).toBe(true);
      expect(play).toHaveBeenCalledTimes(3);
    } finally { await act(async () => root.unmount()); }
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
    expect(apiClient.getLibraryAudioUrl).toHaveBeenCalledWith(detail.id, { expectedEpoch: detail.library_epoch, timeoutMs: 8_000 });
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
    await vi.waitFor(() => expect(apiClient.getLibraryAudioUrl).toHaveBeenCalledWith(detail.id, { expectedEpoch: detail.library_epoch, timeoutMs: 8_000 }));

    expect(audio.src).toContain("token=fresh");
    expect(audio.currentTime).toBe(123);
  });

  it("keeps recovery paused when a media error precedes the queued native pause event", async () => {
    const apiClient = {
      getLibraryAudioUrl: vi.fn(async () => "http://desktop.test/book"),
      updateBookPlaybackPosition: vi.fn(async (_id: number, position: number) => ({ position_sec: position })),
    } as unknown as ApiClient;
    const library = createLibraryMock(createBookDetail());
    let current!: ReturnType<typeof useAudioPlayer>;
    function Probe() { current = useAudioPlayer({ apiClient, library }); return null; }
    const root = createRoot(document.createElement("div"));
    await act(async () => root.render(<Probe />));
    const audio = createFakeAudio();
    Object.assign(audio, { duration: 36_000 });
    current.audioRef.current = audio;
    try {
      await act(async () => current.togglePlay());
      Object.assign(audio, { currentTime: 18_123.25, paused: false });
      await act(async () => current.audioProps.onPlay());
      expect(current.playing).toBe(true);
      vi.mocked(audio.pause).mockImplementation(() => { Object.assign(audio, { paused: true }); });
      // pause() changes the element immediately, but its pause event is queued.
      await act(async () => {
        await current.togglePlay();
        current.audioProps.onError();
      });
      expect(apiClient.getLibraryAudioUrl).toHaveBeenCalledTimes(2);
      expect(audio.play).toHaveBeenCalledOnce();
      expect(audio.currentTime).toBe(18_123.25);
      expect(readPlaybackCheckpoint(7, playbackCheckpointBookIdentity(library.selectedBookDetail!))).toBe(18_123.25);
    } finally { await act(async () => root.unmount()); }
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
