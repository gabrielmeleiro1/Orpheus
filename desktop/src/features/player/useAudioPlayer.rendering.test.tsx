// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import type { UseLibraryResult } from "../library/useLibrary";
import type { LibraryBookDetail } from "../../types/api";
import { useAudioPlayer } from "./useAudioPlayer";

function bookDetail(positionSec = 0): LibraryBookDetail {
  return {
    id: 7, library_id: "library", library_epoch: "epoch", title: "Book", author: null, source_path: null,
    source_type: "epub", conversion_mode: "audiobook", tags: "", favorite: false, source_available: true,
    total_chapters: 2, playback_path: "/library/7.m4b", playback_status: "packaged", playback_position_sec: positionSec,
    playback_url: "/api/library/7/audio",
    chapters: [
      { id: 1, number: 1, title: "One", duration_ms: 60_000, packaged_start_ms: 0, mp3_path: "1.mp3" },
      { id: 2, number: 2, title: "Two", duration_ms: 60_000, packaged_start_ms: 60_000, mp3_path: "2.mp3" },
    ],
  };
}

function libraryFor(detail: LibraryBookDetail): UseLibraryResult {
  return {
    items: [{ id: detail.id, title: detail.title }], selectedBookId: detail.id, selectedBookIndex: 0,
    selectedBookDetail: detail, selectedBookPlayable: true, refreshLibrary: vi.fn(async () => {}),
    selectBook: vi.fn(async () => detail), selectRelative: vi.fn(async () => null), setSelectedPlaybackPosition: vi.fn(),
  } as unknown as UseLibraryResult;
}

function fakeAudio(): HTMLAudioElement {
  return {
    src: "", paused: true, ended: false, currentTime: 0, duration: 120, readyState: 1, volume: 1, muted: false,
    crossOrigin: null, pause: vi.fn(), play: vi.fn(async () => {}), load: vi.fn(), removeAttribute: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
  } as unknown as HTMLAudioElement;
}

beforeEach(() => { vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true); });
afterEach(() => {
  window.localStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("useAudioPlayer rendering", () => {
  it("shows the position from each media time update in the title", async () => {
    const apiClient = {
      getLibraryAudioUrl: vi.fn(async () => "http://desktop.test/book"),
      updateBookPlaybackPosition: vi.fn(async (_id: number, position: number) => ({ position_sec: position })),
    } as unknown as ApiClient;
    const library = libraryFor(bookDetail());
    let current!: ReturnType<typeof useAudioPlayer>;
    function Probe() {
      current = useAudioPlayer({ apiClient, library });
      return null;
    }
    const root = createRoot(document.createElement("div"));
    await act(async () => root.render(<Probe />));
    const audio = fakeAudio();
    current.audioRef.current = audio;
    await act(async () => { await current.togglePlay(); });

    for (const second of [3, 4, 5]) {
      Object.assign(audio, { currentTime: second + 0.25 });
      await act(async () => current.audioProps.onTimeUpdate({} as never));
      expect(current.title).toBe(`Paused · 00:0${second}`);
    }
    await act(async () => root.unmount());
  });
});

describe("playback without a backend", () => {
  it("says why Play does nothing while the backend is disconnected", async () => {
    let current!: ReturnType<typeof useAudioPlayer>;
    const library = libraryFor(bookDetail());
    function Probe() {
      current = useAudioPlayer({ apiClient: null, library });
      return null;
    }
    const root = createRoot(document.createElement("div"));
    await act(async () => root.render(<Probe />));
    current.audioRef.current = fakeAudio();
    await act(async () => { await current.togglePlay(); });
    expect(current.title).toBe("Can't play: the backend is not responding. Reconnecting…");
    await act(async () => root.unmount());
  });
});

describe("player state before and across loads", () => {
  it("shows the saved position, not 00:00, before the book is loaded", async () => {
    let current!: ReturnType<typeof useAudioPlayer>;
    const library = libraryFor(bookDetail(74));
    function Probe() {
      current = useAudioPlayer({ apiClient: null, library });
      return null;
    }
    const root = createRoot(document.createElement("div"));
    await act(async () => root.render(<Probe />));
    expect(current.elapsedSec).toBe(74);
    expect(current.currentChapter?.number).toBe(2);
    await act(async () => root.unmount());
  });

  it("remembers the volume between sessions", async () => {
    let current!: ReturnType<typeof useAudioPlayer>;
    const library = libraryFor(bookDetail());
    function Probe() {
      current = useAudioPlayer({ apiClient: null, library });
      return null;
    }
    const first = createRoot(document.createElement("div"));
    await act(async () => first.render(<Probe />));
    await act(async () => current.setPlayerVolume(35));
    await act(async () => first.unmount());
    const second = createRoot(document.createElement("div"));
    await act(async () => second.render(<Probe />));
    expect(current.volume).toBe(35);
    await act(async () => second.unmount());
  });
});

describe("the end of a book", () => {
  it("shows the book as finished when the library has no next book", async () => {
    const apiClient = {
      getLibraryAudioUrl: vi.fn(async () => "http://desktop.test/book"),
      updateBookPlaybackPosition: vi.fn(async (_id: number, position: number) => ({ position_sec: position })),
    } as unknown as ApiClient;
    const library = libraryFor(bookDetail());
    let current!: ReturnType<typeof useAudioPlayer>;
    function Probe() {
      current = useAudioPlayer({ apiClient, library });
      return null;
    }
    const root = createRoot(document.createElement("div"));
    await act(async () => root.render(<Probe />));
    const audio = fakeAudio();
    current.audioRef.current = audio;
    await act(async () => { await current.togglePlay(); });
    Object.assign(audio, { currentTime: 120, ended: true, paused: true });
    await act(async () => current.audioProps.onEnded());
    expect(current.title).toBe("Finished");
    expect(current.playing).toBe(false);
    await act(async () => root.unmount());
  });
});
