// @vitest-environment happy-dom

import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { invalidateRuntimeBootstrap } from "./app/runtimeBootstrapCache";
import { createQueueItem } from "./features/conversion/conversionPanelModel";
import { readPersistedSettings } from "./features/conversion/conversionOptionsShared";
import { useConversionOptions } from "./features/conversion/useConversionOptions";
import { useConversionSubmitPayload } from "./features/conversion/useConversionSubmitPayload";
import type { UseConversionPanelStateArgs } from "./features/conversion/useConversionPanelState";
import { useConversionQueueController } from "./features/conversion/useConversionQueueController";
import { useConversionQueueRunner } from "./features/conversion/useConversionQueueRunner";
import type { ConversionJobController } from "./features/conversion/useConversionJob";
import type { RuntimeSetupStatusResponse } from "./types/api";

const mocks = vi.hoisted(() => {
  const apiClient = {
    getRuntimeSetupStatus: vi.fn(),
    adoptRuntimeSetup: vi.fn(),
    getModelOptions: vi.fn(),
    getSystemCapabilities: vi.fn(),
    getVoiceOptions: vi.fn(),
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
    startConversion: vi.fn(),
    waitForTerminalState: vi.fn(),
    workspace: null as null | { queue: ReturnType<typeof useConversionQueueController>; runner: ReturnType<typeof useConversionQueueRunner>; options: ReturnType<typeof useConversionOptions>; paste: string; setPaste: (text: string) => void },
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
  useRuntimeIdentity: () => ({ ttsLabel: "Library only", cleanerLabel: "—", ttsReady: false }),
}));
vi.mock("./app/useGlobalShortcuts", () => ({
  useGlobalShortcuts: vi.fn(),
}));
vi.mock("./features/library/useLibrary", () => ({
  useLibrary: () => ({ items: [], indexedCount: 0, refreshLibrary: vi.fn(async () => undefined) }),
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
  ConversionPanel: (props: UseConversionPanelStateArgs) => {
    const { activeTab, onOpenRuntimeSetup } = props;
    const options = useConversionOptions({ ...props, conversionMode: "audiobook" });
    const queue = useConversionQueueController({ apiClient: null });
    const [paste, setPaste] = useState("");
    const payload = useConversionSubmitPayload({
      optionState: options, conversionMode: "audiobook", sourcePath: queue.sourcePath,
      titleOverride: queue.titleOverride, authorOverride: queue.authorOverride,
    });
    const runner = useConversionQueueRunner({
      runtimeOptionsReady: !options.loading && !options.error,
      ...queue,
      apiClient: mocks.apiClient as never,
      conversionJob: { refreshActiveConversion: async () => {}, startConversion: mocks.startConversion, waitForTerminalState: mocks.waitForTerminalState } as unknown as ConversionJobController,
      buildStartPayload: payload.buildStartPayload,
    });
    mocks.workspace = { queue, runner, options, paste, setPaste };
    return activeTab === "config" ? <button type="button" onClick={onOpenRuntimeSetup}>Runtime setup</button> : null;
  },
}));
vi.mock("./features/setup/setupBridge", () => ({
  getRuntimeSetupEnvironment: () => mocks.setupBridge.environment(),
  getRuntimeSetupRunnerStatus: () => mocks.setupBridge.status(),
  listenRuntimeSetupProgress: (onProgress: (event: unknown) => void) => mocks.setupBridge.listen(onProgress),
  startRuntimeSetup: (request: unknown) => mocks.setupBridge.start(request),
  cancelRuntimeSetup: () => mocks.setupBridge.cancel(),
  restartOrpheusBackend: () => mocks.setupBridge.restart(),
}));
vi.mock("./features/library/LibraryPanel", () => ({ LibraryPanel: () => null }));
vi.mock("./features/library/LibraryMaintenanceSection", () => ({ LibraryMaintenanceSection: () => null }));
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
    invalidateRuntimeBootstrap(mocks.apiClient as never);
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    mocks.apiClient.getRuntimeSetupStatus.mockResolvedValue(readyStatus);
    mocks.apiClient.getModelOptions.mockResolvedValue({
      tts_engines: [{ id: "kokoro-mlx", name: "MLX", voices: [], supported_quantizations: ["bf16"] }],
      cleaner_models: [], api_cleaner_models: [],
      cleaner_backend_options: [{ name: "mlx", display_name: "Local MLX", is_local: true, requires_api_key: false,
        models: ["mlx-community/Llama-3.2-3B-Instruct-4bit", "mlx-community/Llama-3.2-1B-Instruct-4bit"] }],
    });
    mocks.apiClient.getSystemCapabilities.mockResolvedValue({
      default_tts_engine: "kokoro-mlx", default_tts_quantization: "bf16",
      cleaner_runtime_availability: { mlx: true }, mlx_runtime_available: true,
      runtime_probe: { state: "fresh", refresh_in_progress: false },
    });
    mocks.apiClient.getVoiceOptions.mockResolvedValue({ voices: [{ id: "af_heart", name: "Heart" }] });
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
    window.localStorage.clear();
  });

  it("preserves queued metadata, pasted drafts, and batch execution through model management", async () => {
    let finishFirst!: (event: { jobId: string; status: "completed" }) => void;
    mocks.startConversion.mockResolvedValue({ ok: true, jobId: "job" });
    mocks.waitForTerminalState.mockImplementationOnce(() => new Promise((resolve) => { finishFirst = resolve; }))
      .mockResolvedValue({ jobId: "job", status: "completed" });
    await act(async () => root.render(<App />));
    await act(async () => findButton(container, "Create").click());
    const sources = [createQueueItem("/tmp/first.txt", "audiobook"), createQueueItem("/tmp/second.txt", "audiobook")]
      .map((item) => ({ ...item, status: "ready" as const }));
    await act(async () => mocks.workspace!.queue.addQueueItems(sources, { selectFirst: true, skipPreflight: true }));
    await act(async () => {
      mocks.workspace!.queue.syncSelectedQueueEdits({ title: "Edited title", author: "Edited author" });
      mocks.workspace!.setPaste("Next pasted draft");
    });
    await act(async () => { void mocks.workspace!.runner.onRunQueue(); });
    await act(async () => findButton(container, "Settings").click());
    expect(container.querySelector(".setup-panel.is-embedded")).not.toBeNull();
    await act(async () => finishFirst({ jobId: "job", status: "completed" }));
    expect(mocks.startConversion).toHaveBeenCalledTimes(2);
    await act(async () => findButton(container, "Create").click());
    expect(mocks.workspace!.queue.queueItems).toHaveLength(2);
    expect(mocks.workspace!.queue.queueItems[0]).toMatchObject({ title: "Edited title", author: "Edited author", status: "done" });
    expect(mocks.workspace!.paste).toBe("Next pasted draft");
  });

  it("uses the saved cleaner for the next conversion and option edits while preserving queued metadata and pasted drafts", async () => {
    const groups = ["tts-mlx", "cleaner-mlx", "cleaner-mlx-small"];
    const status: RuntimeSetupStatusResponse = {
      ...readyStatus, selected_profile: "mlx",
      selected_model_groups: groups.slice(0, 2).map((id) => ({ id, ready: true, runtime_ready: true, assets_ready: true, assets_reason: null })),
      profiles: [{ id: "mlx", label: "Apple Silicon (MLX)", description: "Local", compatible: true, reason: null, model_groups: groups, risk: null }],
      installed_runtime: { profile: "mlx", ready: true, runtime_available: true,
        model_groups: groups.map((id) => ({ id, ready: true, runtime_ready: true, assets_ready: true })) },
    };
    mocks.apiClient.getRuntimeSetupStatus.mockResolvedValue(status);
    const savedStatus = { ...status,
      selected_model_groups: status.selected_model_groups.map((group) => group.id === "cleaner-mlx" ? { ...group, id: "cleaner-mlx-small" } : group),
    };
    mocks.apiClient.adoptRuntimeSetup.mockImplementation(async () => {
      mocks.apiClient.getRuntimeSetupStatus.mockResolvedValue(savedStatus);
      return savedStatus;
    });
    mocks.startConversion.mockResolvedValue({ ok: true, jobId: "job" });
    mocks.waitForTerminalState.mockResolvedValue({ jobId: "job", status: "completed" });
    await act(async () => root.render(<App />));
    await act(async () => findButton(container, "Create").click());
    expect(mocks.workspace!.options.cleanerModel).toBe("mlx-community/Llama-3.2-3B-Instruct-4bit");
    const item = { ...createQueueItem("/tmp/queued.txt", "audiobook"), status: "ready" as const };
    await act(async () => mocks.workspace!.queue.addQueueItems([item], { selectFirst: true, skipPreflight: true }));
    await act(async () => {
      mocks.workspace!.queue.syncSelectedQueueEdits({ title: "Edited title", author: "Edited author" });
      mocks.workspace!.setPaste("Next pasted draft");
    });
    await act(async () => findButton(container, "Settings").click());
    const smallCleaner = [...container.querySelectorAll<HTMLInputElement>("input")]
      .find((input) => input.parentElement?.textContent?.includes("Llama 3.2 · 1B"))!;
    await act(async () => smallCleaner.click());
    await act(async () => findButton(container, "Save runtime settings").click());
    expect(mocks.apiClient.adoptRuntimeSetup).toHaveBeenCalledWith(expect.objectContaining({ model_groups: ["tts-mlx", "cleaner-mlx-small"] }), expect.anything());
    expect(readPersistedSettings().cleaner_model).toBe("mlx-community/Llama-3.2-1B-Instruct-4bit");
    await act(async () => findButton(container, "Create").click());
    expect(mocks.workspace!.queue.queueItems).toHaveLength(1);
    expect(mocks.workspace!.queue.queueItems[0]).toMatchObject({ title: "Edited title", author: "Edited author", status: "ready" });
    expect(mocks.workspace!.paste).toBe("Next pasted draft");
    await act(async () => { void mocks.workspace!.runner.onRunQueue(); });
    expect(mocks.startConversion).toHaveBeenCalledWith(expect.objectContaining({
      title: "Edited title", author: "Edited author",
      runtime_options: expect.objectContaining({ cleaner_backend: "mlx", cleaner_model: "mlx-community/Llama-3.2-1B-Instruct-4bit" }),
    }), expect.objectContaining({ signal: expect.any(AbortSignal) }));
    await act(async () => mocks.workspace!.options.setSpeed("1.25"));
    expect(readPersistedSettings()).toMatchObject({ cleaner_model: "mlx-community/Llama-3.2-1B-Instruct-4bit", preferred_cleaner_model: "mlx-community/Llama-3.2-1B-Instruct-4bit", speed: 1.25 });
    expect(mocks.apiClient.getModelOptions.mock.calls.length).toBeGreaterThan(1);
    expect(mocks.apiClient.getSystemCapabilities.mock.calls.length).toBeGreaterThan(1);
  });

  it("shows the setup wizard once the first status check finds no runtime", async () => {
    let resolveStatus!: (status: RuntimeSetupStatusResponse) => void;
    mocks.apiClient.getRuntimeSetupStatus.mockImplementationOnce(() => new Promise((resolve) => { resolveStatus = resolve; }));
    await act(async () => root.render(<App />));
    expect(container.querySelector(".setup-startup")).not.toBeNull();
    expect(container.querySelector("#setup-heading")).toBeNull();
    await act(async () => resolveStatus({ ...readyStatus, configured: false, ready: false, state: "unconfigured" }));
    expect(container.querySelector("#setup-heading")?.textContent).toBe("Welcome to Orpheus");
  });

  it("keeps the startup screen while a configured runtime is still being checked", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      mocks.apiClient.getRuntimeSetupStatus
        .mockResolvedValueOnce({ ...readyStatus, ready: false, state: "checking", checking: true })
        .mockResolvedValue(readyStatus);
      await act(async () => root.render(<App />));
      expect(container.querySelector(".setup-startup")).not.toBeNull();
      expect(container.querySelector("#setup-heading")).toBeNull();
      await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
      expect(container.querySelector(".setup-startup")).toBeNull();
      expect(container.querySelector("#setup-heading")).toBeNull();
      expect(mocks.apiClient.getRuntimeSetupStatus.mock.calls.length).toBeGreaterThanOrEqual(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("opens the dashboard when only unselected cleaners are still being checked", async () => {
    mocks.apiClient.getRuntimeSetupStatus.mockResolvedValue({
      ...readyStatus, selected_profile: "mlx", checking: true,
      selected_model_groups: [{ id: "tts-mlx", ready: true, runtime_ready: true, assets_ready: true, assets_reason: null }],
      probe: { ready: true, checks: [{ id: "tts-mlx", ready: true }] },
      installed_runtime: { profile: "mlx", ready: true, runtime_available: true, checking: true, model_groups: [
        { id: "tts-mlx", ready: true, runtime_ready: true, assets_ready: true },
        { id: "cleaner-mlx", ready: false, runtime_ready: false, assets_ready: true, runtime_state: "timeout" },
      ] },
    });
    await act(async () => root.render(<App />));
    expect(container.querySelector(".setup-startup")).toBeNull();
    expect(container.querySelector("#setup-heading")).toBeNull();
    expect(findButton(container, "Settings")).toBeDefined();
  });

  it("keeps the dashboard when the backend is lost after a verified launch", async () => {
    mocks.apiClient.getRuntimeSetupStatus.mockResolvedValue(readyStatus);
    await act(async () => root.render(<App />));
    expect(container.querySelector("#setup-heading")).toBeNull();
    const backend = mocks.backend as { apiClient: unknown; connectionState: string };
    const original = { apiClient: backend.apiClient, connectionState: backend.connectionState };
    try {
      // A recreated client pointing at a backend that is gone.
      backend.apiClient = { ...mocks.apiClient, getRuntimeSetupStatus: vi.fn().mockRejectedValue(new TypeError("Failed to fetch")) };
      backend.connectionState = "error";
      await act(async () => root.render(<App />));
      expect(container.querySelector("#setup-heading")).toBeNull();
      expect(container.querySelector(".setup-startup")).toBeNull();
      expect(container.querySelector('[role="status"][aria-label="Backend error"]')).not.toBeNull();
    } finally {
      backend.apiClient = original.apiClient;
      backend.connectionState = original.connectionState;
    }
  });

  it("keeps a returning user on the dashboard when the backend is gone before the first status", async () => {
    window.localStorage.setItem("orpheus.desktop.runtime-ready.v1", "ready");
    mocks.apiClient.getRuntimeSetupStatus.mockRejectedValue(new TypeError("Failed to fetch"));
    await act(async () => root.render(<App />));
    expect(container.querySelector("#setup-heading")).toBeNull();
    expect(container.querySelector(".setup-startup")).toBeNull();
    expect(container.querySelector(".runtime-check-notice")?.textContent).toContain("conversions are unavailable");
  });

  it("retains saved preferences on the dashboard while a returning user's probe is pending", async () => {
    const savedStatus: RuntimeSetupStatusResponse = {
      ...readyStatus, selected_profile: "mlx",
      selected_model_groups: [{ id: "tts-mlx", ready: true, runtime_ready: true, assets_ready: true, assets_reason: null }],
    };
    mocks.apiClient.getRuntimeSetupStatus.mockResolvedValueOnce(savedStatus);
    await act(async () => root.render(<App />));
    const savedPreferences = readPersistedSettings();
    await act(async () => root.unmount());
    root = createRoot(container);
    mocks.apiClient.getRuntimeSetupStatus.mockResolvedValue({
      ...savedStatus, ready: false, state: "checking", checking: true,
      packages: { ready: false, missing: [], incompatible: [] },
      probe: { ready: false, checks: [{ id: "tts-mlx", ready: false, state: "pending" }] },
      selected_model_groups: [{ id: "tts-mlx", ready: false, runtime_ready: false, assets_ready: false, assets_reason: null }],
    });
    await act(async () => root.render(<App />));
    // The earlier verified launch lets the returning user browse while the
    // probe re-verifies; conversions stay blocked until it confirms.
    expect(container.querySelector(".setup-startup")).toBeNull();
    expect(container.querySelector("#setup-heading")).toBeNull();
    expect(container.querySelector(".runtime-check-notice")?.textContent).toContain("conversions are unavailable");
    expect(readPersistedSettings()).toEqual(savedPreferences);
  });

  it("offers retry and library access when startup checks exhaust their polling limit", async () => {
    vi.useFakeTimers();
    try {
      mocks.apiClient.getRuntimeSetupStatus.mockResolvedValue({ ...readyStatus, ready: false, state: "checking", checking: true });
      await act(async () => root.render(<App />));
      await act(async () => { await vi.advanceTimersByTimeAsync(120_000); });
      expect(mocks.apiClient.getRuntimeSetupStatus).toHaveBeenCalledTimes(61);
      expect(container.querySelector('[role="alert"]')?.textContent).toContain("taking longer than expected");
      expect(container.querySelector(".setup-startup-spinner")).toBeNull();
      expect(findButton(container, "Use library only").disabled).toBe(false);
      mocks.apiClient.getRuntimeSetupStatus
        .mockResolvedValueOnce({ ...readyStatus, ready: false, state: "checking", checking: true })
        .mockResolvedValue(readyStatus);
      await act(async () => findButton(container, "Retry").click());
      expect(mocks.apiClient.getRuntimeSetupStatus).toHaveBeenCalledTimes(62);
      expect(container.querySelector(".setup-startup-spinner")).not.toBeNull();
      expect(container.querySelector('[role="alert"]')).toBeNull();
      await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
      expect(mocks.apiClient.getRuntimeSetupStatus).toHaveBeenCalledTimes(63);
      expect(container.querySelector(".setup-startup")).toBeNull();
      expect(findButton(container, "Settings")).toBeDefined();
    } finally {
      vi.useRealTimers();
    }
  });

  it("opens the library after startup checks exhaust their polling limit", async () => {
    vi.useFakeTimers();
    try {
      mocks.apiClient.getRuntimeSetupStatus.mockResolvedValue({ ...readyStatus, ready: false, state: "checking", checking: true });
      await act(async () => root.render(<App />));
      await act(async () => { await vi.advanceTimersByTimeAsync(120_000); });
      await act(async () => findButton(container, "Use library only").click());
      expect(container.querySelector(".setup-startup")).toBeNull();
      expect(container.querySelector("#setup-heading")).toBeNull();
      expect(findButton(container, "Library")).toBeDefined();
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows real model management inside Settings without leaving the workspace", async () => {
    await act(async () => root.render(<App />));
    expect(container.querySelector(".setup-frame")).toBeNull();

    await act(async () => findButton(container, "Settings").click());

    const models = container.querySelector<HTMLElement>(".setup-panel.is-embedded");
    expect(models).not.toBeNull();
    expect(models?.closest(".settings-page")).not.toBeNull();
    expect(container.querySelector(".setup-frame")).toBeNull();
    expect(container.querySelector("#setup-heading")?.textContent).toBe("Voice & models");
    // The embedded section never pulls focus away from where the user is.
    expect(document.activeElement?.id).not.toBe("setup-heading");

    await act(async () => findButton(container, "Library").click());
    expect(container.querySelector<HTMLElement>(".setup-panel.is-embedded")?.hidden).toBe(true);
  });

  it("keeps Settings usable when a runtime check fails without changing the installed profile", async () => {
    mocks.apiClient.getRuntimeSetupStatus
      .mockResolvedValueOnce(readyStatus)
      .mockRejectedValueOnce(new Error("Runtime check unavailable"));
    await act(async () => root.render(<App />));
    await act(async () => findButton(container, "Settings").click());
    await act(async () => findButton(container, "Check runtime").click());
    expect(container.textContent).toContain("Runtime check unavailable");
    expect(container.querySelector(".setup-frame")).toBeNull();
    await act(async () => findButton(container, "Library").click());
    expect(container.querySelector("#tab-btn-library")?.getAttribute("aria-selected")).toBe("true");
    expect(mocks.setupBridge.start).not.toHaveBeenCalled();
  });
});
