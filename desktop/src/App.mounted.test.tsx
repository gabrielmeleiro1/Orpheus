// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RuntimeSetupStatusResponse } from "./types/api";

const mocks = vi.hoisted(() => {
  const apiClient = {
    getRuntimeSetupStatus: vi.fn(),
  };

  return {
    apiClient,
    libraryRefresh: vi.fn(async () => undefined),
    conversionUiState: "idle" as "idle" | "completed" | "failed" | "cancelled",
    setupPageStatuses: [] as Array<RuntimeSetupStatusResponse | null>,
    backend: {
      apiClient: apiClient as typeof apiClient | null,
      backendBaseUrl: "http://127.0.0.1:48123",
      connectionState: "connected" as "connected" | "checking" | "error",
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
  useLibrary: () => ({ items: [{ id: "book-1" }], indexedCount: 1, refreshLibrary: mocks.libraryRefresh }),
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
    uiState: mocks.conversionUiState,
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
vi.mock("./features/setup/SetupPage", () => ({
  SetupPage: ({
    initialStatus,
    onReady,
    onContinueLibraryOnly,
    onExitSettings,
    settingsEntry,
  }: {
    initialStatus: RuntimeSetupStatusResponse | null;
    onReady: (status: RuntimeSetupStatusResponse) => void;
    onContinueLibraryOnly: () => void;
    onExitSettings?: () => void;
    settingsEntry?: boolean;
  }) => {
    mocks.setupPageStatuses.push(initialStatus);
    return (
    <section data-testid="setup-page">
      <h1>Runtime setup</h1>
      <span data-testid="setup-status">{initialStatus?.state ?? "unavailable"}</span>
      <button type="button" onClick={onContinueLibraryOnly}>Continue in library-only mode</button>
      {initialStatus?.ready ? (
        <button type="button" onClick={() => onReady(initialStatus)}>Complete runtime setup</button>
      ) : null}
      {settingsEntry && onExitSettings ? (
        <button type="button" onClick={onExitSettings}>Back to dashboard</button>
      ) : null}
    </section>
    );
  },
}));
vi.mock("./features/conversion/ConversionPanel", () => ({
  ConversionPanel: ({
    activeTab,
    onOpenRuntimeSetup,
    onOpenSettings,
  }: {
    activeTab: string;
    onOpenRuntimeSetup?: () => void;
    onOpenSettings?: () => void;
  }) => (
    <>
      <button type="button" onClick={onOpenSettings}>Open Settings</button>
      {activeTab === "config" ? (
        <button type="button" onClick={onOpenRuntimeSetup}>Runtime setup</button>
      ) : null}
    </>
  ),
}));
vi.mock("./features/dashboard/DashboardPanel", () => ({ DashboardPanel: () => null }));
vi.mock("./features/library/LibraryPanel", () => ({ LibraryPanel: () => null }));
vi.mock("./features/logs/LogPanel", () => ({ LogPanel: () => null }));
vi.mock("./features/player/PlayerPanel", () => ({ PlayerPanel: () => null }));
vi.mock("./features/progress/ProgressPanel", () => ({ ProgressPanel: () => null }));

import App from "./App";

const setupStatus: RuntimeSetupStatusResponse = {
  schema_version: 1,
  configured: false,
  state: "unconfigured",
  state_error: null,
  selected_profile: null,
  selected_model_groups: [],
  packages: { ready: false, missing: [], incompatible: [] },
  probe: { ready: false, checks: [] },
  ready: false,
  restart_required: false,
  profiles: [],
  risk: null,
  backend: { instance_id: "backend-1", status: "running" },
};

function findButton(container: HTMLElement, text: string): HTMLButtonElement {
  const button = [...container.querySelectorAll("button")].find((item) => item.textContent?.includes(text));
  if (!button) throw new Error("missing button: " + text);
  return button;
}

describe("App runtime setup recovery", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    window.localStorage.clear();
    mocks.backend.apiClient = mocks.apiClient;
    mocks.backend.connectionState = "connected";
    mocks.backend.statusMessage = "ready";
    mocks.apiClient.getRuntimeSetupStatus.mockResolvedValue(setupStatus);
    mocks.libraryRefresh.mockClear();
    mocks.conversionUiState = "idle";
    mocks.setupPageStatuses.length = 0;
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.clearAllMocks();
  });

  it.each([
    "Desktop backend startup timed out after 60s",
    "backend failed to recover after multiple attempts; select Retry connection to try again",
  ])("shows first-launch backend failure and an explicit retry: %s", async (message) => {
    mocks.backend.apiClient = null;
    mocks.backend.connectionState = "error";
    mocks.backend.statusMessage = message;
    await act(async () => root.render(<App />));

    expect(container.querySelector('[role="alert"]')?.textContent).toContain(message);
    expect(container.textContent).not.toContain("Checking your local runtime");
    await act(async () => findButton(container, "Retry connection").click());
    expect(mocks.backend.reconnectBackend).toHaveBeenCalledOnce();

    mocks.backend.connectionState = "checking";
    await act(async () => root.render(<App />));
    expect(container.textContent).toContain("Checking your local runtime");
    expect(container.querySelector('[role="alert"]')).toBeNull();

    mocks.backend.apiClient = mocks.apiClient;
    mocks.backend.connectionState = "connected";
    await act(async () => root.render(<App />));
    expect(container.querySelector('[data-testid="setup-page"]')).not.toBeNull();
    expect(mocks.apiClient.getRuntimeSetupStatus).toHaveBeenCalledOnce();
  });

  it("preserves the ready-hint dashboard when the backend is unavailable", async () => {
    window.localStorage.setItem("orpheus.runtime-setup-ready", JSON.stringify({ version: 1, ready: true }));
    mocks.backend.apiClient = null;
    mocks.backend.connectionState = "error";
    await act(async () => root.render(<App />));
    expect(container.querySelector('[aria-label="Orpheus startup"]')).toBeNull();
    expect(findButton(container, "Settings")).toBeDefined();
  });

  it("reopens the full-page setup from Settings without resetting persisted data", async () => {
    await act(async () => {
      root.render(<App />);
    });
    expect(container.querySelector('[data-testid="setup-page"]')).not.toBeNull();

    await act(async () => findButton(container, "Continue in library-only mode").click());
    expect(container.querySelector('[data-testid="setup-page"]')).toBeNull();

    await act(async () => findButton(container, "Settings").click());
    await act(async () => findButton(container, "Runtime setup").click());

    expect(container.querySelector('[data-testid="setup-page"]')).not.toBeNull();
    expect(mocks.apiClient.getRuntimeSetupStatus).toHaveBeenCalled();
  });

  it("force-opens SetupPage with fresh status and keeps it open until explicit exit", async () => {
    const initialReadyStatus: RuntimeSetupStatusResponse = {
      ...setupStatus,
      configured: true,
      state: "ready",
      selected_profile: "base",
      ready: true,
      packages: { ready: true, missing: [], incompatible: [] },
      probe: { ready: true, checks: [] },
    };
    const freshReadyStatus: RuntimeSetupStatusResponse = {
      ...initialReadyStatus,
      backend: { instance_id: "backend-fresh", status: "running" },
    };
    const incompleteStatus: RuntimeSetupStatusResponse = {
      ...setupStatus,
      backend: { instance_id: "backend-incomplete", status: "running" },
    };
    let resolveFreshStatus!: (status: RuntimeSetupStatusResponse) => void;
    const freshStatus = new Promise<RuntimeSetupStatusResponse>((resolve) => {
      resolveFreshStatus = resolve;
    });
    mocks.apiClient.getRuntimeSetupStatus
      .mockResolvedValueOnce(initialReadyStatus)
      .mockImplementationOnce(() => freshStatus)
      .mockResolvedValueOnce(incompleteStatus);

    await act(async () => {
      root.render(<App />);
    });
    expect(container.querySelector('[data-testid="setup-page"]')).toBeNull();

    await act(async () => findButton(container, "Settings").click());
    await act(async () => findButton(container, "Runtime setup").click());
    expect(mocks.apiClient.getRuntimeSetupStatus).toHaveBeenCalledTimes(2);
    expect(container.querySelector('[data-testid="setup-status"]')?.textContent).toBe("unavailable");

    await act(async () => resolveFreshStatus(freshReadyStatus));
    expect(container.querySelector('[data-testid="setup-page"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="setup-status"]')?.textContent).toBe("ready");
    expect(mocks.setupPageStatuses.some((status) => status === initialReadyStatus)).toBe(false);
    expect(mocks.setupPageStatuses.some((status) => status === freshReadyStatus)).toBe(true);

    await act(async () => findButton(container, "Complete runtime setup").click());
    expect(container.querySelector('[data-testid="setup-page"]')).not.toBeNull();

    await act(async () => findButton(container, "Back to dashboard").click());
    expect(mocks.apiClient.getRuntimeSetupStatus).toHaveBeenCalledTimes(3);
    expect(container.querySelector('[data-testid="setup-page"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="setup-status"]')?.textContent).toBe("unconfigured");
    expect(container.textContent).not.toContain("Back to dashboard");
  });

  it("uses the library-only bypass when backing out of incomplete Settings setup", async () => {
    await act(async () => {
      root.render(<App />);
    });
    await act(async () => findButton(container, "Continue in library-only mode").click());

    await act(async () => findButton(container, "Settings").click());
    await act(async () => findButton(container, "Runtime setup").click());
    expect(container.querySelector('[data-testid="setup-page"]')).not.toBeNull();

    await act(async () => findButton(container, "Back to dashboard").click());

    expect(container.querySelector('[data-testid="setup-page"]')).toBeNull();
  });

  it("keeps every lazy tab's aria-controls target in the document", async () => {
    await act(async () => {
      root.render(<App />);
    });
    await act(async () => findButton(container, "Continue in library-only mode").click());

    for (const label of ["Convert", "Document", "Library", "Progress", "Logs", "Settings"]) {
      const tab = findButton(container, label);
      const panelId = tab.getAttribute("aria-controls");
      expect(panelId).toBeTruthy();
      expect(container.querySelector(`#${panelId}`)).not.toBeNull();
    }
  });

  it("refreshes the library when a conversion completes", async () => {
    await act(async () => {
      root.render(<App />);
    });
    await act(async () => findButton(container, "Continue in library-only mode").click());

    mocks.conversionUiState = "completed";
    await act(async () => {
      root.render(<App />);
    });

    expect(mocks.libraryRefresh).toHaveBeenCalledTimes(1);
  });

  it.each(["failed", "cancelled"] as const)(
    "refreshes the library when a conversion is %s so partial output appears",
    async (terminalState) => {
      await act(async () => {
        root.render(<App />);
      });
      await act(async () => findButton(container, "Continue in library-only mode").click());

      mocks.conversionUiState = terminalState;
      await act(async () => {
        root.render(<App />);
      });

      expect(mocks.libraryRefresh).toHaveBeenCalledTimes(1);
    },
  );

  it("moves focus to the Settings tab when a conversion shortcut opens Settings", async () => {
    await act(async () => {
      root.render(<App />);
    });
    await act(async () => findButton(container, "Continue in library-only mode").click());

    await act(async () => findButton(container, "Convert").click());
    await act(async () => findButton(container, "Open Settings").click());

    expect(document.activeElement?.id).toBe("tab-btn-settings");
  });
});
