// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import type { LibraryBookDetail, LibraryBookSummary } from "../../types/api";
import { useLibrary, type UseLibraryResult } from "../library/useLibrary";
import { useAudioPlayer } from "./useAudioPlayer";

const summary = (id: number): LibraryBookSummary => ({
  id, title: `Book ${id}`, author: null, source_path: `/books/${id}.epub`, conversion_mode: "audiobook", tags: "",
  favorite: false, source_available: true, total_chapters: 2, completed_chapters: 2,
});
const detail = (id: number, epoch = "epoch-1"): LibraryBookDetail => ({
  ...summary(id), source_path: `/books/${id}.epub`, source_type: "epub", playback_path: `/out/${id}.m4b`, playback_status: "packaged",
  playback_position_sec: 0, playback_url: `/api/library/${id}/audio`, library_id: "library", library_epoch: epoch,
  chapters: [
    { id: id * 10 + 1, number: 1, title: "Opening", duration_ms: 60_000, packaged_start_ms: 0, mp3_path: "1.mp3" },
    { id: id * 10 + 2, number: 2, title: "Ending", duration_ms: 60_000, packaged_start_ms: 60_000, mp3_path: "2.mp3" },
  ],
});

function fakeAudio() {
  const audio = {
    src: "", paused: true, ended: false, currentTime: 0, duration: 120, readyState: 1, volume: 1, muted: false,
    crossOrigin: null as string | null,
    pause: vi.fn(() => { audio.paused = true; }),
    play: vi.fn(async () => { audio.paused = false; }),
    load: vi.fn(),
    removeAttribute: vi.fn((name: string) => { if (name === "src") audio.src = ""; }),
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
  };
  return audio;
}

describe("playing one book while browsing others", () => {
  let root: Root;
  let ids: number[];
  let epoch: string;
  let library!: UseLibraryResult;
  let player!: ReturnType<typeof useAudioPlayer>;
  let audio: ReturnType<typeof fakeAudio>;
  let apiClient: ApiClient;

  beforeEach(async () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    ids = [1, 2, 3];
    epoch = "epoch-1";
    apiClient = {
      getLibrary: vi.fn(async () => ({ items: ids.map(summary), count: ids.length, library_id: "library", library_epoch: epoch })),
      getLibraryCount: vi.fn(async () => ({ count: ids.length })),
      getLibraryBook: vi.fn(async (id: number) => detail(id, epoch)),
      getLibraryAudioUrl: vi.fn(async (id: number) => `http://desktop.test/api/library/${id}/audio?token=t`),
      updateBookPlaybackPosition: vi.fn(async (id: number, position: number) => ({
        book_id: id, position_sec: position, library_id: "library", library_epoch: epoch,
      })),
      deleteBook: vi.fn(async (id: number) => {
        ids = ids.filter((item) => item !== id);
        return { deleted: true, files_pending: false };
      }),
    } as unknown as ApiClient;
    function Probe() {
      library = useLibrary({ apiClient });
      player = useAudioPlayer({ apiClient, library });
      return null;
    }
    audio = fakeAudio();
    root = createRoot(document.createElement("div"));
    await act(async () => root.render(<Probe />));
    player.audioRef.current = audio as unknown as HTMLAudioElement;
    // Play the first book.
    await act(async () => { await player.playSelectedBook(); });
    await act(async () => player.audioProps.onPlay());
    expect(player.playingBookId).toBe(1);
    expect(audio.paused).toBe(false);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    window.localStorage.clear();
    vi.unstubAllGlobals();
  });

  it("keeps playing while another book is opened", async () => {
    const src = audio.src;
    await act(async () => { await library.selectBook(1); });
    expect(library.selectedBookId).toBe(2);
    expect(audio.pause).not.toHaveBeenCalled();
    expect(audio.src).toBe(src);
    expect(player.playingBookId).toBe(1);
    expect(player.playing).toBe(true);
    // The player keeps showing the playing book.
    expect(player.playbackDetail?.id).toBe(1);
    expect(player.playerVisible).toBe(true);
  });

  it("switches playback when Play is pressed on a viewed book", async () => {
    await act(async () => { await library.selectBook(2); });
    await act(async () => { await player.playSelectedBook(); });
    expect(apiClient.getLibraryAudioUrl).toHaveBeenLastCalledWith(3, expect.anything());
    expect(audio.src).toContain("/api/library/3/audio");
    expect(player.playingBookId).toBe(3);
    expect(player.playbackDetail?.id).toBe(3);
    // The previous book's position was saved before it was replaced.
    expect(apiClient.updateBookPlaybackPosition).toHaveBeenCalledWith(1, expect.any(Number), expect.anything());
  });

  it("toggles the playing book from the transport while another book is viewed", async () => {
    await act(async () => { await library.selectBook(1); });
    await act(async () => { await player.togglePlay(); });
    expect(audio.pause).toHaveBeenCalledTimes(1);
    expect(player.playingBookId).toBe(1);
    expect(apiClient.getLibraryAudioUrl).toHaveBeenCalledTimes(1);
  });

  it("continues with the book after the finished one, not after the viewed one", async () => {
    await act(async () => { await library.selectBook(2); });
    Object.assign(audio, { currentTime: 120, ended: true, paused: true });
    await act(async () => player.audioProps.onEnded());
    expect(apiClient.getLibraryAudioUrl).toHaveBeenLastCalledWith(2, expect.anything());
    expect(player.playingBookId).toBe(2);
    expect(library.selectedBookId).toBe(2);
    expect(audio.play).toHaveBeenCalledTimes(2);
  });

  it("stops cleanly when the playing book is deleted", async () => {
    await act(async () => { await library.deleteSelectedBook(player.releaseBookAudio); });
    expect(audio.pause).toHaveBeenCalled();
    expect(audio.src).toBe("");
    expect(player.playingBookId).toBeNull();
    expect(library.items.map((item) => item.id)).toEqual([2, 3]);
    // The next selected book is offered, not started.
    expect(audio.play).toHaveBeenCalledTimes(1);
  });

  it("keeps playing when a viewed book that is not playing is deleted", async () => {
    await act(async () => { await library.selectBook(2); });
    const src = audio.src;
    await act(async () => { await library.deleteSelectedBook(player.releaseBookAudio); });
    expect(library.items.map((item) => item.id)).toEqual([1, 2]);
    expect(audio.pause).not.toHaveBeenCalled();
    expect(audio.src).toBe(src);
    expect(player.playingBookId).toBe(1);
  });

  it("releases the playing book when the library is replaced", async () => {
    epoch = "epoch-2";
    await act(async () => { await library.reloadLibrarySession?.(); });
    expect(audio.pause).toHaveBeenCalled();
    expect(player.playingBookId).toBeNull();
    // A position save for the old library is never sent after the switch.
    const calls = vi.mocked(apiClient.updateBookPlaybackPosition).mock.calls.length;
    await act(async () => { await player.audioProps.onTimeUpdate({} as never); });
    expect(vi.mocked(apiClient.updateBookPlaybackPosition).mock.calls.length).toBe(calls);
  });
});

