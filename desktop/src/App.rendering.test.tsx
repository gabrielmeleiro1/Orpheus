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
    renders: { setup: 0, maintenance: 0, nowPlaying: 0, drawer: 0 },
    settingsHidden: [] as boolean[],
    settingsPageRenders: 0,
    panelRenders: [] as Array<{ hidden: boolean; progressPct: number }>,
    conversionJob: {
      uiState: "idle", activeJobId: null, activeJobTitle: "", isCancelling: false, progressPct: 0, logs: [], stages: [],
      startConversion: vi.fn(),
    } as Record<string, unknown>,
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
// One object per job state, as the real hook memoizes it; an SSE event replaces it.
vi.mock("./features/conversion/useConversionJob", () => ({ useConversionJob: () => mocks.conversionJob }));
vi.mock("./features/setup/SetupPage", () => ({
  SetupPage: ({ onContinueLibraryOnly, settingsEntry, hidden }: { onContinueLibraryOnly: () => void; settingsEntry?: boolean; hidden?: boolean }) => {
    if (settingsEntry) {
      mocks.renders.setup += 1;
      mocks.settingsHidden.push(hidden === true);
      return null;
    }
    return <button type="button" onClick={onContinueLibraryOnly}>Continue in library-only mode</button>;
  },
}));
vi.mock("./features/conversion/ConversionPanel", () => ({
  ConversionPanel: ({ hidden, conversionJob }: { hidden: boolean; conversionJob: { progressPct: number } }) => {
    mocks.panelRenders.push({ hidden, progressPct: conversionJob.progressPct });
    return null;
  },
}));
vi.mock("./features/setup/SettingsPage", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./features/setup/SettingsPage")>();
  return {
    SettingsPage: (props: Parameters<typeof actual.SettingsPage>[0]) => {
      mocks.settingsPageRenders += 1;
      return actual.SettingsPage(props);
    },
  };
});
vi.mock("./features/library/LibraryPanel", () => ({ LibraryPanel: () => null }));
vi.mock("./features/library/LibraryMaintenanceSection", () => ({ LibraryMaintenanceSection: () => { mocks.renders.maintenance += 1; return null; } }));
vi.mock("./features/player/PlayerPanel", () => ({ PlayerPanel: () => null }));
vi.mock("./features/player/NowPlayingView", () => ({ NowPlayingView: () => { mocks.renders.nowPlaying += 1; return null; } }));
vi.mock("./features/progress/ConversionDetailsDrawer", () => ({ ConversionDetailsDrawer: () => { mocks.renders.drawer += 1; return null; } }));
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

  it("does not re-render hidden Settings sections or closed overlays on playback ticks, and updates them once shown", async () => {
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    const tab = (label: string) => container.querySelector<HTMLButtonElement>(`[role="tab"][aria-label="${label}"]`)!;
    const tick = async (count: number) => {
      for (let step = 0; step < count; step += 1) {
        mocks.elapsedSec += 1;
        await act(async () => root.render(<App />));
      }
    };
    try {
      await act(async () => root.render(<App />));
      await act(async () => [...container.querySelectorAll("button")]
        .find((button) => button.textContent?.includes("Continue in library-only mode"))!.click());
      await act(async () => tab("Settings").click());
      expect(mocks.settingsHidden.at(-1)).toBe(false);
      await act(async () => tab("Library").click());
      expect(mocks.settingsHidden.at(-1)).toBe(true);

      mocks.renders = { setup: 0, maintenance: 0, nowPlaying: 0, drawer: 0 };
      await tick(10);
      expect(mocks.renders).toEqual({ setup: 0, maintenance: 0, nowPlaying: 0, drawer: 0 });

      await act(async () => tab("Settings").click());
      expect(mocks.renders.setup).toBeGreaterThan(0);
      expect(mocks.settingsHidden.at(-1)).toBe(false);
      // Visible with unchanged props: ticks still do not reach it.
      mocks.renders.setup = 0;
      await tick(3);
      expect(mocks.renders.setup).toBe(0);
    } finally {
      await act(async () => root.unmount());
      container.remove();
    }
  });

  it("keeps playback ticks and progress events out of the hidden Settings sections while Create keeps updating", async () => {
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    const tab = (label: string) => container.querySelector<HTMLButtonElement>(`[role="tab"][aria-label="${label}"]`)!;
    const tick = async (count: number) => {
      for (let step = 0; step < count; step += 1) {
        mocks.elapsedSec += 1;
        await act(async () => root.render(<App />));
      }
    };
    const progressEvent = async (progressPct: number) => {
      mocks.conversionJob = { ...mocks.conversionJob, uiState: "running", activeJobId: "job-1", progressPct };
      await act(async () => root.render(<App />));
    };
    const reset = () => {
      mocks.renders = { setup: 0, maintenance: 0, nowPlaying: 0, drawer: 0 };
      mocks.settingsPageRenders = 0;
      mocks.panelRenders = [];
    };
    const idleJob = mocks.conversionJob;
    try {
      await act(async () => root.render(<App />));
      await act(async () => [...container.querySelectorAll("button")]
        .find((button) => button.textContent?.includes("Continue in library-only mode"))!.click());
      await act(async () => tab("Settings").click());
      expect(container.querySelector('section[aria-label="Storage"]')).not.toBeNull();
      await act(async () => tab("Create").click());
      expect(container.querySelector('section[aria-label="Storage"]')).toBeNull();

      // On Create: ticks reach neither the Settings page nor the panel.
      reset();
      await tick(5);
      expect(mocks.settingsPageRenders).toBe(0);
      expect(mocks.panelRenders).toEqual([]);

      // A progress event still reaches the visible panel, but no Settings-only section.
      await progressEvent(40);
      expect(mocks.panelRenders.at(-1)).toEqual({ hidden: false, progressPct: 40 });
      expect(mocks.renders.setup).toBe(0);
      expect(mocks.renders.maintenance).toBe(0);

      // On Library: neither ticks nor progress events reach the Settings sections.
      await act(async () => tab("Library").click());
      reset();
      await tick(5);
      await progressEvent(55);
      expect(mocks.renders.setup).toBe(0);
      expect(mocks.renders.maintenance).toBe(0);
      // Only the progress event reaches the page, to hand the hidden panel the new job.
      expect(mocks.settingsPageRenders).toBe(1);

      // Shown again, the sections render and the panel has the newest progress.
      await act(async () => tab("Settings").click());
      expect(mocks.renders.setup).toBeGreaterThan(0);
      expect(mocks.renders.maintenance).toBeGreaterThan(0);
      expect(mocks.panelRenders.at(-1)).toEqual({ hidden: false, progressPct: 55 });
      expect(container.querySelector('section[aria-label="Storage"]')).not.toBeNull();
      // Visible Settings: ticks still do not reach the page.
      reset();
      await tick(3);
      expect(mocks.settingsPageRenders).toBe(0);
      expect(mocks.renders.maintenance).toBe(0);
    } finally {
      mocks.conversionJob = idleJob;
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
