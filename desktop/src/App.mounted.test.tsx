// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import type { Window as HappyDOMWindow } from "happy-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RuntimeSetupStatusResponse } from "./types/api";

const mocks = vi.hoisted(() => {
  const apiClient = {
    getRuntimeSetupStatus: vi.fn(),
    getActiveConversion: vi.fn(async () => ({ active: false, job: { id: "job", status: "completed", book_id: 7, repair: false } })),
  };

  return {
    apiClient,
    conversionPanelProps: null as { hidden?: boolean; apiClient?: unknown; runtimeState?: string; conversionJob?: { uiState: string } } | null,
    libraryRefresh: vi.fn(async () => undefined),
    selectBook: vi.fn(async () => null),
    selectBookById: vi.fn(async () => null),
    activeJobId: null as string | null,
    conversionUiState: "idle" as "idle" | "running" | "completed" | "failed" | "cancelled",
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
vi.mock("./app/useRuntimeBootstrapRefresh", () => ({
  useRuntimeBootstrapRefresh: vi.fn(),
}));
vi.mock("./app/useGlobalShortcuts", () => ({
  useGlobalShortcuts: vi.fn(),
}));
vi.mock("./features/library/useLibrary", () => ({
  useLibrary: () => ({ items: [{ id: 7 }, { id: 8 }], unfilteredItems: [{ id: 7 }, { id: 8 }], indexedCount: 2,
    refreshLibrary: mocks.libraryRefresh, selectBook: mocks.selectBook, selectBookById: mocks.selectBookById }),
}));
vi.mock("./features/player/useAudioPlayer", () => ({
  useAudioPlayer: () => ({
    sleepTimer: { kind: "off" }, sleepRemainingSec: null, setSleepTimer: vi.fn(),
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
    activeJobId: mocks.activeJobId,
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
    embedded,
    hidden,
  }: {
    initialStatus: RuntimeSetupStatusResponse | null;
    onReady: (status: RuntimeSetupStatusResponse) => void;
    onContinueLibraryOnly: () => void;
    embedded?: boolean;
    hidden?: boolean;
  }) => {
    if (embedded) {
      return (
        <section data-testid="settings-models" hidden={hidden}>
          <h2 id="setup-heading" tabIndex={-1}>Voice &amp; models</h2>
          <span data-testid="settings-models-status">{initialStatus?.state ?? "unavailable"}</span>
          <button type="button" onClick={() => onReady({ ...(initialStatus as RuntimeSetupStatusResponse), ready: false, state: "unconfigured" })}>Delete the voice model</button>
        </section>
      );
    }
    mocks.setupPageStatuses.push(initialStatus);
    return (
    <section data-testid="setup-page">
      <h1>Runtime setup</h1>
      <span data-testid="setup-status">{initialStatus?.state ?? "unavailable"}</span>
      <button type="button" onClick={onContinueLibraryOnly}>Continue in library-only mode</button>
      {initialStatus?.ready ? (
        <button type="button" onClick={() => onReady(initialStatus)}>Complete runtime setup</button>
      ) : null}
    </section>
    );
  },
}));
vi.mock("./features/conversion/ConversionPanel", () => ({
  ConversionPanel: (props: {
    hidden?: boolean;
    apiClient?: unknown;
    runtimeState?: string;
    conversionJob?: { uiState: string };
    activeTab: string;
    onOpenSettings?: () => void;
  }) => {
    mocks.conversionPanelProps = props;
    return <button type="button" onClick={props.onOpenSettings}>Open Settings</button>;
  },
}));
vi.mock("./features/library/LibraryPanel", () => ({ LibraryPanel: ({ hidden, albumOpen, onOpenAlbum }: { hidden: boolean; albumOpen: boolean; onOpenAlbum: (index: number) => void }) => <section id="tab-library" role="tabpanel" hidden={hidden} data-album-open={albumOpen}><button onClick={() => onOpenAlbum(0)}>Open album</button></section> }));
vi.mock("./features/library/LibraryMaintenanceSection", () => ({ LibraryMaintenanceSection: () => null }));
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
    mocks.activeJobId = null;
    mocks.apiClient.getActiveConversion.mockReset().mockResolvedValue({ active: false, job: { id: "job", status: "completed", book_id: 7, repair: false } });
    mocks.conversionPanelProps = null;
    mocks.setupPageStatuses.length = 0;
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.clearAllMocks();
    vi.useRealTimers();
  });

  it("tells Create that a library-only session cannot convert", async () => {
    await act(async () => root.render(<App />));
    await act(async () => findButton(container, "Continue in library-only mode").click());
    await act(async () => findButton(container, "Create").click());
    expect(mocks.conversionPanelProps?.hidden).toBe(false);
    expect(mocks.conversionPanelProps?.runtimeState).toBe("unavailable");
  });

  it("updates hidden conversion controllers through disconnect and reconnect", async () => {
    await act(async () => root.render(<App />));
    await act(async () => findButton(container, "Continue in library-only mode").click());
    await act(async () => findButton(container, "Create").click());
    await act(async () => findButton(container, "Library").click());
    expect(mocks.conversionPanelProps?.hidden).toBe(true);
    expect(mocks.conversionPanelProps?.apiClient).toBe(mocks.apiClient);
    mocks.backend.apiClient = null;
    await act(async () => root.render(<App />));
    expect(mocks.conversionPanelProps?.apiClient).toBeNull();
    const replacement = { ...mocks.apiClient, getRuntimeSetupStatus: vi.fn().mockResolvedValue(setupStatus) };
    mocks.backend.apiClient = replacement;
    mocks.conversionUiState = "failed";
    await act(async () => root.render(<App />));
    expect(mocks.conversionPanelProps?.hidden).toBe(true);
    expect(mocks.conversionPanelProps?.apiClient).toBe(replacement);
    expect(mocks.conversionPanelProps?.conversionJob?.uiState).toBe("failed");
  });

  it("explains a slow first status check after five seconds", async () => {
    vi.useFakeTimers();
    try {
      mocks.apiClient.getRuntimeSetupStatus.mockImplementationOnce(() => new Promise(() => undefined));
      await act(async () => root.render(<App />));
      expect(container.textContent).toContain("Checking your local runtime");
      expect(container.textContent).not.toContain("First launch checks");
      await act(async () => vi.advanceTimersByTime(4_999));
      expect(container.textContent).not.toContain("First launch checks");
      await act(async () => vi.advanceTimersByTime(1));
      expect(container.textContent).toContain("First launch checks your GPU and model files — this can take up to a minute.");
      await act(async () => root.unmount());
    } finally {
      vi.useRealTimers();
    }
  });

  it.each([
    "Desktop backend startup timed out after 60s",
    "backend failed to recover after multiple attempts; select Retry to try again",
  ])("shows first-launch backend failure and an explicit retry: %s", async (message) => {
    mocks.backend.apiClient = null;
    mocks.backend.connectionState = "error";
    mocks.backend.statusMessage = message;
    await act(async () => root.render(<App />));

    expect(container.querySelector('[role="alert"]')?.textContent).toContain(message);
    // A human sentence leads; the raw backend message stays as a quiet detail.
    expect(container.querySelector('[role="alert"] p')?.textContent).toBe("Retry, or restart Orpheus if this keeps happening.");
    expect(container.querySelector('[role="alert"] .error-detail')?.textContent).toBe(message);
    expect(container.textContent).not.toContain("Checking your local runtime");
    await act(async () => findButton(container, "Retry").click());
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

  it("stays in Settings when a model change leaves the runtime unready", async () => {
    const ready: RuntimeSetupStatusResponse = {
      ...setupStatus,
      configured: true,
      state: "ready",
      selected_profile: "base",
      ready: true,
      packages: { ready: true, missing: [], incompatible: [] },
      probe: { ready: true, checks: [] },
    };
    mocks.apiClient.getRuntimeSetupStatus.mockResolvedValue(ready);
    await act(async () => root.render(<App />));
    await act(async () => findButton(container, "Settings").click());
    await act(async () => findButton(container, "Delete the voice model").click());

    expect(container.querySelector('[data-testid="setup-page"]')).toBeNull();
    expect(container.querySelector("#tab-btn-settings")?.getAttribute("aria-selected")).toBe("true");
    expect(container.querySelector('[data-testid="settings-models-status"]')?.textContent).toBe("unconfigured");
  });

  it("keeps every lazy tab's aria-controls target in the document", async () => {
    await act(async () => {
      root.render(<App />);
    });
    await act(async () => findButton(container, "Continue in library-only mode").click());

    for (const label of ["Create", "Library", "Settings"]) {
      const tab = findButton(container, label);
      const panelId = tab.getAttribute("aria-controls");
      expect(panelId).toBeTruthy();
      expect(container.querySelector(`#${panelId}`)).not.toBeNull();
    }
  });

  it("keeps the selected section when the sidebar is collapsed and restored", async () => {
    await act(async () => root.render(<App />));
    await act(async () => findButton(container, "Continue in library-only mode").click());
    await act(async () => findButton(container, "Library").click());
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Hide sidebar"]')!.click());
    expect(container.querySelector<HTMLElement>(".rail")?.hidden).toBe(true);
    expect(document.activeElement?.getAttribute("aria-label")).toBe("Show sidebar");
    expect(container.querySelector("#tab-btn-library")?.getAttribute("aria-selected")).toBe("true");
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Show sidebar"]')!.click());
    expect(container.querySelector<HTMLElement>(".rail")?.hidden).toBe(false);
    expect(document.activeElement?.id).toBe("tab-btn-library");
    expect(container.querySelector("#tab-btn-library")?.getAttribute("aria-selected")).toBe("true");
  });

  it.each([
    { width: 760, height: 900, compact: true },
    { width: 761, height: 900, compact: false },
    { width: 860, height: 900, compact: false },
    { width: 1280, height: 900, compact: false },
    { width: 1680, height: 900, compact: false },
    { width: 640, height: 540, compact: true },
    { width: 1280, height: 540, compact: true },
    { width: 1280, height: 541, compact: false },
  ])("preserves sidebar navigation at $width × $height", async ({ width, height, compact }) => {
    const browserWindow = window as unknown as HappyDOMWindow;
    const originalViewport = { width: window.innerWidth, height: window.innerHeight };
    const matchMedia = window.matchMedia.bind(window);
    const mediaSpy = vi.spyOn(window, "matchMedia").mockImplementation((query) => {
      const media = matchMedia(query);
      // Happy DOM evaluates comma-separated queries as AND instead of OR.
      Object.defineProperty(media, "matches", {
        get: () => query.split(",").some((part) => matchMedia(part.trim()).matches),
      });
      return media;
    });
    try {
      browserWindow.happyDOM.setViewport({ width, height });
      await act(async () => root.render(<App />));
      await act(async () => findButton(container, "Continue in library-only mode").click());
      expect(container.querySelector<HTMLElement>(".rail")?.hidden).toBe(compact);

      if (compact) {
        await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Show sidebar"]')!.click());
        expect(document.activeElement?.id).toBe("tab-btn-library");
      }
      await act(async () => findButton(container, "Library").click());
      expect(container.querySelector("#tab-btn-library")?.getAttribute("aria-selected")).toBe("true");
      expect(container.querySelector<HTMLElement>(".rail")?.hidden).toBe(compact);
      if (compact) {
        expect(document.activeElement?.id).toBe("workspace-heading");
      }
    } finally {
      await act(async () => browserWindow.happyDOM.setViewport(originalViewport));
      mediaSpy.mockRestore();
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

  it("acknowledges a finished conversion with a notice that opens the library", async () => {
    await act(async () => root.render(<App />));
    await act(async () => findButton(container, "Continue in library-only mode").click());
    await act(async () => findButton(container, "Create").click());
    mocks.conversionUiState = "running";
    await act(async () => root.render(<App />));
    expect(container.querySelector(".completion-notice")).toBeNull();
    // Running no longer pulls the user away from the page they are on.
    expect(container.querySelector("#tab-btn-convert")?.getAttribute("aria-selected")).toBe("true");

    mocks.conversionUiState = "completed";
    await act(async () => root.render(<App />));
    const notice = container.querySelector(".completion-notice.is-completed");
    expect(notice?.textContent).toContain("Your audiobook is ready");
    expect(mocks.apiClient.getActiveConversion).toHaveBeenCalledOnce();
    expect(container.querySelector("#tab-btn-convert")?.getAttribute("aria-selected")).toBe("true");
    await act(async () => findButton(notice as HTMLElement, "Open").click());
    expect(container.querySelector(".completion-notice")).toBeNull();
    expect(container.querySelector("#tab-btn-library")?.getAttribute("aria-selected")).toBe("true");
  });

  it.each([[true, "Updated in your library."], [false, "Added to your library."]])("says a finished job with repair %s was %s", async (repair, copy) => {
    // Book 9 is not on the loaded shelf: the job, not the shelf, says whether it was repaired.
    mocks.apiClient.getActiveConversion.mockResolvedValueOnce({ active: false, job: { id: "job", status: "completed", book_id: 9, repair } });
    await act(async () => root.render(<App />));
    await act(async () => findButton(container, "Continue in library-only mode").click());
    mocks.conversionUiState = "running";
    await act(async () => root.render(<App />));
    mocks.conversionUiState = "completed";
    await act(async () => root.render(<App />));
    expect(container.querySelector(".completion-notice.is-completed")?.textContent).toContain(copy);
  });

  it("opens a finished book that is not on the loaded shelf", async () => {
    mocks.apiClient.getActiveConversion.mockResolvedValueOnce({ active: false, job: { id: "job", status: "completed", book_id: 9, repair: true } });
    await act(async () => root.render(<App />));
    await act(async () => findButton(container, "Continue in library-only mode").click());
    await act(async () => findButton(container, "Create").click());
    mocks.conversionUiState = "running";
    await act(async () => root.render(<App />));
    mocks.conversionUiState = "completed";
    await act(async () => root.render(<App />));
    await act(async () => findButton(container.querySelector<HTMLElement>(".completion-notice")!, "Open").click());
    expect(mocks.selectBookById).toHaveBeenCalledExactlyOnceWith(9);
    expect(container.querySelector("#tab-library")?.getAttribute("data-album-open")).toBe("true");
  });

  it("reports a failed conversion with a shortcut to its details", async () => {
    await act(async () => root.render(<App />));
    await act(async () => findButton(container, "Continue in library-only mode").click());
    mocks.conversionUiState = "running";
    await act(async () => root.render(<App />));
    mocks.conversionUiState = "failed";
    await act(async () => root.render(<App />));
    expect(container.querySelector(".completion-notice.is-failed")?.textContent).toContain("failed");
    await act(async () => findButton(container.querySelector<HTMLElement>(".completion-notice")!, "Details").click());
    expect(container.querySelector('[role="dialog"][aria-label="Conversion details"]')).not.toBeNull();
  });

  it("keeps a newer completion notice attached to its own job when an older lookup resolves late", async () => {
    let resolveOld!: (value: Awaited<ReturnType<typeof mocks.apiClient.getActiveConversion>>) => void;
    mocks.apiClient.getActiveConversion.mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }))
      .mockResolvedValueOnce({ active: false, job: { id: "B", status: "completed", book_id: 8, repair: false } });
    await act(async () => root.render(<App />));
    await act(async () => findButton(container, "Continue in library-only mode").click());
    for (const id of ["A", "B"]) {
      mocks.activeJobId = id;
      mocks.conversionUiState = "running";
      await act(async () => root.render(<App />));
      mocks.activeJobId = null;
      mocks.conversionUiState = "completed";
      await act(async () => root.render(<App />));
    }
    await act(async () => resolveOld({ active: false, job: { id: "A", status: "completed", book_id: 7, repair: false } }));
    await act(async () => findButton(container.querySelector<HTMLElement>(".completion-notice")!, "Open").click());
    expect(mocks.selectBookById).toHaveBeenCalledExactlyOnceWith(8);
  });

  it("dismisses a completion ten seconds after publication even if its book lookup settles later", async () => {
    vi.useFakeTimers();
    let resolveLookup!: (value: Awaited<ReturnType<typeof mocks.apiClient.getActiveConversion>>) => void;
    mocks.apiClient.getActiveConversion.mockImplementationOnce(() => new Promise((resolve) => { resolveLookup = resolve; }));
    await act(async () => root.render(<App />));
    await act(async () => findButton(container, "Continue in library-only mode").click());
    mocks.conversionUiState = "running";
    await act(async () => root.render(<App />));
    mocks.conversionUiState = "completed";
    await act(async () => root.render(<App />));
    await act(async () => vi.advanceTimersByTimeAsync(7_000));
    await act(async () => resolveLookup({ active: false, job: { id: "job", status: "completed", book_id: 7, repair: false } }));
    expect(container.querySelector(".completion-notice")).not.toBeNull();
    await act(async () => vi.advanceTimersByTimeAsync(3_000));
    expect(container.querySelector(".completion-notice")).toBeNull();
  });

  it("returns to the shelf from an album with either keyboard or mouse tab selection", async () => {
    await act(async () => root.render(<App />));
    await act(async () => findButton(container, "Continue in library-only mode").click());
    for (const method of ["keyboard", "mouse"]) {
      await act(async () => findButton(container, "Open album").click());
      expect(container.querySelector("#tab-library")?.getAttribute("data-album-open")).toBe("true");
      await act(async () => findButton(container, "Create").click());
      await act(async () => {
        if (method === "keyboard") container.querySelector("#tab-btn-convert")!.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true }));
        else container.querySelector<HTMLButtonElement>("#tab-btn-library")!.click();
      });
      expect(container.querySelector("#tab-library")?.getAttribute("data-album-open")).toBe("false");
    }
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

  it("focuses visible content when a conversion shortcut opens Settings with the sidebar hidden", async () => {
    await act(async () => {
      root.render(<App />);
    });
    await act(async () => findButton(container, "Continue in library-only mode").click());

    await act(async () => findButton(container, "Create").click());
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Hide sidebar"]')!.click());
    await act(async () => findButton(container, "Open Settings").click());

    expect(document.activeElement?.id).toBe("workspace-heading");
    expect(document.activeElement?.textContent).toBe("Settings");
    expect(container.querySelector<HTMLElement>(".rail")?.hidden).toBe(true);
  });

  it("returns focus to the sidebar toggle on Escape", async () => {
    await act(async () => root.render(<App />));
    await act(async () => findButton(container, "Continue in library-only mode").click());
    const tab = container.querySelector<HTMLButtonElement>("#tab-btn-library")!;
    tab.focus();
    await act(async () => tab.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(document.activeElement?.getAttribute("aria-label")).toBe("Show sidebar");
    expect(container.querySelector<HTMLElement>(".rail")?.hidden).toBe(true);
  });

  it("keeps the current workspace mounted while requiring fresh readiness after reconnect", async () => {
    const ready = { ...setupStatus, configured: true, state: "ready" as const, ready: true };
    mocks.apiClient.getRuntimeSetupStatus.mockResolvedValue(ready);
    await act(async () => root.render(<App />));
    await act(async () => findButton(container, "Create").click());
    const workspace = container.querySelector(".app-frame");
    let resolveStatus!: (status: RuntimeSetupStatusResponse) => void;
    mocks.backend.apiClient = { ...mocks.apiClient, getRuntimeSetupStatus: vi.fn(() => new Promise<RuntimeSetupStatusResponse>((resolve) => { resolveStatus = resolve; })) };
    await act(async () => root.render(<App />));
    expect(container.querySelector(".app-frame")).toBe(workspace);
    expect(container.querySelector("#tab-btn-convert")?.getAttribute("aria-selected")).toBe("true");
    expect(mocks.conversionPanelProps?.runtimeState).toBe("checking");
    await act(async () => resolveStatus(ready));
    // Verified for the new client: a status with no voice model cannot convert.
    expect(mocks.conversionPanelProps?.runtimeState).toBe("unavailable");
    expect(container.querySelector(".app-frame")).toBe(workspace);
  });
});