describe("transport on a chaptered book", () => {
  it("steps through the playing book's chapters, not the library", async () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    const apiClient = {
      getLibrary: vi.fn(async () => ({ items: [1, 2].map(summary), count: 2, library_id: "library", library_epoch: "epoch-1" })),
      getLibraryCount: vi.fn(async () => ({ count: 2 })),
      getLibraryBook: vi.fn(async (id: number) => detail(id)),
      getLibraryAudioUrl: vi.fn(async (id: number) => `http://desktop.test/api/library/${id}/audio?token=t`),
      updateBookPlaybackPosition: vi.fn(async (id: number, position: number) => ({ book_id: id, position_sec: position })),
    } as unknown as ApiClient;
    let library!: UseLibraryResult;
    let player!: ReturnType<typeof useAudioPlayer>;
    function Probe() {
      library = useLibrary({ apiClient });
      player = useAudioPlayer({ apiClient, library });
      return null;
    }
    const audio = fakeAudio();
    const root = createRoot(document.createElement("div"));
    try {
      await act(async () => root.render(<Probe />));
      player.audioRef.current = audio as unknown as HTMLAudioElement;
      await act(async () => { await player.playSelectedBook(); });
      expect(player.chapterTransport).toBe(true);
      expect(player.canGoPrevious).toBe(false);
      expect(player.canGoNext).toBe(true);
      await act(async () => { await player.playRelative(1); });
      expect(audio.currentTime).toBe(60);
      expect(player.playingBookId).toBe(1);
      expect(library.selectedBookId).toBe(1);
    } finally {
      await act(async () => root.unmount());
      vi.unstubAllGlobals();
    }
  });
});

describe("transport on a book without chapters", () => {
  it("steps through the library from the playing book, not the viewed one", async () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    const singleChapter = (id: number): LibraryBookDetail => ({ ...detail(id), chapters: detail(id).chapters.slice(0, 1) });
    const apiClient = {
      getLibrary: vi.fn(async () => ({ items: [1, 2, 3].map(summary), count: 3, library_id: "library", library_epoch: "epoch-1" })),
      getLibraryCount: vi.fn(async () => ({ count: 3 })),
      getLibraryBook: vi.fn(async (id: number) => singleChapter(id)),
      getLibraryAudioUrl: vi.fn(async (id: number) => `http://desktop.test/api/library/${id}/audio?token=t`),
      updateBookPlaybackPosition: vi.fn(async (id: number, position: number) => ({ book_id: id, position_sec: position })),
    } as unknown as ApiClient;
    let library!: UseLibraryResult;
    let player!: ReturnType<typeof useAudioPlayer>;
    function Probe() {
      library = useLibrary({ apiClient });
      player = useAudioPlayer({ apiClient, library });
      return null;
    }
    const audio = fakeAudio();
    const root = createRoot(document.createElement("div"));
    try {
      await act(async () => root.render(<Probe />));
      player.audioRef.current = audio as unknown as HTMLAudioElement;
      await act(async () => { await player.playSelectedBook(); });
      await act(async () => { await library.selectBook(2); });
      expect(player.chapterTransport).toBe(false);
      expect(player.canGoPrevious).toBe(false);
      expect(player.canGoNext).toBe(true);
      await act(async () => { await player.playRelative(1); });
      expect(player.playingBookId).toBe(2);
      expect(library.selectedBookId).toBe(2);
    } finally {
      await act(async () => root.unmount());
      vi.unstubAllGlobals();
    }
  });
});
