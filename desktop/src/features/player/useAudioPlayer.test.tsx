import { renderToString } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import type { UseLibraryResult } from "../library/useLibrary";
import type { LibraryBookDetail } from "../../types/api";
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
  vi.restoreAllMocks();
});

describe("useAudioPlayer audio load failures", () => {
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
});
