// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { RuntimeSetupStatusResponse } from "./types/api";

const mocks = vi.hoisted(() => {
  const apiClient = {
    getRuntimeSetupStatus: vi.fn(),
    getActiveConversion: vi.fn(async () => ({ active: false, job: null })),
  };
  return {
    apiClient,
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
vi.mock("./app/usePrimaryRuntime", () => ({ useRuntimeIdentity: () => ({ ttsLabel: "", cleanerLabel: "", ttsReady: false }) }));
vi.mock("./app/useGlobalShortcuts", () => ({ useGlobalShortcuts: vi.fn() }));
vi.mock("./features/library/useLibrary", () => ({
  useLibrary: () => ({ items: [], indexedCount: 0, refreshLibrary: vi.fn(async () => undefined), selectBook: vi.fn(async () => null), selectedBookDetail: null }),
}));
vi.mock("./features/player/useAudioPlayer", () => ({
  useAudioPlayer: () => ({
    sleepTimer: { kind: "off" }, sleepRemainingSec: null, setSleepTimer: vi.fn(),
    playRelative: vi.fn(), seekBy: vi.fn(), seekTo: vi.fn(), togglePlay: vi.fn(), jumpToChapter: vi.fn(),
    playerEligible: true, playerVisible: true, playing: false, title: "Book", statusText: "",
    elapsedSec: 0, durationSec: 0, canSeek: false, volume: 1, canGoPrevious: false, canGoNext: false,
    chapterPlaylist: false, audioRef: { current: null }, audioProps: {}, currentChapter: null, currentChapterLabel: "",
    closePlayer: vi.fn(), setPlayerVolume: vi.fn(), releaseBookAudio: vi.fn(),
  }),
}));
vi.mock("./features/conversion/useConversionJob", () => ({
  useConversionJob: () => ({
    uiState: "idle", activeJobId: null, activeJobStatus: "idle", isCancelling: false, isStarting: false,
    formError: "", statusText: "", progressPct: 0, etaText: "", stages: [], currentStage: null, currentMessage: "",
    performanceSnapshot: null, logs: [], cancelActiveConversion: vi.fn(), refreshActiveConversion: vi.fn(), clearFormError: vi.fn(),
  }),
}));
vi.mock("./features/setup/SetupPage", () => ({
  SetupPage: ({ onContinueLibraryOnly, embedded }: { onContinueLibraryOnly: () => void; embedded?: boolean }) =>
    embedded ? null : <button type="button" onClick={onContinueLibraryOnly}>Continue in library-only mode</button>,
}));
vi.mock("./features/conversion/ConversionPanel", () => ({ ConversionPanel: () => null }));
vi.mock("./features/library/LibraryPanel", () => ({ LibraryPanel: () => <section id="tab-library" role="tabpanel" /> }));
vi.mock("./features/library/LibraryMaintenanceSection", () => ({ LibraryMaintenanceSection: () => null }));
// The real player bar has two ways into Now Playing: the book itself and an icon button.
vi.mock("./features/player/PlayerPanel", () => ({
  PlayerPanel: ({ onToggleNowPlaying }: { onToggleNowPlaying: () => void }) => (
    <footer>
      <button type="button" className="player-open-now-playing" onClick={onToggleNowPlaying}>Open now playing: Book</button>
      <button type="button" id="player-now-playing-btn" onClick={onToggleNowPlaying}>Now playing</button>
    </footer>
  ),
}));

import App from "./App";

const setupStatus = {
  schema_version: 1, configured: false, state: "unconfigured", state_error: null, selected_profile: null,
  selected_model_groups: [], packages: { ready: false, missing: [], incompatible: [] }, probe: { ready: false, checks: [] },
  ready: false, restart_required: false, profiles: [], risk: null, backend: { instance_id: "backend-1", status: "running" },
} as RuntimeSetupStatusResponse;

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  window.localStorage.clear();
  mocks.apiClient.getRuntimeSetupStatus.mockResolvedValue(setupStatus);
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.clearAllMocks();
});

it("returns focus to the control that opened Now Playing when Escape closes it", async () => {
  await act(async () => root.render(<App />));
  const libraryOnly = [...container.querySelectorAll("button")].find((button) => button.textContent === "Continue in library-only mode")!;
  await act(async () => libraryOnly.click());
  const opener = container.querySelector<HTMLButtonElement>(".player-open-now-playing")!;
  opener.focus();
  await act(async () => opener.click());
  const view = container.querySelector<HTMLElement>("[role='dialog'][aria-label='Now playing']")!;
  expect(view.contains(document.activeElement)).toBe(true);
  await act(async () => view.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
  expect(container.querySelector("[role='dialog'][aria-label='Now playing']")).toBeNull();
  expect(document.activeElement).toBe(opener);
});
