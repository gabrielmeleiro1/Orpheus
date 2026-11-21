// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RuntimeSetupStatusResponse } from "./types/api";

const mocks = vi.hoisted(() => {
  const apiClient = {
    getRuntimeSetupStatus: vi.fn(),
    getActiveConversion: vi.fn(async () => ({ active: false, job: null })),
  };
  const items = Array.from({ length: 30 }, (_, index) => ({ id: index + 1, title: `Book ${index + 1}`, author: "Author", source_path: null }));
  const library = { items, unfilteredItems: items, indexedCount: items.length, selectedBookId: 1, selectedBookDetail: null, libraryGeneration: 0,
    loadingList: false, refreshLibrary: vi.fn(async () => undefined), selectBook: vi.fn(async () => null),
    selectBookById: vi.fn(async () => null) };
  return {
    apiClient,
    library,
    elapsedSec: 0,
    playing: true,
    coverRenders: 0,
    backend: {
      apiClient,
      backendBaseUrl: "http://127.0.0.1:48123",
      connectionState: "connected" as const,
      health: null,
      statusMessage: "ready",
      reconnectBackend: vi.fn(),
    },
  };
});

vi.mock("./app/useBackendClient", () => ({ useBackendClient: () => mocks.backend }));
vi.mock("./app/useRuntimeBootstrapRefresh", () => ({ useRuntimeBootstrapRefresh: vi.fn() }));
vi.mock("./app/useGlobalShortcuts", () => ({ useGlobalShortcuts: vi.fn() }));
vi.mock("./features/library/useLibrary", () => ({ useLibrary: () => mocks.library }));
vi.mock("./features/player/useAudioPlayer", () => ({
  useAudioPlayer: () => ({
    sleepTimer: { kind: "off" }, sleepRemainingSec: null, setSleepTimer: vi.fn(), playRelative: vi.fn(), seekBy: vi.fn(),
    seekTo: vi.fn(), togglePlay: vi.fn(), jumpToChapter: vi.fn(), releaseBookAudio: vi.fn(), closePlayer: vi.fn(),
    setPlayerVolume: vi.fn(), playerEligible: true, playerVisible: true, playing: mocks.playing, title: "",
    playingBookId: 1, playbackDetail: null, chapterTransport: false, playSelectedBook: vi.fn(),
    elapsedSec: mocks.elapsedSec, durationSec: 600, canSeek: true, volume: 80, canGoPrevious: false, canGoNext: false,
    audioRef: { current: null }, audioProps: {}, currentChapter: null, currentChapterLabel: "", chapterPlaylist: false,
  }),
}));
vi.mock("./features/conversion/useConversionJob", () => ({
  useConversionJob: () => ({
    uiState: "idle", activeJobId: null, activeJobTitle: "", isCancelling: false, progressPct: 0, logs: [], stages: [],
    startConversion: vi.fn(),
  }),
}));
vi.mock("./features/setup/SetupPage", () => ({
  SetupPage: ({ onContinueLibraryOnly }: { onContinueLibraryOnly: () => void }) => (
    <button type="button" onClick={onContinueLibraryOnly}>Continue in library-only mode</button>
  ),
}));
vi.mock("./features/conversion/ConversionPanel", () => ({ ConversionPanel: () => null }));
vi.mock("./features/library/LibraryPanel", () => ({ LibraryPanel: () => null }));
vi.mock("./features/library/LibraryMaintenanceSection", () => ({ LibraryMaintenanceSection: () => null }));
vi.mock("./features/player/PlayerPanel", () => ({ PlayerPanel: () => null }));
vi.mock("./features/player/NowPlayingView", () => ({ NowPlayingView: () => null }));
vi.mock("./features/progress/ConversionDetailsDrawer", () => ({ ConversionDetailsDrawer: () => null }));
// An unmemoized stand-in, so any re-render of a sidebar row is counted.
vi.mock("./ui/BookCover", () => ({
  BookCover: () => {
    mocks.coverRenders += 1;
    return <span className="book-cover" />;
  },
}));

import App from "./App";

const setupStatus = {
  schema_version: 1, configured: false, state: "unconfigured", state_error: null, selected_profile: null,
  selected_model_groups: [], packages: { ready: false, missing: [], incompatible: [] }, probe: { ready: false, checks: [] },
  ready: false, restart_required: false, profiles: [], backend: { instance_id: "backend-1", status: "running" },
} as unknown as RuntimeSetupStatusResponse;

describe("App sidebar library rendering", () => {
  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
    window.localStorage.clear();
    mocks.apiClient.getRuntimeSetupStatus.mockResolvedValue(setupStatus);
    mocks.elapsedSec = 0;
    mocks.playing = true;
  });
  afterEach(() => vi.unstubAllGlobals());

  it("does not re-render the sidebar books on playback ticks", async () => {
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    try {
      await act(async () => root.render(<App />));
      await act(async () => [...container.querySelectorAll("button")]
        .find((button) => button.textContent?.includes("Continue in library-only mode"))!.click());
      expect(container.querySelectorAll(".rail-book")).toHaveLength(30);
      expect(container.querySelector(".rail-book .rail-book-author")?.textContent).toBe("Playing");

      mocks.coverRenders = 0;
      for (let second = 1; second <= 10; second += 1) {
        mocks.elapsedSec = second;
        await act(async () => root.render(<App />));
      }
      expect(mocks.coverRenders).toBe(0);

      mocks.playing = false;
      await act(async () => root.render(<App />));
      expect(container.querySelector(".rail-book .rail-book-author")?.textContent).toBe("Author");
    } finally {
      await act(async () => root.unmount());
      container.remove();
    }
  });

  it("lists the whole library in the sidebar while the panel's list is filtered", async () => {
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    const all = mocks.library.items;
    mocks.library.items = all.slice(2, 3);
    try {
      await act(async () => root.render(<App />));
      await act(async () => [...container.querySelectorAll("button")]
        .find((button) => button.textContent?.includes("Continue in library-only mode"))!.click());
      expect(container.querySelectorAll(".rail-book")).toHaveLength(30);
      await act(async () => container.querySelectorAll<HTMLButtonElement>(".rail-book")[4].click());
      expect(mocks.library.selectBookById).toHaveBeenCalledExactlyOnceWith(5);
    } finally {
      mocks.library.items = all;
      await act(async () => root.unmount());
      container.remove();
    }
  });
});
