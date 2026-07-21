// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RuntimeSetupStatusResponse } from "./types/api";

const mocks = vi.hoisted(() => {
  const apiClient = {
    getRuntimeSetupStatus: vi.fn(),
  };
  const setupBridge = {
    environment: vi.fn(),
    status: vi.fn(),
    listen: vi.fn(),
    start: vi.fn(),
    cancel: vi.fn(),
    restart: vi.fn(),
  };

  return {
    apiClient,
    setupBridge,
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

vi.mock("./app/useBackendClient", () => ({
  useBackendClient: () => mocks.backend,
}));
vi.mock("./app/usePrimaryRuntime", () => ({
  useRuntimeIdentityLabel: () => "TTS: Library only",
}));
vi.mock("./app/useGlobalShortcuts", () => ({
  useGlobalShortcuts: vi.fn(),
}));
vi.mock("./features/library/useLibrary", () => ({
  useLibrary: () => ({ items: [], indexedCount: 0, refreshLibrary: vi.fn(async () => undefined) }),
}));
vi.mock("./features/player/useAudioPlayer", () => ({
  useAudioPlayer: () => ({
    playRelative: vi.fn(),
    seekBy: vi.fn(),
    seekTo: vi.fn(),
    togglePlay: vi.fn(),
    playerEligible: false,
    playerVisible: false,
    playing: false,
    title: "",
    statusText: "",
    elapsedSec: 0,
    durationSec: 0,
    canSeek: false,
    volume: 1,
    canGoPrevious: false,
    canGoNext: false,
    audioRef: { current: null },
    audioProps: {},
    currentChapter: null,
    currentChapterLabel: "",
    closePlayer: vi.fn(),
    setPlayerVolume: vi.fn(),
  }),
}));
vi.mock("./features/conversion/useConversionJob", () => ({
  useConversionJob: () => ({
    uiState: "idle",
    activeJobId: null,
    activeJobStatus: "idle",
    isCancelling: false,
    isStarting: false,
    formError: "",
    statusText: "",
    progressPct: 0,
    etaText: "",
    stages: [],
    currentStage: null,
    currentMessage: "",
    performanceSnapshot: null,
    logs: [],
    cancelActiveConversion: vi.fn(async () => undefined),
    refreshActiveConversion: vi.fn(async () => undefined),
    clearFormError: vi.fn(),
  }),
}));
vi.mock("./features/conversion/ConversionPanel", () => ({
  ConversionPanel: ({ activeTab, onOpenRuntimeSetup }: { activeTab: string; onOpenRuntimeSetup?: () => void }) => (
    activeTab === "config" ? (
      <button type="button" onClick={onOpenRuntimeSetup}>Runtime setup</button>
    ) : null
  ),
}));
vi.mock("./features/setup/setupBridge", () => ({
  getRuntimeSetupEnvironment: () => mocks.setupBridge.environment(),
  getRuntimeSetupRunnerStatus: () => mocks.setupBridge.status(),
  listenRuntimeSetupProgress: (onProgress: (event: unknown) => void) => mocks.setupBridge.listen(onProgress),
  startRuntimeSetup: (request: unknown) => mocks.setupBridge.start(request),
  cancelRuntimeSetup: () => mocks.setupBridge.cancel(),
  restartOrpheusBackend: () => mocks.setupBridge.restart(),
}));
vi.mock("./features/dashboard/DashboardPanel", () => ({ DashboardPanel: () => null }));
vi.mock("./features/library/LibraryPanel", () => ({ LibraryPanel: () => null }));
vi.mock("./features/logs/LogPanel", () => ({ LogPanel: () => null }));
vi.mock("./features/player/PlayerPanel", () => ({ PlayerPanel: () => null }));
vi.mock("./features/progress/ProgressPanel", () => ({ ProgressPanel: () => null }));

import App from "./App";

const readyStatus: RuntimeSetupStatusResponse = {
  schema_version: 1,
  configured: true,
  state: "ready",
  state_error: null,
  selected_profile: "base",
  selected_model_groups: [],
  packages: { ready: true, missing: [], incompatible: [] },
  probe: { ready: true, checks: [] },
  ready: true,
  restart_required: false,
  profiles: [],
  risk: null,
  backend: { instance_id: "backend-ready", status: "running" },
};

function findButton(container: HTMLElement, text: string): HTMLButtonElement {
  const button = [...container.querySelectorAll("button")].find((item) => item.textContent?.includes(text));
  if (!button) throw new Error("missing button: " + text);
  return button;
}

describe("App runtime setup integration", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    mocks.apiClient.getRuntimeSetupStatus.mockResolvedValue(readyStatus);
    mocks.setupBridge.environment.mockResolvedValue({ available: true, mode: "source_checkout", reason: "" });
    mocks.setupBridge.status.mockResolvedValue({
      lifecycle: "idle",
      profile: null,
      modelGroups: [],
      restartRequired: false,
      error: null,
    });
    mocks.setupBridge.listen.mockResolvedValue(vi.fn());
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.clearAllMocks();
  });

  it("requests fresh status and keeps real Settings-entry setup open until Back", async () => {
    let resolveFreshStatus!: (status: RuntimeSetupStatusResponse) => void;
    const freshStatus = new Promise<RuntimeSetupStatusResponse>((resolve) => {
      resolveFreshStatus = resolve;
    });
    mocks.apiClient.getRuntimeSetupStatus
      .mockResolvedValueOnce(readyStatus)
      .mockImplementationOnce(() => freshStatus);

    await act(async () => root.render(<App />));
    expect(container.querySelector(".setup-frame")).toBeNull();

    await act(async () => findButton(container, "Settings").click());
    await act(async () => findButton(container, "Runtime setup").click());

    expect(mocks.apiClient.getRuntimeSetupStatus).toHaveBeenCalledTimes(2);
    expect(container.querySelector(".setup-frame")).not.toBeNull();
    expect(document.activeElement?.id).toBe("setup-heading");

    await act(async () => resolveFreshStatus(readyStatus));
    expect(container.querySelector(".setup-frame")).not.toBeNull();
    expect(document.activeElement?.id).toBe("setup-heading");

    await act(async () => findButton(container, "Back to dashboard").click());
    expect(container.querySelector(".setup-frame")).toBeNull();
  });
});
