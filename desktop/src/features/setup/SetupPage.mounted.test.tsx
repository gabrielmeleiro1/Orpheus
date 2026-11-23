// @vitest-environment happy-dom

import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import type { RuntimeModelDownloadStatus, RuntimeSetupStatusResponse } from "../../types/api";
import { SetupPage, type SetupPageBridge } from "./SetupPage";
import type { RuntimeSetupEnvironment, RuntimeSetupRunnerStatus } from "./setupBridge";
import type { SetupProgressEvent } from "./setupModel";

const setupStatus: RuntimeSetupStatusResponse = {
  schema_version: 1, configured: false, state: "unconfigured", state_error: null,
  selected_profile: null, selected_model_groups: [],
  packages: { ready: false, missing: [], incompatible: [] },
  probe: { ready: false, checks: [] }, ready: false, restart_required: false,
  profiles: [
    { id: "base", label: "Library only", description: "Library.", compatible: true, reason: null, model_groups: [] },
    { id: "mlx", label: "Apple Silicon (MLX)", description: "Local.", compatible: true, reason: null, model_groups: ["tts-mlx", "cleaner-mlx"] },
    { id: "cuda", label: "NVIDIA CUDA", description: "GPU.", compatible: false, reason: "Unavailable.", model_groups: [] },
  ], backend: { instance_id: "one", status: "running" },
};

function bridge(overrides: Partial<SetupPageBridge> = {}): SetupPageBridge {
  return {
    environment: vi.fn(async (): Promise<RuntimeSetupEnvironment> => ({ available: true, mode: "source_checkout", reason: "" })),
    start: vi.fn(async (): Promise<RuntimeSetupRunnerStatus> => ({ lifecycle: "running", profile: "base", modelGroups: [], stage: null, message: null, percent: null, restartRequired: false, error: null })),
    status: vi.fn(async (): Promise<RuntimeSetupRunnerStatus> => ({ lifecycle: "idle", profile: null, modelGroups: [], stage: null, message: null, percent: null, restartRequired: false, error: null })),
    cancel: vi.fn(async (): Promise<RuntimeSetupRunnerStatus> => ({ lifecycle: "cancelled", profile: "base", modelGroups: [], stage: null, message: null, percent: null, restartRequired: false, error: "cancelled" })),
    restart: vi.fn(async () => undefined),
    listen: vi.fn(async () => vi.fn()),
    ...overrides,
  };
}

/** How App mounts the page inside Settings. */
const settingsMount = { settingsEntry: true, embedded: true } as const;

function findButton(container: HTMLElement, text: string): HTMLButtonElement {
  const button = [...container.querySelectorAll("button")].find((item) => item.textContent?.includes(text));
  if (!button) throw new Error(`missing button: ${text}`);
  return button;
}

describe("mounted SetupPage", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.restoreAllMocks();
  });

  /** Renders the page; the runtime/model choices are opened unless `collapsed` is set, as most tests drive them. */
  async function renderPage(runtimeBridge: SetupPageBridge, props: Partial<Parameters<typeof SetupPage>[0]> = {}, { collapsed = false } = {}) {
    const api = props.apiClient ?? { getRuntimeSetupStatus: vi.fn(async () => setupStatus) } as unknown as ApiClient;
    await act(async () => {
      root.render(<SetupPage apiClient={api} initialStatus={setupStatus} initialError={null} onReady={vi.fn()} onContinueLibraryOnly={vi.fn()} onReconnectBackend={vi.fn()} bridge={runtimeBridge} {...props} />);
    });
    const toggle = container.querySelector<HTMLButtonElement>('.setup-customize-toggle[aria-expanded="false"]');
    if (toggle && !collapsed) await act(async () => toggle.click());
    return api;
  }

  it("keeps first run to a summary until the choices are opened", async () => {
    await renderPage(packagedSettingsBridge(), { initialStatus: cudaCatalogStatus() }, { collapsed: true });
    const toggle = findButton(container, "Customize");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(container.querySelector('input[name="setup-cleaner"]')).toBeNull();
    expect(container.querySelector(".setup-plan")?.textContent).toContain("Recommended");
    expect(container.querySelector(".setup-plan-detail")?.textContent).toBe("Kokoro TTS + Qwen 2.5 · 1.5B · 4-bit · 3.6 GB download");
    expect(container.querySelector<HTMLButtonElement>(".setup-primary")!.textContent).toContain("Install");
    await act(async () => toggle.click());
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(document.getElementById(toggle.getAttribute("aria-controls")!)).not.toBeNull();
    expect(container.querySelectorAll('input[name="setup-cleaner"]')).toHaveLength(3);
  });

  function readySettings(cleanerReady = true): RuntimeSetupStatusResponse {
    return {
      ...setupStatus, configured: true, ready: true, state: "ready", selected_profile: "mlx",
      selected_model_groups: [{ id: "tts-mlx", runtime_ready: true, assets_ready: true, assets_reason: null, ready: true }],
      installed_runtime: { profile: "mlx", runtime_available: true, ready: true, model_groups: [
        { id: "tts-mlx", runtime_ready: true, assets_ready: true, ready: true },
        { id: "cleaner-mlx", runtime_ready: true, assets_ready: cleanerReady, ready: cleanerReady },
      ] },
    };
  }

  const packagedSettingsBridge = () => bridge({ environment: vi.fn(async (): Promise<RuntimeSetupEnvironment> => ({ available: false, mode: "packaged", reason: "Bundled runtime." })) });

  it("does not wait on an unselected cleaner that timed out", async () => {
    const status = readySettings();
    status.installed_runtime!.checking = true;
    status.installed_runtime!.model_groups[1] = {
      ...status.installed_runtime!.model_groups[1], ready: false, runtime_ready: false, runtime_state: "timeout",
    };
    await renderPage(packagedSettingsBridge(), { initialStatus: status, ...settingsMount });
    expect(container.textContent).not.toContain("Checking your hardware…");
    // Settings has nothing to confirm while the saved selection is unchanged.
    expect(container.querySelector(".setup-primary")).toBeNull();
    const cleaner = [...container.querySelectorAll<HTMLInputElement>("input")]
      .find((input) => input.parentElement?.textContent?.includes("Llama 3.2 · 3B"))!;
    await act(async () => cleaner.click());
    expect(container.querySelector<HTMLButtonElement>(".setup-primary")!.disabled).toBe(true);
    expect(container.textContent).toContain("Checking your hardware…");
    const noCleaner = [...container.querySelectorAll<HTMLInputElement>("input")]
      .find((input) => input.parentElement?.textContent?.includes("No local cleaner"))!;
    await act(async () => noCleaner.click());
    expect(container.textContent).not.toContain("Checking your hardware…");
    expect(container.querySelector(".setup-primary")).toBeNull();
  });

  function cudaCatalogStatus(): RuntimeSetupStatusResponse {
    const groups = ["tts-cpu-cuda", "cleaner-cuda", "cleaner-cuda-large"];
    return {
      ...setupStatus, recommended_profile: "cuda",
      profiles: setupStatus.profiles.map((profile) => profile.id === "cuda" ? { ...profile, compatible: true, reason: null, model_groups: groups } : profile),
      installed_runtime: { profile: "cuda", runtime_available: true, ready: false, model_groups: groups.map((id) => ({ id, runtime_ready: true, assets_ready: false, ready: false })) },
    };
  }

  it("shows checking hardware and re-polls a timed-out runtime before enabling Install", async () => {
    vi.useFakeTimers();
    try {
      const available = cudaCatalogStatus();
      const pending = { ...available, installed_runtime: Object.assign({}, available.installed_runtime, {
        checking: true, runtime_available: false,
        model_groups: available.installed_runtime!.model_groups.map((group) => ({ ...group, runtime_ready: false, runtime_state: "timeout", runtime_reason: null })),
      }) };
      const getStatus = vi.fn(async () => available);
      await renderPage(packagedSettingsBridge(), { initialStatus: pending, apiClient: { getRuntimeSetupStatus: getStatus } as unknown as ApiClient });
      expect(container.textContent).toContain("Checking your hardware…");
      expect(container.textContent).not.toContain("could not verify the installed runtime");
      expect(container.textContent).not.toContain("This runtime is unavailable");
      expect(container.querySelector<HTMLButtonElement>(".setup-primary")!.textContent).toContain("Checking");
      await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
      expect(getStatus).toHaveBeenCalledOnce();
      expect(container.textContent).not.toContain("Checking your hardware…");
      expect(findButton(container, "Install").disabled).toBe(false);
      await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
      expect(getStatus).toHaveBeenCalledOnce();
    } finally {
      vi.useRealTimers();
    }
  });

  it("bounds automatic hardware checks and lets the user check again", async () => {
    vi.useFakeTimers();
    try {
      const available = cudaCatalogStatus();
      const pending = { ...available, checking: true, installed_runtime: {
        ...available.installed_runtime!, checking: true, runtime_available: false,
        model_groups: available.installed_runtime!.model_groups.map((group) => ({ ...group, runtime_ready: false, runtime_state: "timeout" as const })),
      } };
      const getStatus = vi.fn(async () => pending);
      await renderPage(packagedSettingsBridge(), { initialStatus: pending, apiClient: { getRuntimeSetupStatus: getStatus } as unknown as ApiClient });
      await act(async () => { await vi.advanceTimersByTimeAsync(360_000); });
      expect(getStatus).toHaveBeenCalledTimes(120);
      await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
      expect(getStatus).toHaveBeenCalledTimes(120);
      await act(async () => findButton(container, "Check again").click());
      expect(getStatus).toHaveBeenCalledTimes(121);
    } finally {
      vi.useRealTimers();
    }
  });

  it("disables Install with the definitive runtime failure reason", async () => {
    const unavailable = cudaCatalogStatus();
    unavailable.installed_runtime = Object.assign({}, unavailable.installed_runtime, {
      runtime_available: false,
      model_groups: unavailable.installed_runtime!.model_groups.map((group) => ({ ...group, runtime_ready: false, runtime_state: "unavailable", runtime_reason: "missing kokoro" })),
    });
    await renderPage(packagedSettingsBridge(), { initialStatus: unavailable });
    expect(findButton(container, "Install").disabled).toBe(true);
    expect(container.textContent).toContain("missing kokoro");
    expect(container.textContent).not.toContain("Checking your hardware…");
  });

  it.each([false, true])("allows CPU onboarding with disabled cleaners (assets ready: %s)", async (ready) => {
    const initial = cudaCatalogStatus();
    initial.profiles = initial.profiles.map((profile) => profile.id === "cuda" ? {
      ...profile, device: "cpu", group_availability: Object.fromEntries(["cleaner-cuda", "cleaner-cuda-large"].map((id) => [id, {
        available: false, unavailable_reason: "Needs an NVIDIA GPU with a CUDA 12.6 driver",
      }])),
    } : profile);
    initial.installed_runtime!.device = "cpu";
    initial.installed_runtime!.ready = ready;
    initial.installed_runtime!.model_groups.forEach((group) => {
      group.runtime_ready = group.id === "tts-cpu-cuda";
      group.assets_ready = ready;
      group.ready = ready && group.runtime_ready;
    });
    const adopt = vi.fn(async () => ({ ...initial, configured: true, ready: true, state: "ready" }));
    const start = vi.fn(async () => ({ lifecycle: "running", profile: "cuda", model_groups: ["tts-cpu-cuda"] }));
    await renderPage(packagedSettingsBridge(), { initialStatus: initial, apiClient: { adoptRuntimeSetup: adopt, startRuntimeModelDownload: start } as unknown as ApiClient });
    expect(container.textContent).toContain("NVIDIA CUDA — running on CPU");
    await act(async () => container.querySelector<HTMLButtonElement>(".setup-plan .setup-tip")!.click());
    expect(document.querySelector('[role="tooltip"]')?.textContent).toContain("Basic cleanup is used instead");
    expect(container.textContent).not.toContain("could not verify the installed runtime");
    const cleaners = [...container.querySelectorAll<HTMLInputElement>('input[name="setup-cleaner"]')];
    expect(cleaners[0].checked).toBe(true);
    expect(cleaners.slice(1).every((input) => input.disabled && !input.checked)).toBe(true);
    expect(container.textContent).toContain("Needs an NVIDIA GPU with a CUDA 12.6 driver");
    const primary = container.querySelector<HTMLButtonElement>(".setup-primary")!;
    expect(primary.disabled).toBe(false);
    await act(async () => primary.click());
    expect(ready ? adopt : start).toHaveBeenCalledWith(expect.objectContaining({ profile: "cuda", model_groups: ["tts-cpu-cuda"] }), expect.anything());
    if (ready) {
      const configured: RuntimeSetupStatusResponse = { ...initial, configured: true, ready: true, state: "ready", selected_profile: "cuda", selected_model_groups: [{ id: "tts-cpu-cuda", ready: true, runtime_ready: true, assets_ready: true, assets_reason: null }] };
      await renderPage(packagedSettingsBridge(), { initialStatus: configured, ...settingsMount, apiClient: { getRuntimeModelInventory: vi.fn(async () => ({ models: [] })) } as unknown as ApiClient });
      const managed = [...container.querySelectorAll<HTMLInputElement>('input[name="managed-cleaner"]')].filter((input) => input.closest("article"));
      expect(managed).toHaveLength(2);
      expect(managed.every((input) => input.disabled)).toBe(true);
      expect(container.textContent).toContain("Needs an NVIDIA GPU with a CUDA 12.6 driver");
    }
  });

  it("installs CUDA narration when the cleaner driver requirement is unmet", async () => {
    const initial = cudaCatalogStatus();
    const reason = "Needs an NVIDIA GPU with a CUDA 12.6 driver";
    initial.profiles = initial.profiles.map((profile) => profile.id === "cuda" ? {
      ...profile, device: "cuda", group_availability: Object.fromEntries(["cleaner-cuda", "cleaner-cuda-large"].map((id) => [id, {
        available: false, unavailable_reason: reason,
      }])),
    } : profile);
    initial.installed_runtime!.device = "cuda";
    initial.installed_runtime!.model_groups.forEach((group) => {
      group.runtime_ready = group.id === "tts-cpu-cuda";
    });
    const start = vi.fn(async () => ({ lifecycle: "running", profile: "cuda", model_groups: ["tts-cpu-cuda"] }));
    await renderPage(packagedSettingsBridge(), { initialStatus: initial, apiClient: { startRuntimeModelDownload: start } as unknown as ApiClient });
    const cleaners = [...container.querySelectorAll<HTMLInputElement>('input[name="setup-cleaner"]')];
    expect(cleaners[0].checked).toBe(true);
    expect(cleaners.slice(1).every((input) => input.disabled && !input.checked)).toBe(true);
    expect(container.textContent).toContain(reason);
    const primary = container.querySelector<HTMLButtonElement>(".setup-primary")!;
    expect(primary.disabled).toBe(false);
    expect(primary.textContent).toContain("Install");
    await act(async () => primary.click());
    expect(start).toHaveBeenCalledWith(expect.objectContaining({ profile: "cuda", model_groups: ["tts-cpu-cuda"] }), expect.anything());
  });

  it.each([false, true])("scrolls installation into view once, respecting reduced motion (%s)", async (reducedMotion) => {
    const scroll = vi.spyOn(Element.prototype, "scrollIntoView").mockImplementation(() => undefined);
    vi.spyOn(window, "matchMedia").mockReturnValue({ matches: reducedMotion, addEventListener: vi.fn(), removeEventListener: vi.fn() } as unknown as MediaQueryList);
    let onProgress!: (event: SetupProgressEvent) => void;
    const runtimeBridge = bridge({ listen: vi.fn(async (listener) => { onProgress = listener; return vi.fn(); }) });
    await renderPage(runtimeBridge);
    expect(scroll).not.toHaveBeenCalled();
    await act(async () => container.querySelector<HTMLButtonElement>(".setup-primary")!.click());
    expect(scroll).toHaveBeenCalledExactlyOnceWith({ behavior: reducedMotion ? "instant" : "smooth", block: "start" });
    expect(scroll.mock.instances[0]).toBe(container.querySelector(".setup-progress"));
    const bar = container.querySelector('[role="progressbar"]');
    expect(bar?.hasAttribute("aria-valuenow")).toBe(false);
    await act(async () => onProgress({ stage: "packages", message: "Installing", percent: 25 } as SetupProgressEvent));
    expect(scroll).toHaveBeenCalledTimes(1);
  });

  it("shows model and precision choices on the first page and downloads only the chosen pair", async () => {
    const start = vi.fn(async () => ({ lifecycle: "running" }));
    await renderPage(packagedSettingsBridge(), { initialStatus: cudaCatalogStatus(), apiClient: { startRuntimeModelDownload: start } as unknown as ApiClient });
    expect(container.querySelector(".setup-precision legend")?.textContent).toContain("Cleaner precision");
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Cleaner precision details"]')!.click());
    expect(document.querySelector('[role="tooltip"]')?.textContent).toContain("Quantization stores model weights");
    expect(container.querySelector<HTMLInputElement>('input[name="setup-cleaner"]:checked')?.parentElement?.textContent).toContain("1.5B");
    const small = [...container.querySelectorAll<HTMLInputElement>('input[name="setup-cleaner"]')].find((input) => input.parentElement?.textContent?.includes("3B"))!;
    await act(async () => { small.click(); container.querySelector<HTMLInputElement>('input[value="8bit"]')!.click(); });
    expect(container.querySelectorAll('input[name="setup-cleaner"]:checked')).toHaveLength(1);
    await act(async () => container.querySelector<HTMLButtonElement>('.setup-primary')!.click());
    expect(start).toHaveBeenCalledExactlyOnceWith({ profile: "cuda", model_groups: ["tts-cpu-cuda", "cleaner-cuda-large"], cleaner_cuda_quantization: "8bit" }, { timeoutMs: 15_000 });
  });

  it("saves a precision change with no downloads and never adopts every cached cleaner", async () => {
    const initial = cudaCatalogStatus();
    initial.installed_runtime!.ready = true;
    initial.installed_runtime!.model_groups.forEach((group) => { group.assets_ready = true; group.ready = true; });
    const adopt = vi.fn(async () => ({ ...initial, configured: true, ready: true, state: "ready" }));
    const start = vi.fn();
    await renderPage(packagedSettingsBridge(), { initialStatus: initial, apiClient: { adoptRuntimeSetup: adopt, startRuntimeModelDownload: start } as unknown as ApiClient });
    await act(async () => container.querySelector<HTMLInputElement>('input[value="fp16"]')!.click());
    await act(async () => findButton(container, "Continue").click());
    expect(adopt).toHaveBeenCalledExactlyOnceWith({ profile: "cuda", model_groups: ["tts-cpu-cuda", "cleaner-cuda"], cleaner_cuda_quantization: "fp16" }, { timeoutMs: 120_000 });
    expect(start).not.toHaveBeenCalled();
  });

  it("keeps the user's no-cleaner choice across parent status snapshots and while saving", async () => {
    const initialStatus = cudaCatalogStatus();
    initialStatus.installed_runtime!.ready = true;
    initialStatus.installed_runtime!.model_groups.forEach((group) => { group.assets_ready = true; group.ready = true; });
    let resolveSave!: (value: RuntimeSetupStatusResponse) => void;
    const adopt = vi.fn(() => new Promise<RuntimeSetupStatusResponse>((resolve) => { resolveSave = resolve; }));
    const apiClient = { adoptRuntimeSetup: adopt } as unknown as ApiClient;
    const runtimeBridge = packagedSettingsBridge();
    await renderPage(runtimeBridge, { initialStatus, apiClient });
    const voiceRow = container.querySelector<HTMLInputElement>('.setup-model-row input[type="checkbox"]')!.parentElement!;
    expect(voiceRow.querySelector(".setup-state.is-success")?.textContent).toBe("Downloaded");
    const noCleaner = () => container.querySelector<HTMLInputElement>('input[name="setup-cleaner"]')!;
    await act(async () => noCleaner().click());
    const refreshed = { ...initialStatus, installed_runtime: { ...initialStatus.installed_runtime! } };
    await renderPage(runtimeBridge, { initialStatus: refreshed, apiClient });
    expect(noCleaner().checked).toBe(true);
    await act(async () => findButton(container, "Continue").click());
    expect(adopt).toHaveBeenCalledExactlyOnceWith({ profile: "cuda", model_groups: ["tts-cpu-cuda"], cleaner_cuda_quantization: "4bit" }, { timeoutMs: 120_000 });
    const primary = container.querySelector<HTMLButtonElement>(".setup-primary")!;
    expect(primary.disabled).toBe(true);
    expect(primary.textContent).toContain("Continue…");
    expect(noCleaner().checked).toBe(true);
    expect(noCleaner().closest("fieldset")!.disabled).toBe(true);
    await act(async () => resolveSave({ ...initialStatus, configured: true, ready: true, state: "ready" }));
  });

  it.each(["client", "incompatible"])("resets a dirty choice when the %s changes", async (change) => {
    const initialStatus = cudaCatalogStatus();
    const apiClient = {} as ApiClient;
    const runtimeBridge = packagedSettingsBridge();
    await renderPage(runtimeBridge, { initialStatus, apiClient });
    await act(async () => container.querySelector<HTMLInputElement>('input[name="setup-cleaner"]')!.click());
    const next = change === "incompatible" ? { ...initialStatus, profiles: initialStatus.profiles.map((profile) => profile.id === "cuda" ? { ...profile, compatible: false } : profile) } : { ...initialStatus };
    await renderPage(runtimeBridge, { initialStatus: next, apiClient: change === "client" ? {} as ApiClient : apiClient });
    if (change === "client") {
      expect(container.querySelector<HTMLInputElement>('input[name="setup-cleaner"]:checked')!.parentElement!.textContent).toContain("1.5B");
    } else {
      expect(container.querySelector(".setup-plan-title")!.textContent).toContain("Library only");
    }
  });



  it.each(["base", "mlx"] as const)("edits a ready runtime in Settings and persists the exact %s selection", async (profile) => {
    const initialStatus = readySettings();
    let resolveSave!: (value: RuntimeSetupStatusResponse) => void;
    const adopt = vi.fn(() => new Promise<RuntimeSetupStatusResponse>((resolve) => { resolveSave = resolve; }));
    const onReady = vi.fn();
    const api = { adoptRuntimeSetup: adopt } as unknown as ApiClient;
    await renderPage(packagedSettingsBridge(), { initialStatus, apiClient: api, ...settingsMount, onReady });
    expect(container.querySelector(".setup-primary")).toBeNull();
    if (profile === "base") {
      await act(async () => container.querySelector<HTMLInputElement>('input[value="base"]')!.click());
    } else {
      await act(async () => [...container.querySelectorAll<HTMLInputElement>('input')]
        .find((input) => input.parentElement?.textContent?.includes("Llama 3.2 · 3B"))!.click());
    }
    const save = findButton(container, "Save runtime settings");
    await act(async () => { save.click(); save.click(); });
    expect(adopt).toHaveBeenCalledTimes(1);
    expect(adopt).toHaveBeenCalledWith({ profile, model_groups: profile === "base" ? [] : ["tts-mlx", "cleaner-mlx"], cleaner_cuda_quantization: "4bit" }, { timeoutMs: 120_000 });
    expect(container.querySelector(".setup-primary")!.textContent).toContain("Saving…");
    expect([...container.querySelectorAll("fieldset")].every((fieldset) => fieldset.disabled)).toBe(true);
    await act(async () => resolveSave({ ...initialStatus, selected_profile: profile }));
    expect(onReady).toHaveBeenCalledOnce();
  });

  it("restores an installed runtime from library-only Settings without reinstalling", async () => {
    const ready = readySettings();
    const adopt = vi.fn(async () => ready);
    const runtimeBridge = packagedSettingsBridge();
    await renderPage(runtimeBridge, {
      initialStatus: { ...ready, selected_profile: "base", selected_model_groups: [] },
      ...settingsMount, apiClient: { adoptRuntimeSetup: adopt } as unknown as ApiClient,
    });
    await act(async () => container.querySelector<HTMLInputElement>('input[value="mlx"]')!.click());
    await act(async () => findButton(container, "Save runtime settings").click());
    expect(adopt).toHaveBeenCalledWith({ profile: "mlx", model_groups: ["tts-mlx", "cleaner-mlx"], cleaner_cuda_quantization: "4bit" }, { timeoutMs: 120_000 });
    expect(runtimeBridge.start).not.toHaveBeenCalled();
  });

  it("keeps a failed Settings save retryable without silently switching to library-only", async () => {
    const adopt = vi.fn().mockRejectedValueOnce(new Error("Backend disconnected")).mockResolvedValue(readySettings());
    const onReady = vi.fn();
    const onContinueLibraryOnly = vi.fn();
    await renderPage(packagedSettingsBridge(), { initialStatus: readySettings(), ...settingsMount, apiClient: { adoptRuntimeSetup: adopt } as unknown as ApiClient, onReady, onContinueLibraryOnly });
    await act(async () => container.querySelector<HTMLInputElement>('input[value="base"]')!.click());
    await act(async () => findButton(container, "Save runtime settings").click());
    expect(container.textContent).toContain("Backend disconnected");
    expect(onReady).not.toHaveBeenCalled();
    expect(onContinueLibraryOnly).not.toHaveBeenCalled();
    expect(container.querySelector<HTMLInputElement>('input[value="base"]')!.checked).toBe(true);
    await act(async () => findButton(container, "Save runtime settings").click());
    expect(adopt).toHaveBeenCalledTimes(2);
    expect(onReady).toHaveBeenCalledOnce();
  });

  it("downloads a missing optional cleaner even when the configured TTS is ready", async () => {
    const start = vi.fn(async () => ({ lifecycle: "running" }));
    const adopt = vi.fn();
    await renderPage(packagedSettingsBridge(), { initialStatus: readySettings(false), ...settingsMount, apiClient: { startRuntimeModelDownload: start, adoptRuntimeSetup: adopt } as unknown as ApiClient });
    await act(async () => [...container.querySelectorAll<HTMLInputElement>('input')]
      .find((input) => input.parentElement?.textContent?.includes("Llama 3.2 · 3B"))!.click());
    await act(async () => container.querySelector<HTMLButtonElement>('.setup-primary')!.click());
    expect(start).toHaveBeenCalledWith({ profile: "mlx", model_groups: ["tts-mlx", "cleaner-mlx"], cleaner_cuda_quantization: "4bit" }, { timeoutMs: 15_000 });
    expect(adopt).not.toHaveBeenCalled();
  });

  it("does not offer downloads for an unavailable cleaner runtime", async () => {
    const initialStatus = readySettings(false);
    initialStatus.installed_runtime!.model_groups[1].runtime_ready = false;
    await renderPage(packagedSettingsBridge(), { initialStatus, ...settingsMount });
    await act(async () => [...container.querySelectorAll<HTMLInputElement>('input')]
      .find((input) => input.parentElement?.textContent?.includes("Llama 3.2 · 3B"))!.click());
    expect(findButton(container, "Install").disabled).toBe(true);
    expect(container.textContent).toContain("This runtime is unavailable in this installation");
    expect(container.querySelector<HTMLInputElement>('input[value="cuda"]')!.disabled).toBe(true);
  });

  it("preserves unsaved Settings choices when checking runtime status", async () => {
    const initialStatus = readySettings();
    const onReady = vi.fn();
    const api = { getRuntimeSetupStatus: vi.fn(async () => initialStatus) } as unknown as ApiClient;
    await renderPage(packagedSettingsBridge(), { initialStatus, ...settingsMount, apiClient: api, onReady });
    await act(async () => container.querySelector<HTMLInputElement>('input[value="base"]')!.click());
    await act(async () => findButton(container, "Check runtime").click());
    expect(api.getRuntimeSetupStatus).toHaveBeenCalledOnce();
    expect(container.querySelector<HTMLInputElement>('input[value="base"]')!.checked).toBe(true);
    expect(findButton(container, "Save runtime settings").disabled).toBe(false);
    expect(onReady).not.toHaveBeenCalled();
  });

  it("shows a busy line while the runtime check runs, then what it found and when", async () => {
    const initialStatus = readySettings();
    let finish!: (status: RuntimeSetupStatusResponse) => void;
    const api = { getRuntimeSetupStatus: vi.fn(() => new Promise<RuntimeSetupStatusResponse>((resolve) => { finish = resolve; })) } as unknown as ApiClient;
    await renderPage(packagedSettingsBridge(), { initialStatus, ...settingsMount, apiClient: api });
    expect(container.querySelector(".setup-runtime-check")).toBeNull();
    await act(async () => findButton(container, "Check runtime").click());
    const busy = container.querySelector(".setup-runtime-check")!;
    expect(busy.getAttribute("role")).toBe("status");
    expect(busy.getAttribute("aria-busy")).toBe("true");
    expect(busy.textContent).toContain("Checking the runtime…");
    await act(async () => finish(initialStatus));
    const result = container.querySelector(".setup-runtime-check")!;
    expect(result.getAttribute("role")).toBe("status");
    expect(result.getAttribute("aria-busy")).toBe("false");
    expect(result.textContent).toMatch(/^Runtime ready\. Checked \d{1,2}[:.]\d{2}.*\.$/);
    expect(result.classList.contains("is-error")).toBe(false);

    await act(async () => findButton(container, "Check runtime").click());
    const broken = readySettings();
    broken.ready = false;
    broken.state = "error";
    broken.selected_model_groups = [{ id: "tts-mlx", runtime_ready: true, assets_ready: false, assets_reason: "corrupt", ready: false }];
    await act(async () => finish(broken));
    const problem = container.querySelector(".setup-runtime-check")!;
    expect(problem.textContent).toContain("Kokoro TTS is damaged. Checked ");
    expect(problem.classList.contains("is-error")).toBe(true);
  });

  it("leaves a failed runtime check to the error notice", async () => {
    const api = { getRuntimeSetupStatus: vi.fn(async () => { throw new Error("backend offline"); }) } as unknown as ApiClient;
    await renderPage(packagedSettingsBridge(), { initialStatus: readySettings(), ...settingsMount, apiClient: api });
    await act(async () => findButton(container, "Check runtime").click());
    expect(container.querySelector(".setup-runtime-check")).toBeNull();
    expect(container.querySelector(".setup-error")?.textContent).toContain("backend offline");
  });

  it("says the Settings cleaner choice sets the runtime's model, not the cleaner a book uses", async () => {
    const api = { getRuntimeModelInventory: vi.fn(async () => ({ models: [] })) } as unknown as ApiClient;
    await renderPage(packagedSettingsBridge(), { initialStatus: readySettings(), ...settingsMount, apiClient: api });
    const manager = container.querySelector(".setup-model-manager")!;
    expect([...manager.querySelectorAll(".setup-group-label")].map((label) => label.textContent))
      .toEqual(["Voice · required", "Text cleaner · runtime model"]);
    const hint = manager.querySelector("#managed-cleaner-hint")!;
    expect(hint.textContent).toBe("The model this runtime is set up with. Text cleaning in Settings chooses the cleaner your books use.");
    const radios = [...manager.querySelectorAll<HTMLInputElement>('input[name="managed-cleaner"]')];
    expect(radios.length).toBeGreaterThan(1);
    expect(radios.every((radio) => radio.getAttribute("aria-describedby") === "managed-cleaner-hint")).toBe(true);
    expect(radios.map((radio) => radio.getAttribute("aria-label")).filter(Boolean)).toEqual(["Set up this runtime with Llama 3.2 · 3B"]);
  });

  it("confirms deletion of one model, guards duplicate clicks, and offers install again", async () => {
    let currentStatus = readySettings();
    let resolveDelete!: (value: { deleted_model_groups: string[]; freed_bytes: number; state_deleted: boolean }) => void;
    const remove = vi.fn(() => new Promise<{ deleted_model_groups: string[]; freed_bytes: number; state_deleted: boolean }>((resolve) => { resolveDelete = resolve; }));
    const start = vi.fn(async () => ({ lifecycle: "running" }));
    let removed = false;
    const api = {
      getRuntimeModelInventory: vi.fn(async () => ({ models: [
        { id: "tts-mlx", downloaded: true, size_bytes: 700_000_000 },
        { id: "cleaner-mlx", downloaded: !removed, size_bytes: removed ? 0 : 2_000_000_000 },
      ] })),
      deleteRuntimeModel: remove, startRuntimeModelDownload: start,
      getRuntimeSetupStatus: vi.fn(async () => currentStatus),
    } as unknown as ApiClient;
    await renderPage(packagedSettingsBridge(), { initialStatus: currentStatus, ...settingsMount, apiClient: api });
    const deleteButton = container.querySelector<HTMLButtonElement>('[aria-label="Delete Llama 3.2 · 3B"]')!;
    await act(async () => deleteButton.click());
    expect(remove).not.toHaveBeenCalled();
    await act(async () => findButton(container, "Cancel").click());
    expect(container.textContent).not.toContain("Your books and audio will stay");
    expect(document.activeElement).toBe(deleteButton);
    await act(async () => deleteButton.click());
    expect(document.activeElement?.textContent).toBe("Cancel");
    await act(async () => document.activeElement!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(container.querySelector(".setup-model-confirm")).toBeNull();
    expect(document.activeElement).toBe(deleteButton);
    await act(async () => deleteButton.click());
    await act(async () => { findButton(container, "Delete model").click(); findButton(container, "Delete model").click(); });
    expect(remove).toHaveBeenCalledExactlyOnceWith("cleaner-mlx", { timeoutMs: 120_000 });
    expect(container.querySelector<HTMLFieldSetElement>('fieldset')!.disabled).toBe(true);
    removed = true;
    currentStatus = readySettings(false);
    await act(async () => resolveDelete({ deleted_model_groups: ["cleaner-mlx"], freed_bytes: 2_000_000_000, state_deleted: false }));
    expect(container.textContent).toContain("Llama 3.2 · 3B deleted. Freed");
    expect(container.querySelector('[aria-label="Delete Kokoro TTS"]')).not.toBeNull();
    const install = container.querySelector<HTMLButtonElement>('[aria-label="Download Llama 3.2 · 3B"]')!;
    expect(install.textContent).toBe("Download");
    await act(async () => install.click());
    expect(start).toHaveBeenCalledExactlyOnceWith({ profile: "mlx", model_groups: ["cleaner-mlx"], configure_runtime: false }, { timeoutMs: 15_000 });
  });

  it("finishes a model-only download without replacing the saved runtime selection", async () => {
    const snapshot = { lifecycle: "complete", profile: "mlx", model_groups: ["cleaner-mlx"], total_models: 1, completed_models: 1, current_model_group: null, error: null, configure_runtime: false };
    const adopt = vi.fn();
    const onReady = vi.fn();
    const api = {
      getRuntimeSetupStatus: vi.fn(async () => readySettings()),
      getRuntimeModelDownloadStatus: vi.fn(async () => snapshot),
      startRuntimeModelDownload: vi.fn(async () => snapshot),
      adoptRuntimeSetup: adopt,
    } as unknown as ApiClient;
    await renderPage(packagedSettingsBridge(), { initialStatus: readySettings(false), ...settingsMount, apiClient: api, onReady });
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Download Llama 3.2 · 3B"]')!.click());
    await act(async () => undefined);
    expect(adopt).not.toHaveBeenCalled();
    expect(api.getRuntimeSetupStatus).toHaveBeenCalled();
    expect(onReady).toHaveBeenCalledWith(expect.objectContaining({ selected_model_groups: [expect.objectContaining({ id: "tts-mlx" })] }));
    expect(container.textContent).toContain("Model installed");
  });

  it.each([false, true])("retries the model whose download failed (resumed after remount: %s)", async (resumed) => {
    const running: RuntimeModelDownloadStatus = { lifecycle: "running", profile: "mlx", model_groups: ["cleaner-mlx"], total_models: 1, completed_models: 0, current_model_group: "cleaner-mlx", error: null, configure_runtime: false };
    const failed: RuntimeModelDownloadStatus = { ...running, lifecycle: "failed", error: "Network unreachable" };
    const downloadStatus = vi.fn(async (): Promise<RuntimeModelDownloadStatus> => failed);
    if (resumed) downloadStatus.mockResolvedValueOnce(running);
    const start = vi.fn(async () => running);
    const api = { getRuntimeModelDownloadStatus: downloadStatus, startRuntimeModelDownload: start } as unknown as ApiClient;
    await renderPage(packagedSettingsBridge(), { initialStatus: readySettings(false), ...settingsMount, apiClient: api });
    if (!resumed) await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Download Llama 3.2 · 3B"]')!.click());
    await act(async () => undefined);
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("Network unreachable");
    start.mockClear();
    await act(async () => findButton(container, "Retry").click());
    expect(start).toHaveBeenCalledExactlyOnceWith({ profile: "mlx", model_groups: ["cleaner-mlx"], configure_runtime: false }, { timeoutMs: 15_000 });
  });

  it("does not mark a previously downloaded model as downloading during a later install", async () => {
    const start = vi.fn(async () => { throw new Error("Installer busy"); });
    const api = { getRuntimeSetupStatus: vi.fn(async () => readySettings(false)), startRuntimeModelDownload: start } as unknown as ApiClient;
    const runtimeBridge = bridge({ start: vi.fn(() => new Promise<RuntimeSetupRunnerStatus>(() => undefined)) });
    await renderPage(runtimeBridge, { initialStatus: readySettings(false), ...settingsMount, apiClient: api });
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Download Llama 3.2 · 3B"]')!.click());
    expect(start).toHaveBeenCalledOnce();
    const cleaner = [...container.querySelectorAll<HTMLInputElement>("input")]
      .find((input) => input.parentElement?.textContent?.includes("Llama 3.2 · 3B"))!;
    await act(async () => cleaner.click());
    await act(async () => findButton(container, "Install").click());
    expect(runtimeBridge.start).toHaveBeenCalledOnce();
    expect(container.querySelector(".is-downloading")).toBeNull();
    expect(container.textContent).not.toContain("Downloading…");
  });

  it("keeps an installed model and deletion controls visible after a delete failure", async () => {
    const remove = vi.fn(async () => { throw new Error("Wait for the current conversion to finish"); });
    const api = { deleteRuntimeModel: remove } as unknown as ApiClient;
    await renderPage(packagedSettingsBridge(), { initialStatus: readySettings(), ...settingsMount, apiClient: api });
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Delete Llama 3.2 · 3B"]')!.click());
    await act(async () => findButton(container, "Delete model").click());
    expect(container.textContent).toContain("Wait for the current conversion to finish");
    expect(container.querySelector<HTMLButtonElement>('[aria-label="Delete Llama 3.2 · 3B"]')!.disabled).toBe(false);
    expect(container.querySelector('[aria-label="Download Llama 3.2 · 3B"]')).toBeNull();
    expect(findButton(container, "Check runtime").disabled).toBe(false);
  });

  it("explains why a saved selection without the voice model cannot be used, and lets the user add it back", async () => {
    const status = readySettings();
    status.selected_model_groups = [{ id: "cleaner-mlx", runtime_ready: true, assets_ready: true, assets_reason: null, ready: true }];
    await renderPage(packagedSettingsBridge(), { initialStatus: status, ...settingsMount });
    const kokoro = () => [...container.querySelectorAll<HTMLInputElement>('input')]
      .find((input) => input.parentElement?.textContent?.includes("Kokoro TTS"))!;
    expect(kokoro().checked).toBe(false);
    expect(findButton(container, "Continue").disabled).toBe(true);
    expect(container.textContent).toContain("Select the Kokoro voice model");
    await act(async () => kokoro().click());
    expect(kokoro().checked).toBe(true);
    expect(kokoro().disabled).toBe(true);
    expect(container.textContent).not.toContain("Select the Kokoro voice model");
  });

  it("keeps the required Kokoro voice model selected while a cleaner changes", async () => {
    const start = vi.fn();
    await renderPage(packagedSettingsBridge(), { initialStatus: cudaCatalogStatus(), apiClient: { startRuntimeModelDownload: start } as unknown as ApiClient });
    const voice = container.querySelector<HTMLInputElement>('.setup-model-list input[type="checkbox"]')!;
    expect(voice.checked).toBe(true);
    expect(voice.disabled).toBe(true);
    expect(voice.getAttribute("aria-label")).toBe("Kokoro TTS (required)");
    await act(async () => voice.click());
    expect(voice.checked).toBe(true);
    const none = [...container.querySelectorAll<HTMLInputElement>('input[name="setup-cleaner"]')].find((input) => input.parentElement?.textContent?.includes("No local cleaner"))!;
    await act(async () => none.click());
    expect(voice.checked).toBe(true);
    expect(findButton(container, "Install").disabled).toBe(false);
    expect(start).not.toHaveBeenCalled();
  });

  it("shows a short compatibility reason on disabled runtime cards and keeps the full one available", async () => {
    await renderPage(bridge(), { ...settingsMount });
    const cuda = container.querySelector<HTMLInputElement>('input[value="cuda"]')!;
    expect(cuda.disabled).toBe(true);
    const card = cuda.closest("label")!;
    expect(card.querySelector(".setup-card-description")?.textContent).toBe("Windows or Linux only");
    expect(card.getAttribute("data-tip")).toBe("Unavailable.");
    expect(card.textContent).toContain("Unavailable.");
  });

  it("shows damaged models and preserves Delete after a partial removal", async () => {
    const inventory = { models: [{ id: "tts-mlx", downloaded: true, size_bytes: 42, damaged: true }] };
    const api = { getRuntimeModelInventory: vi.fn(async () => inventory), getRuntimeSetupStatus: vi.fn(async () => readySettings()), deleteRuntimeModel: vi.fn(async () => ({ freed_bytes: 10, residual_model_groups: ["tts-mlx"], message: "Couldn't delete some files — they may be in use." })) } as unknown as ApiClient;
    await renderPage(packagedSettingsBridge(), { ...settingsMount, initialStatus: readySettings(), apiClient: api });
    expect(container.textContent).toContain("Damaged");
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Delete Kokoro TTS"]')!.click());
    await act(async () => findButton(container, "Delete model").click());
    expect(container.textContent).toContain("Couldn't delete some files");
    expect(container.querySelector('[aria-label="Delete Kokoro TTS"]')).not.toBeNull();
  });

  it("disables Delete with the backend's reason while a model is in use", async () => {
    const reason = "Kokoro TTS is in use by the running conversion.";
    const inventory = { models: [{ id: "tts-mlx", downloaded: true, size_bytes: 42, deletable: false, delete_blocked: { code: "model_in_use", message: reason } }] };
    const api = { getRuntimeModelInventory: vi.fn(async () => inventory), getRuntimeSetupStatus: vi.fn(async () => readySettings()) } as unknown as ApiClient;
    await renderPage(packagedSettingsBridge(), { ...settingsMount, initialStatus: readySettings(), apiClient: api });
    const remove = container.querySelector<HTMLButtonElement>('[aria-label="Delete Kokoro TTS"]')!;
    expect(remove.disabled).toBe(true);
    expect(remove.getAttribute("data-tip")).toBe(reason);
  });

  it("ignores a save response from a disconnected backend", async () => {
    const initialStatus = readySettings();
    let resolveSave!: (status: RuntimeSetupStatusResponse) => void;
    const oldApi = { adoptRuntimeSetup: vi.fn(() => new Promise<RuntimeSetupStatusResponse>((resolve) => { resolveSave = resolve; })) } as unknown as ApiClient;
    const newApi = {} as ApiClient;
    const runtimeBridge = packagedSettingsBridge();
    const onReady = vi.fn();
    await renderPage(runtimeBridge, { initialStatus, ...settingsMount, apiClient: oldApi, onReady });
    await act(async () => container.querySelector<HTMLInputElement>('input[value="base"]')!.click());
    await act(async () => findButton(container, "Save runtime settings").click());
    await renderPage(runtimeBridge, { initialStatus, ...settingsMount, apiClient: newApi, onReady });
    await act(async () => resolveSave({ ...initialStatus, selected_profile: "base", selected_model_groups: [] }));
    expect(onReady).not.toHaveBeenCalled();
    expect(container.querySelector<HTMLInputElement>('input[value="mlx"]')!.checked).toBe(true);
    expect(container.querySelector(".setup-primary")).toBeNull();
  });

  it("uses native profile/model controls, exact payload, duplicate guard, focus, and unsubscribe", async () => {
    let resolveStart!: (value: Awaited<ReturnType<SetupPageBridge["start"]>>) => void;
    const start = vi.fn(() => new Promise<Awaited<ReturnType<SetupPageBridge["start"]>>>((resolve) => { resolveStart = resolve; }));
    const unlisten = vi.fn();
    const runtimeBridge = bridge({ start, listen: vi.fn(async () => unlisten) });
    await renderPage(runtimeBridge);

    const mlx = container.querySelector<HTMLInputElement>('input[type="radio"][value="mlx"]')!;
    await act(async () => mlx.click());
    const cleaner = [...container.querySelectorAll<HTMLInputElement>('input[name="setup-cleaner"]')]
      .find((input) => input.parentElement?.textContent?.includes("No local cleaner"))!;
    await act(async () => cleaner.click());
    const install = findButton(container, "Install");
    await act(async () => { install.click(); install.click(); });
    expect(start).toHaveBeenCalledTimes(1);
    expect(start).toHaveBeenCalledWith({ profile: "mlx", modelGroups: ["tts-mlx"], cleanerCudaQuantization: "4bit" });
    expect(document.activeElement?.id).toBe("setup-progress-heading");
    resolveStart({ lifecycle: "running", profile: "mlx", modelGroups: ["tts-mlx"], stage: null, message: null, percent: null, restartRequired: false, error: null });
    await act(async () => undefined);
    await act(async () => root.unmount());
    expect(unlisten).toHaveBeenCalledOnce();
    root = createRoot(container);
  });

  it("keeps start disabled until the progress listener is registered", async () => {
    let resolveListen!: (unlisten: () => void) => void;
    const listen = vi.fn(() => new Promise<() => void>((resolve) => {
      resolveListen = resolve;
    }));
    const start = vi.fn(async (): Promise<RuntimeSetupRunnerStatus> => ({
      lifecycle: "running",
      profile: "base",
      modelGroups: [],
      stage: null,
      message: null,
      percent: null,
      restartRequired: false,
      error: null,
    }));
    const status = vi.fn(async (): Promise<RuntimeSetupRunnerStatus> => ({
      lifecycle: "idle",
      profile: null,
      modelGroups: [],
      stage: null,
      message: null,
      percent: null,
      restartRequired: false,
      error: null,
    }));
    const runtimeBridge = bridge({ listen, start, status });
    const onReady = vi.fn();

    await renderPage(runtimeBridge, { onReady });

    const install = findButton(container, "Use library only");
    expect(install.disabled).toBe(true);
    expect(start).not.toHaveBeenCalled();
    expect(status).not.toHaveBeenCalled();

    await act(async () => {
      resolveListen(vi.fn());
      await Promise.resolve();
    });

    expect(install.disabled).toBe(false);
    expect(status).toHaveBeenCalledOnce();
    await act(async () => install.click());
    expect(start).toHaveBeenCalledOnce();
  });

  it("keeps start disabled until runner reconciliation completes", async () => {
    let resolveStatus!: (value: RuntimeSetupRunnerStatus) => void;
    const status = vi.fn(() => new Promise<RuntimeSetupRunnerStatus>((resolve) => {
      resolveStatus = resolve;
    }));
    const runtimeBridge = bridge({
      listen: vi.fn(async () => vi.fn()),
      status,
    });

    await renderPage(runtimeBridge);
    const install = findButton(container, "Use library only");
    expect(install.disabled).toBe(true);

    await act(async () => undefined);
    expect(status).toHaveBeenCalledOnce();
    expect(install.disabled).toBe(true);

    await act(async () => {
      resolveStatus({ lifecycle: "idle", profile: null, modelGroups: [], stage: null, message: null, percent: null, restartRequired: false, error: null });
      await Promise.resolve();
    });
    expect(install.disabled).toBe(false);
  });

  it("shows retryable progress listener recovery after registration fails", async () => {
    let attempts = 0;
    const listen = vi.fn(async () => {
      attempts += 1;
      if (attempts === 1) throw new Error("event bridge unavailable");
      return vi.fn();
    });
    const runtimeBridge = bridge({ listen });

    await renderPage(runtimeBridge);
    await act(async () => undefined);

    expect(container.textContent).toContain("Progress connection unavailable");
    expect(findButton(container, "Copy diagnostics")).toBeTruthy();
    const retry = findButton(container, "Retry");
    expect(findButton(container, "Use library only").disabled).toBe(true);

    await act(async () => retry.click());
    await act(async () => undefined);
    expect(listen).toHaveBeenCalledTimes(2);
    expect(findButton(container, "Use library only").disabled).toBe(false);
  });

  it("registers before reconciling a running setup after remount", async () => {
    const progressListeners: Array<(event: SetupProgressEvent) => void> = [];
    let resolveRemountListen!: (unlisten: () => void) => void;
    let registrationCount = 0;
    const listen = vi.fn((onProgress: (event: SetupProgressEvent) => void) => {
      progressListeners.push(onProgress);
      registrationCount += 1;
      if (registrationCount === 1) return Promise.resolve(vi.fn());
      return new Promise<() => void>((resolve) => {
        resolveRemountListen = resolve;
      });
    });
    const status = vi.fn(async (): Promise<RuntimeSetupRunnerStatus> => ({
      lifecycle: "running",
      profile: "mlx",
      modelGroups: ["tts-mlx"],
      stage: "verify",
      message: "Verifying setup",
      percent: 92,
      restartRequired: false,
      error: null,
    }));
    const runtimeBridge = bridge({ listen, status });
    const onReady = vi.fn();

    await renderPage(runtimeBridge, { onReady });
    expect(status).toHaveBeenCalledOnce();

    await act(async () => root.unmount());
    root = createRoot(container);
    await renderPage(runtimeBridge, { onReady });

    expect(status).toHaveBeenCalledOnce();
    await act(async () => {
      resolveRemountListen(vi.fn());
      await Promise.resolve();
    });
    expect(status).toHaveBeenCalledTimes(2);
    expect(container.querySelector('[role="progressbar"]')?.getAttribute("aria-valuenow")).toBe("92");
    expect(container.textContent).toContain("Verifying setup");
    expect(container.querySelectorAll(".setup-stage.is-complete")).toHaveLength(2);

    await act(async () => {
      progressListeners[1]({ event: "stage_started", stage: "verify", message: "Verifying setup", percent: 92 });
    });
    expect(container.querySelector('[role="progressbar"]')?.getAttribute("aria-valuenow")).toBe("92");
    expect(container.textContent).toContain("Verifying setup");
    expect(container.querySelectorAll(".setup-stage.is-complete")).toHaveLength(2);
  });

  it("refreshes readiness from the reconnected backend and leaves setup after one restart", async () => {
    const restartStatus = { ...setupStatus, configured: true, state: "restart_required" as const, restart_required: true, selected_profile: "base" as const };
    const readyStatus = {
      ...restartStatus,
      state: "ready" as const,
      ready: true,
      restart_required: false,
      packages: { ready: true, missing: [], incompatible: [] },
      probe: { ready: true, checks: [] },
      backend: { instance_id: "two", status: "running" },
    };
    const runtimeBridge = bridge();
    const staleApi = { getRuntimeSetupStatus: vi.fn(async () => restartStatus) } as unknown as ApiClient;
    const readyApi = { getRuntimeSetupStatus: vi.fn(async () => readyStatus) } as unknown as ApiClient;
    const onReady = vi.fn();
    const reconnect = vi.fn();

    function Harness() {
      const [api, setApi] = useState(staleApi);
      return (
        <SetupPage
          apiClient={api}
          initialStatus={restartStatus}
          initialError={null}
          onReady={onReady}
          onContinueLibraryOnly={vi.fn()}
          onReconnectBackend={() => {
            reconnect();
            setApi(readyApi);
          }}
          bridge={runtimeBridge}
        />
      );
    }

    await act(async () => root.render(<Harness />));
    vi.mocked(staleApi.getRuntimeSetupStatus).mockClear();
    await act(async () => {
      const restart = findButton(container, "Restart Orpheus");
      restart.click();
      restart.click();
    });
    expect(runtimeBridge.restart).toHaveBeenCalledOnce();
    expect(reconnect).toHaveBeenCalledOnce();
    expect(staleApi.getRuntimeSetupStatus).not.toHaveBeenCalled();
    expect(readyApi.getRuntimeSetupStatus).toHaveBeenCalledOnce();
    expect(onReady).toHaveBeenCalledOnce();
    expect(onReady).toHaveBeenCalledWith(readyStatus);
  });

  it("does not reconnect after a failed restart and keeps restart available", async () => {
    const restartStatus = { ...setupStatus, configured: true, state: "restart_required" as const, restart_required: true, selected_profile: "base" as const };
    let failRestart!: (reason: Error) => void;
    const runtimeBridge = bridge({ restart: vi.fn()
      .mockReturnValueOnce(new Promise<void>((_, reject) => { failRestart = reject; }))
      .mockResolvedValueOnce(undefined) });
    const reconnect = vi.fn();
    await renderPage(runtimeBridge, { initialStatus: restartStatus, onReconnectBackend: reconnect });
    await act(async () => findButton(container, "Restart Orpheus").click());
    await act(async () => failRestart(new Error("restart failed")));
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("restart failed");
    expect(reconnect).not.toHaveBeenCalled();
    await act(async () => findButton(container, "Restart Orpheus").click());
    expect(runtimeBridge.restart).toHaveBeenCalledTimes(2);
    expect(reconnect).toHaveBeenCalledOnce();
  });

  it("keeps repeated cancellation owned by a stopping backend download in a source checkout", async () => {
    const runtimeBridge = bridge();
    const initial = readySettings(false);
    const running = { lifecycle: "running" as const, profile: "mlx", model_groups: ["cleaner-mlx"], total_models: 1, completed_models: 0 };
    const cancelDownload = vi.fn(async () => ({ ...running, lifecycle: "stopping" as const }));
    const apiClient = { startRuntimeModelDownload: vi.fn(async () => running), cancelRuntimeModelDownload: cancelDownload } as unknown as ApiClient;
    await renderPage(runtimeBridge, { initialStatus: initial, ...settingsMount, apiClient });
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label^="Download "]')!.click());
    await act(async () => findButton(container, "Cancel").click());
    expect(container.textContent).toContain("Stopping download");
    await act(async () => findButton(container, "Cancel").click());
    expect(runtimeBridge.cancel).not.toHaveBeenCalled();
    expect(cancelDownload).toHaveBeenCalledOnce();
    expect(findButton(container, "Check runtime").disabled).toBe(true);
  });

  it("cancels an authoritative running setup", async () => {
    const runtimeBridge = bridge({ status: vi.fn(async (): Promise<RuntimeSetupRunnerStatus> => ({ lifecycle: "running", profile: "base", modelGroups: [], stage: null, message: null, percent: null, restartRequired: false, error: null })) });
    await renderPage(runtimeBridge);
    await act(async () => findButton(container, "Cancel").click());
    expect(runtimeBridge.cancel).toHaveBeenCalledOnce();
  });

  it("deduplicates pending model cancellation and ignores its result after backend replacement", async () => {
    const running = { lifecycle: "running" as const, profile: "mlx", model_groups: ["tts-mlx"], total_models: 1, completed_models: 0 };
    type CancelledDownload = Omit<typeof running, "lifecycle"> & { lifecycle: "cancelled" };
    let resolveCancel!: (value: CancelledDownload) => void;
    const cancelDownload = vi.fn(() => new Promise<CancelledDownload>((resolve) => { resolveCancel = resolve; }));
    const oldApi = {
      getRuntimeModelDownloadStatus: vi.fn(async () => running),
      cancelRuntimeModelDownload: cancelDownload,
    } as unknown as ApiClient;
    const runtimeBridge = packagedSettingsBridge();
    await renderPage(runtimeBridge, { apiClient: oldApi });
    await act(async () => {
      findButton(container, "Cancel").click();
      findButton(container, "Cancel").click();
    });
    expect(cancelDownload).toHaveBeenCalledOnce();

    const newApi = { getRuntimeModelDownloadStatus: vi.fn(async () => running) } as unknown as ApiClient;
    await renderPage(runtimeBridge, { apiClient: newApi });
    await act(async () => resolveCancel({ ...running, lifecycle: "cancelled" }));
    expect(container.textContent).not.toContain("Model download cancelled.");
    expect(findButton(container, "Cancel")).toBeTruthy();
    expect(container.querySelector("#setup-progress-heading")).not.toBeNull();
  });

  it("allows retry after model cancellation fails", async () => {
    const running = { lifecycle: "running" as const, profile: "mlx", model_groups: ["tts-mlx"], total_models: 1, completed_models: 0 };
    const cancelDownload = vi.fn()
      .mockRejectedValueOnce(new Error("Temporary connection failure"))
      .mockResolvedValue({ ...running, lifecycle: "stopping" });
    const apiClient = { getRuntimeModelDownloadStatus: vi.fn(async () => running), cancelRuntimeModelDownload: cancelDownload } as unknown as ApiClient;
    await renderPage(packagedSettingsBridge(), { apiClient });
    await act(async () => findButton(container, "Cancel").click());
    expect(container.textContent).toContain("Temporary connection failure");
    await act(async () => findButton(container, "Cancel").click());
    expect(cancelDownload).toHaveBeenCalledTimes(2);
    expect(container.textContent).toContain("Stopping download");
    expect(container.textContent).not.toContain("Temporary connection failure");
  });

  it("stays busy while an async cancel is still in flight", async () => {
    const runtimeBridge = bridge({
      status: vi.fn(async (): Promise<RuntimeSetupRunnerStatus> => ({ lifecycle: "running", profile: "base", modelGroups: [], stage: null, message: null, percent: null, restartRequired: false, error: null })),
      cancel: vi.fn(async (): Promise<RuntimeSetupRunnerStatus> => ({ lifecycle: "cancelling", profile: "base", modelGroups: [], stage: null, message: null, percent: null, restartRequired: false, error: null })),
    });
    await renderPage(runtimeBridge);
    await act(async () => findButton(container, "Cancel").click());
    expect(runtimeBridge.cancel).toHaveBeenCalledOnce();
    // A "cancelling" runner is still running: the Cancel affordance must remain
    // and the install/library primary action must stay disabled.
    expect(findButton(container, "Cancel")).toBeTruthy();
    expect(findButton(container, "Use library only").disabled).toBe(true);
    expect(findButton(container, "Back to dashboard").disabled).toBe(false);
  });

  it("does not let the busy poll interrupt an in-flight restart", async () => {
    vi.useFakeTimers();
    try {
      const restartStatus = { ...setupStatus, configured: true, state: "restart_required" as const, restart_required: true, selected_profile: "base" as const };
      const runtimeBridge = bridge({
        // A finished install leaves the runner terminal ("complete"); the 1s poll
        // must not observe this mid-restart and flip the UI out of the busy state.
        status: vi.fn(async (): Promise<RuntimeSetupRunnerStatus> => ({ lifecycle: "complete", profile: "base", modelGroups: [], stage: null, message: null, percent: null, restartRequired: false, error: null })),
      });
      const api = { getRuntimeSetupStatus: vi.fn(async () => restartStatus) } as unknown as ApiClient;
      await renderPage(runtimeBridge, { apiClient: api, initialStatus: restartStatus });
      await act(async () => undefined);

      await act(async () => { findButton(container, "Restart Orpheus").click(); });
      // Reconnect never completes (apiClient unchanged), so the restart stays in
      // flight. Advancing several 1s poll intervals must keep the busy/restart UI.
      await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });

      expect(container.textContent).not.toContain("Setup status unavailable");
      expect(container.textContent).toContain("Orpheus restarted. Checking the runtime…");
      expect(findButton(container, "Restart Orpheus").disabled).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it.each(["disconnected", "stale"])("waits for restart after installation with a %s backend", async (backend) => {
    const complete: RuntimeSetupRunnerStatus = {
      lifecycle: "complete", profile: "cuda", modelGroups: ["tts-cpu-cuda"],
      stage: "verify", message: "Setup state saved.", percent: 100,
      restartRequired: true, error: null,
    };
    const runtimeBridge = bridge({ status: vi.fn(async () => complete) });
    const getStatus = vi.fn().mockRejectedValue(new Error("Old backend is unavailable"));
    const api = { getRuntimeSetupStatus: getStatus } as unknown as ApiClient;
    const onReady = vi.fn();
    await renderPage(runtimeBridge, { apiClient: backend === "disconnected" ? null : api, onReady });

    expect(getStatus).not.toHaveBeenCalled();
    expect(onReady).not.toHaveBeenCalled();
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(container.querySelector('[role="progressbar"]')?.getAttribute("aria-valuenow")).toBe("100");
    expect(container.textContent).toContain("Installation complete. Restart Orpheus to load the selected runtime.");
    expect(findButton(container, "Restart Orpheus").disabled).toBe(false);
  });

  it("does not query the old backend when start returns a completed installation", async () => {
    const runtimeBridge = bridge({ start: vi.fn(async (): Promise<RuntimeSetupRunnerStatus> => ({
      lifecycle: "complete", profile: "base", modelGroups: [], stage: "verify",
      message: "Setup state saved.", percent: 100, restartRequired: true, error: null,
    })) });
    const api = await renderPage(runtimeBridge);
    await act(async () => findButton(container, "Use library only").click());
    expect(api.getRuntimeSetupStatus).not.toHaveBeenCalled();
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(findButton(container, "Restart Orpheus").disabled).toBe(false);
  });

  it("offers no Cancel while Orpheus restarts and reconnects", async () => {
    const runtimeBridge = bridge({ status: vi.fn(async (): Promise<RuntimeSetupRunnerStatus> => ({
      lifecycle: "complete", profile: "base", modelGroups: [], stage: "verify",
      message: "Setup state saved.", percent: 100, restartRequired: true, error: null,
    })) });
    await renderPage(runtimeBridge);
    await act(async () => findButton(container, "Restart Orpheus").click());
    expect(container.textContent).toContain("Orpheus restarted. Checking the runtime…");
    expect([...container.querySelectorAll(".setup-footer button")].map((button) => button.textContent)).toEqual(["Restart Orpheus"]);
  });

  it("keeps restart locked across a disconnected client until fresh readiness resolves", async () => {
    const complete: RuntimeSetupRunnerStatus = {
      lifecycle: "complete", profile: "base", modelGroups: [], stage: "verify",
      message: "Setup state saved.", percent: 100, restartRequired: true, error: null,
    };
    const runtimeBridge = bridge({ status: vi.fn(async () => complete) });
    const staleApi = { getRuntimeSetupStatus: vi.fn() } as unknown as ApiClient;
    let resolveReady!: (status: RuntimeSetupStatusResponse) => void;
    const readyApi = { getRuntimeSetupStatus: vi.fn(() => new Promise<RuntimeSetupStatusResponse>((resolve) => { resolveReady = resolve; })) } as unknown as ApiClient;
    const onReady = vi.fn();
    let connect!: () => void;

    function Harness() {
      const [api, setApi] = useState<ApiClient | null>(staleApi);
      connect = () => setApi(readyApi);
      return <SetupPage apiClient={api} initialStatus={setupStatus} initialError={null}
        onReady={onReady} onContinueLibraryOnly={vi.fn()}
        onReconnectBackend={() => setApi(null)} bridge={runtimeBridge} />;
    }

    await act(async () => root.render(<Harness />));
    await act(async () => findButton(container, "Restart Orpheus").click());
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(findButton(container, "Restart Orpheus").disabled).toBe(true);
    await act(async () => connect());
    expect(findButton(container, "Restart Orpheus").disabled).toBe(true);
    expect(staleApi.getRuntimeSetupStatus).not.toHaveBeenCalled();
    expect(readyApi.getRuntimeSetupStatus).toHaveBeenCalledOnce();
    const readyStatus: RuntimeSetupStatusResponse = {
      ...setupStatus, configured: true, state: "ready", ready: true, restart_required: false,
      backend: { instance_id: "two", status: "running" },
    };
    await act(async () => resolveReady(readyStatus));
    expect(onReady).toHaveBeenCalledExactlyOnceWith(readyStatus);
    expect(container.textContent).not.toContain("Restart Orpheus");
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });

  it("offers repair after one successful restart when the runtime remains unavailable", async () => {
    const complete: RuntimeSetupRunnerStatus = {
      lifecycle: "complete", profile: "base", modelGroups: [], stage: "verify",
      message: "Setup state saved.", percent: 100, restartRequired: true, error: null,
    };
    const runtimeBridge = bridge({ status: vi.fn(async () => complete) });
    const staleApi = { getRuntimeSetupStatus: vi.fn() } as unknown as ApiClient;
    const unavailable: RuntimeSetupStatusResponse = {
      ...setupStatus, configured: true, state: "unavailable", restart_required: false,
    };
    const readyApi = { getRuntimeSetupStatus: vi.fn(async () => unavailable) } as unknown as ApiClient;
    const onReady = vi.fn();
    function Harness() {
      const [api, setApi] = useState(staleApi);
      return <SetupPage apiClient={api} initialStatus={setupStatus} initialError={null}
        onReady={onReady} onContinueLibraryOnly={vi.fn()}
        onReconnectBackend={() => setApi(readyApi)} bridge={runtimeBridge} />;
    }
    await act(async () => root.render(<Harness />));
    await act(async () => findButton(container, "Restart Orpheus").click());
    expect(runtimeBridge.restart).toHaveBeenCalledOnce();
    expect(onReady).not.toHaveBeenCalled();
    expect(container.textContent).not.toContain("Restart Orpheus");
    expect(container.textContent).toContain("Orpheus restarted, but the selected runtime is not ready.");
    // The message names the control that is actually offered.
    expect(container.textContent).toContain("Select Retry to install it again.");
    expect(runtimeBridge.listen).toHaveBeenCalledOnce();
    expect(findButton(container, "Use library only").disabled).toBe(false);
    await act(async () => findButton(container, "Retry").click());
    expect(runtimeBridge.start).toHaveBeenCalledOnce();
    expect(runtimeBridge.restart).toHaveBeenCalledOnce();
  });

  it("refreshes setup status and reports ready when the runner is complete", async () => {
    const readyStatus = {
      ...setupStatus,
      configured: true,
      state: "ready" as const,
      ready: true,
      restart_required: false,
      selected_profile: "base" as const,
      packages: { ready: true, missing: [], incompatible: [] },
      probe: { ready: true, checks: [] },
    };
    const runtimeBridge = bridge({
      status: vi.fn(async (): Promise<RuntimeSetupRunnerStatus> => ({
        lifecycle: "complete",
        profile: "base",
        modelGroups: [],
        stage: null,
        message: null,
        percent: null,
        restartRequired: false,
        error: null,
      })),
    });
    const onReady = vi.fn();
    const api = { getRuntimeSetupStatus: vi.fn(async () => readyStatus) } as unknown as ApiClient;
    await renderPage(runtimeBridge, { apiClient: api, onReady });
    await act(async () => undefined);

    expect(api.getRuntimeSetupStatus).toHaveBeenCalledOnce();
    expect(onReady).toHaveBeenCalledWith(readyStatus);
    expect(container.querySelector('[role="progressbar"]')?.getAttribute("aria-valuenow")).toBe("100");
  });

  it("focuses the setup heading when the page mounts", async () => {
    await renderPage(bridge());
    expect(document.activeElement?.id).toBe("setup-heading");
    expect(container.textContent).not.toContain("Copy diagnostics");
  });

  it("never registers Tauri mutations when unavailable and keeps library-only recovery", async () => {
    const runtimeBridge = bridge({ environment: vi.fn(async (): Promise<RuntimeSetupEnvironment> => ({ available: false, mode: "browser", reason: "Desktop source checkout required." })) });
    const continueLibrary = vi.fn();
    await renderPage(runtimeBridge, { onContinueLibraryOnly: continueLibrary });
    expect(container.textContent).toContain("Open the desktop app to set up audio creation");
    expect(container.textContent).not.toContain("source checkout");
    expect(container.textContent).not.toContain(".venv");
    expect(runtimeBridge.listen).not.toHaveBeenCalled();
    expect(runtimeBridge.status).not.toHaveBeenCalled();
    // The primary action must stay usable without the Tauri installer: it
    // resolves to the library-only fallback instead of a dead button.
    expect([...container.querySelectorAll("button")].filter((button) => button.textContent === "Use library only")).toHaveLength(1);
    const libraryOnly = findButton(container, "Use library only");
    expect(libraryOnly.disabled).toBe(false);
    await act(async () => libraryOnly.click());
    expect(continueLibrary).toHaveBeenCalledOnce();
  });

  it("falls back to the session override when adopt succeeds but the runtime stays unavailable", async () => {
    const runtimeBridge = bridge({ environment: vi.fn(async (): Promise<RuntimeSetupEnvironment> => ({ available: false, mode: "browser", reason: "Desktop source checkout required." })) });
    const unavailableStatus: RuntimeSetupStatusResponse = {
      ...setupStatus,
      configured: true,
      state: "unavailable",
      selected_profile: "base",
      packages: { ready: false, missing: [], incompatible: [{ name: "fastapi", expected: "==0.139.0", installed: "0.129.0" }] },
    };
    const adopt = vi.fn(async () => unavailableStatus);
    const api = { getRuntimeSetupStatus: vi.fn(async () => setupStatus), adoptRuntimeSetup: adopt } as unknown as ApiClient;
    const onReady = vi.fn();
    const continueLibrary = vi.fn();
    await renderPage(runtimeBridge, { apiClient: api, onReady, onContinueLibraryOnly: continueLibrary });
    await act(async () => findButton(container, "Use library only").click());
    expect(adopt).toHaveBeenCalledOnce();
    expect(onReady).not.toHaveBeenCalled();
    expect(continueLibrary).toHaveBeenCalledOnce();
  });

  it("does not trap users when the setup status API is inaccessible", async () => {
    const runtimeBridge = bridge({ environment: vi.fn(async (): Promise<RuntimeSetupEnvironment> => ({ available: false, mode: "packaged", reason: "Profile-baked runtime." })) });
    const continueLibrary = vi.fn();
    await renderPage(runtimeBridge, {
      initialStatus: null,
      initialError: "Setup status endpoint unavailable.",
      onContinueLibraryOnly: continueLibrary,
    });
    expect(container.textContent).toContain("Setup status endpoint unavailable.");
    await act(async () => findButton(container, "Use library only").click());
    expect(continueLibrary).toHaveBeenCalledOnce();
  });

  it("adopts an installed-ready runtime and reports the fresh status", async () => {
    const installedStatus: RuntimeSetupStatusResponse = {
      ...setupStatus,
      recommended_profile: "mlx",
      installed_runtime: { profile: "mlx", ready: true, runtime_available: true, model_groups: [{ id: "tts-mlx", runtime_ready: true, assets_ready: true, ready: true }, { id: "cleaner-mlx", runtime_ready: true, assets_ready: false, ready: false }] },
    };
    const readyStatus = { ...installedStatus, configured: true, state: "ready" as const, ready: true, installed_runtime: null };
    const adopt = vi.fn(async () => readyStatus);
    const onReady = vi.fn();
    const api = { getRuntimeSetupStatus: vi.fn(async () => installedStatus), adoptRuntimeSetup: adopt } as unknown as ApiClient;
    await renderPage(bridge(), { apiClient: api, initialStatus: installedStatus, onReady });

    expect(container.querySelector('.setup-plan [role="note"]')?.textContent).toContain("Installed MLX runtime ready");
    await act(async () => container.querySelector<HTMLInputElement>('input[name="setup-cleaner"]')!.click());
    const cta = findButton(container, "Continue");
    expect(cta.disabled).toBe(false);
    await act(async () => cta.click());
    expect(adopt).toHaveBeenCalledWith(
      { profile: "mlx", model_groups: ["tts-mlx"], cleaner_cuda_quantization: "4bit" },
      { timeoutMs: 120_000 },
    );
    expect(onReady).toHaveBeenCalledWith(readyStatus);
  });

  it("offers Install instead of a no-op Continue when the saved runtime's packages are missing", async () => {
    const missingPackages: RuntimeSetupStatusResponse = {
      ...setupStatus, configured: true, state: "unavailable", ready: false, selected_profile: "mlx",
      selected_model_groups: [{ id: "tts-mlx", runtime_ready: true, assets_ready: true, assets_reason: null, ready: true }],
      packages: { ready: false, missing: [{ name: "mlx-audio", expected: ">=0.2", installed: null }], incompatible: [] },
      installed_runtime: null,
    };
    const adopt = vi.fn();
    const runtimeBridge = bridge();
    const api = { getRuntimeSetupStatus: vi.fn(async () => missingPackages), adoptRuntimeSetup: adopt } as unknown as ApiClient;
    await renderPage(runtimeBridge, { apiClient: api, initialStatus: missingPackages });

    const primary = container.querySelector<HTMLButtonElement>(".setup-primary")!;
    expect(primary.textContent).toContain("Install");
    expect(primary.disabled).toBe(false);
    await act(async () => primary.click());
    expect(adopt).not.toHaveBeenCalled();
    expect(runtimeBridge.start).toHaveBeenCalledOnce();
  });

  it("surfaces an adopt failure without reporting ready", async () => {
    const installedStatus: RuntimeSetupStatusResponse = {
      ...setupStatus,
      recommended_profile: "mlx",
      installed_runtime: { profile: "mlx", ready: true, runtime_available: true, model_groups: [{ id: "tts-mlx", runtime_ready: true, assets_ready: true, ready: true }] },
    };
    const adopt = vi.fn(async () => { throw new Error("API request failed (409): CUDA capability probe failed"); });
    const onReady = vi.fn();
    const api = { getRuntimeSetupStatus: vi.fn(async () => installedStatus), adoptRuntimeSetup: adopt } as unknown as ApiClient;
    await renderPage(bridge(), { apiClient: api, initialStatus: installedStatus, onReady });

    await act(async () => container.querySelector<HTMLInputElement>('input[name="setup-cleaner"]')!.click());
    await act(async () => findButton(container, "Continue").click());
    expect(adopt).toHaveBeenCalledOnce();
    expect(onReady).not.toHaveBeenCalled();
    expect(container.textContent).toContain("CUDA capability probe failed");
  });

  it("persists library-only choice via adopt when runtime mutations are unavailable", async () => {
    const readyBase = { ...setupStatus, configured: true, state: "ready" as const, ready: true, selected_profile: "base" as const };
    const adopt = vi.fn(async () => readyBase);
    const onReady = vi.fn();
    const continueLibrary = vi.fn();
    const api = { getRuntimeSetupStatus: vi.fn(async () => setupStatus), adoptRuntimeSetup: adopt } as unknown as ApiClient;
    const runtimeBridge = bridge({ environment: vi.fn(async (): Promise<RuntimeSetupEnvironment> => ({ available: false, mode: "packaged", reason: "Baked runtime." })) });
    await renderPage(runtimeBridge, { apiClient: api, onContinueLibraryOnly: continueLibrary, onReady });

    await act(async () => findButton(container, "Use library only").click());
    expect(adopt).toHaveBeenCalledWith(
      { profile: "base", model_groups: [] },
      { timeoutMs: 15_000 },
    );
    expect(onReady).toHaveBeenCalledWith(readyBase);
    expect(continueLibrary).not.toHaveBeenCalled();
  });

  it("falls back to the in-memory library-only override when adopt rejects", async () => {
    const adopt = vi.fn(async () => { throw new Error("network down"); });
    const continueLibrary = vi.fn();
    const api = { getRuntimeSetupStatus: vi.fn(async () => setupStatus), adoptRuntimeSetup: adopt } as unknown as ApiClient;
    const runtimeBridge = bridge({ environment: vi.fn(async (): Promise<RuntimeSetupEnvironment> => ({ available: false, mode: "packaged", reason: "Baked runtime." })) });
    await renderPage(runtimeBridge, { apiClient: api, onContinueLibraryOnly: continueLibrary });

    await act(async () => findButton(container, "Use library only").click());
    expect(adopt).toHaveBeenCalledOnce();
    expect(continueLibrary).toHaveBeenCalledOnce();
  });

  it("shows a configured packaged runtime as ready in Settings without offering a reinstall", async () => {
    const packagedBridge = bridge({ environment: vi.fn(async () => ({ available: false, mode: "packaged" as const, reason: "Packaged runtime" })) });
    await renderPage(packagedBridge, { initialStatus: readySettings(), ...settingsMount });
    expect(container.querySelector('.setup-plan [role="status"]')?.textContent).toContain("Apple Silicon (MLX) runtime is ready");
    expect(container.textContent).not.toContain("Audio creation is unavailable");
    expect(container.querySelector(".setup-primary")).toBeNull();
    expect(packagedBridge.start).not.toHaveBeenCalled();
  });

  it("arms then confirms deletion of local runtime data and reports freed space", async () => {
    const onReady = vi.fn();
    const del = vi.fn(async () => ({ deleted_model_groups: ["tts-mlx"], freed_bytes: 734003200, state_deleted: true }));
    const api = { getRuntimeSetupStatus: vi.fn(async () => setupStatus), deleteRuntimeSetupData: del } as unknown as ApiClient;
    await renderPage(bridge(), { apiClient: api, ...settingsMount, onReady });

    const disclosure = container.querySelector<HTMLDetailsElement>(".setup-danger")!;
    expect(disclosure.open).toBe(false);
    await act(async () => { disclosure.open = true; });
    await act(async () => findButton(container, "Delete local runtime data").click());
    expect(del).not.toHaveBeenCalled();
    await act(async () => {
      disclosure.open = false;
      disclosure.dispatchEvent(new Event("toggle"));
    });
    expect(findButton(container, "Delete local runtime data")).toBeTruthy();
    await act(async () => { disclosure.open = true; });
    await act(async () => findButton(container, "Delete local runtime data").click());
    expect(document.activeElement?.textContent).toBe("Cancel");
    await act(async () => document.activeElement!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(container.querySelector(".setup-model-confirm")).toBeNull();
    expect(document.activeElement).toBe(findButton(container, "Delete local runtime data"));
    await act(async () => findButton(container, "Delete local runtime data").click());
    await act(async () => findButton(container, "Cancel").click());
    expect(document.activeElement).toBe(findButton(container, "Delete local runtime data"));
    await act(async () => findButton(container, "Delete local runtime data").click());
    expect(del).not.toHaveBeenCalled();
    await act(async () => {
      const confirm = container.querySelector<HTMLButtonElement>(".setup-model-confirm .btn-danger")!;
      confirm.click();
      confirm.click();
    });
    expect(del).toHaveBeenCalledOnce();
    expect(container.textContent).toContain("Freed 700 MB.");
    expect(onReady).toHaveBeenCalledWith(setupStatus);
  });

  it("locks the page while local runtime data is reset and reports a failed refresh as a reset", async () => {
    let finishDelete!: (value: { deleted_model_groups: string[]; freed_bytes: number; state_deleted: boolean }) => void;
    const del = vi.fn(() => new Promise((resolve) => { finishDelete = resolve; }));
    const api = {
      getRuntimeSetupStatus: vi.fn(async () => { throw new Error("offline"); }),
      deleteRuntimeSetupData: del,
      startRuntimeModelDownload: vi.fn(),
    } as unknown as ApiClient;
    await renderPage(packagedSettingsBridge(), { initialStatus: readySettings(false), apiClient: api, ...settingsMount });
    await act(async () => findButton(container, "Delete local runtime data").click());
    await act(async () => container.querySelector<HTMLButtonElement>(".setup-model-confirm .btn-danger")!.click());
    expect(del).toHaveBeenCalledOnce();
    expect(container.querySelector<HTMLButtonElement>('[aria-label="Download Llama 3.2 · 3B"]')!.disabled).toBe(true);
    expect(findButton(container, "Check runtime").disabled).toBe(true);
    expect(container.querySelector(".setup-primary")?.textContent).toBe("Resetting local data…");
    await act(async () => finishDelete({ deleted_model_groups: ["tts-mlx"], freed_bytes: 734003200, state_deleted: true }));
    expect(container.textContent).toContain("Freed 700 MB.");
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("Local runtime data was deleted, but setup status could not be refreshed.");
    expect(container.querySelector<HTMLButtonElement>('[aria-label="Download Llama 3.2 · 3B"]')!.disabled).toBe(false);
  });

  it("hides the delete affordance outside Settings and during an install", async () => {
    const api = { getRuntimeSetupStatus: vi.fn(async () => setupStatus), deleteRuntimeSetupData: vi.fn() } as unknown as ApiClient;
    await renderPage(bridge(), { apiClient: api });
    expect(container.textContent).not.toContain("Delete local runtime data");

    const runningBridge = bridge({ status: vi.fn(async (): Promise<RuntimeSetupRunnerStatus> => ({ lifecycle: "running", profile: "base", modelGroups: [], stage: null, message: null, percent: null, restartRequired: false, error: null })) });
    await act(async () => root.unmount());
    root = createRoot(container);
    await renderPage(runningBridge, { apiClient: api, ...settingsMount });
    await act(async () => undefined);
    expect(container.textContent).not.toContain("Delete local runtime data");
  });

  it("downloads models via the backend and adopts the baked runtime in packaged mode", async () => {
    const packagedBridge = bridge({ environment: vi.fn(async (): Promise<RuntimeSetupEnvironment> => ({ available: false, mode: "packaged", reason: "Profile-baked runtime." })) });
    const bakedStatus: RuntimeSetupStatusResponse = {
      ...setupStatus,
      recommended_profile: "mlx",
      installed_runtime: { profile: "mlx", ready: false, runtime_available: true, model_groups: [{ id: "tts-mlx", runtime_ready: true, assets_ready: false, ready: false }, { id: "cleaner-mlx", runtime_ready: true, assets_ready: false, ready: false }] },
    };
    const readyStatus = { ...bakedStatus, configured: true, state: "ready" as const, ready: true, installed_runtime: null };
    const downloadSnapshot = { lifecycle: "complete" as const, profile: "mlx", model_groups: ["tts-mlx", "cleaner-mlx"], total_models: 1, completed_models: 1, current_model_group: null, error: null };
    const start = vi.fn(async () => ({ ...downloadSnapshot, lifecycle: "running" as const, completed_models: 0, current_model_group: "tts-mlx" }));
    const adopt = vi.fn(async () => readyStatus);
    const onReady = vi.fn();
    const api = {
      getRuntimeSetupStatus: vi.fn(async () => bakedStatus),
      getRuntimeModelDownloadStatus: vi.fn(async () => downloadSnapshot),
      startRuntimeModelDownload: start,
      adoptRuntimeSetup: adopt,
    } as unknown as ApiClient;
    await renderPage(packagedBridge, { apiClient: api, initialStatus: bakedStatus, onReady });

    expect(container.textContent).not.toContain("Install from a source checkout");
    const install = findButton(container, "Install");
    expect(install.disabled).toBe(false);
    await act(async () => { install.click(); install.click(); });
    expect(start).toHaveBeenCalledTimes(1);
    expect(start).toHaveBeenCalledWith(
      { profile: "mlx", model_groups: ["tts-mlx", "cleaner-mlx"], cleaner_cuda_quantization: "4bit" },
      { timeoutMs: 15_000 },
    );
    await act(async () => undefined);
    await act(async () => undefined);
    expect(adopt).toHaveBeenCalledWith(
      { profile: "mlx", model_groups: ["tts-mlx", "cleaner-mlx"], cleaner_cuda_quantization: "4bit" },
      { timeoutMs: 120_000 },
    );
    expect(onReady).toHaveBeenCalledWith(readyStatus);
  });

  it("adopts the resumed download plan once even if the parent rerenders during verification", async () => {
    const packagedBridge = bridge({ environment: vi.fn(async () => ({ available: false, mode: "packaged" as const, reason: "Baked runtime." })) });
    const snapshot = { lifecycle: "running" as const, profile: "mlx", model_groups: ["tts-mlx"], total_models: 1, completed_models: 0, current_model_group: "tts-mlx", error: null };
    let resolveAdopt!: (value: RuntimeSetupStatusResponse) => void;
    const adopt = vi.fn(() => new Promise<RuntimeSetupStatusResponse>((resolve) => { resolveAdopt = resolve; }));
    const api = {
      getRuntimeSetupStatus: vi.fn(async () => setupStatus),
      getRuntimeModelDownloadStatus: vi.fn().mockResolvedValueOnce(snapshot).mockResolvedValue({ ...snapshot, lifecycle: "complete", completed_models: 1 }),
      adoptRuntimeSetup: adopt,
    } as unknown as ApiClient;
    await renderPage(packagedBridge, { apiClient: api });
    expect(adopt).toHaveBeenCalledWith(
      { profile: "mlx", model_groups: ["tts-mlx"], cleaner_cuda_quantization: "4bit" },
      { timeoutMs: 120_000 },
    );
    const onReady = vi.fn();
    await renderPage(packagedBridge, { apiClient: api, onReady });
    expect(adopt).toHaveBeenCalledOnce();
    const ready = { ...setupStatus, configured: true, ready: true, state: "ready" as const, selected_profile: "mlx" as const };
    await act(async () => resolveAdopt(ready));
    expect(onReady).toHaveBeenCalledWith(ready);
  });

  it("offers cancellation while packaged model downloads are running", async () => {
    const packagedBridge = bridge({ environment: vi.fn(async (): Promise<RuntimeSetupEnvironment> => ({ available: false, mode: "packaged", reason: "Profile-baked runtime." })) });
    const bakedStatus: RuntimeSetupStatusResponse = {
      ...setupStatus,
      recommended_profile: "mlx",
      installed_runtime: { profile: "mlx", ready: false, runtime_available: true, model_groups: [{ id: "tts-mlx", runtime_ready: true, assets_ready: false, ready: false }, { id: "cleaner-mlx", runtime_ready: true, assets_ready: false, ready: false }] },
    };
    const runningSnapshot = { lifecycle: "running" as const, profile: "mlx", model_groups: ["tts-mlx"], total_models: 1, completed_models: 0, current_model_group: "tts-mlx", error: null };
    const cancelDownload = vi.fn(async () => ({ ...runningSnapshot, lifecycle: "stopping" as const }));
    const api = {
      getRuntimeSetupStatus: vi.fn(async () => bakedStatus),
      getRuntimeModelDownloadStatus: vi.fn(async () => runningSnapshot),
      startRuntimeModelDownload: vi.fn(async () => runningSnapshot),
      cancelRuntimeModelDownload: cancelDownload,
      adoptRuntimeSetup: vi.fn(),
    } as unknown as ApiClient;
    await renderPage(packagedBridge, { apiClient: api, initialStatus: bakedStatus });

    await act(async () => findButton(container, "Install").click());
    const cancelButton = findButton(container, "Cancel");
    await act(async () => cancelButton.click());

    expect(cancelDownload).toHaveBeenCalledWith({ timeoutMs: 8_000 });
    expect(container.textContent).toContain("Stopping download");
  });

  it.each([[1, 0], [2, 0], [2, 1]])("shows indeterminate progress until a count-only download completes (%s models, %s completed)", async (total, completed) => {
    vi.useFakeTimers();
    try {
      let complete = false;
      const snapshot = {
        lifecycle: "running" as const, profile: "mlx", model_groups: ["tts-mlx", "cleaner-mlx"],
        total_models: total, completed_models: completed, current_model_group: "cleaner-mlx", error: null,
      };
      const api = {
        getRuntimeSetupStatus: vi.fn(async () => setupStatus),
        getRuntimeModelDownloadStatus: vi.fn(async () => complete
          ? { ...snapshot, lifecycle: "complete" as const, completed_models: total, current_model_group: null }
          : snapshot),
        adoptRuntimeSetup: vi.fn(async () => readySettings()),
      } as unknown as ApiClient;
      await renderPage(packagedSettingsBridge(), { apiClient: api });
      const bar = container.querySelector('[role="progressbar"]')!;
      expect(bar.hasAttribute("aria-valuenow")).toBe(false);
      expect(container.querySelector(".setup-progress-message")?.textContent)
        .toBe(`Downloading model ${completed + 1} of ${total}: Llama 3.2 · 3B`);

      complete = true;
      await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
      expect(bar.getAttribute("aria-valuenow")).toBe("100");
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps a slow packaged download cancellable without overlapping polls or consulting the bridge", async () => {
    vi.useFakeTimers();
    try {
      const packagedBridge = bridge({ environment: vi.fn(async () => ({ available: false, mode: "packaged" as const, reason: "Baked runtime." })) });
      const running = { lifecycle: "running" as const, profile: "mlx", model_groups: ["tts-mlx"], total_models: 1, completed_models: 0, current_model_group: "tts-mlx", error: null };
      let resolvePoll!: (value: typeof running) => void;
      const poll = vi.fn().mockResolvedValueOnce(running).mockImplementation(() => new Promise<typeof running>((resolve) => { resolvePoll = resolve; }));
      const cancelDownload = vi.fn(async () => ({ ...running, lifecycle: "cancelled" as const }));
      const api = {
        getRuntimeSetupStatus: vi.fn(async () => setupStatus),
        getRuntimeModelDownloadStatus: poll,
        cancelRuntimeModelDownload: cancelDownload,
      } as unknown as ApiClient;
      await renderPage(packagedBridge, { apiClient: api });
      await act(async () => { await vi.advanceTimersByTimeAsync(4_000); });
      expect(poll).toHaveBeenCalledTimes(2); // Resume check and one unresolved poll.
      expect(packagedBridge.status).not.toHaveBeenCalled();
      await act(async () => findButton(container, "Cancel").click());
      expect(cancelDownload).toHaveBeenCalledOnce();
      await act(async () => { resolvePoll(running); });
      expect(container.querySelector("#setup-progress-heading")).toBeNull();
      expect([...container.querySelectorAll("button")].some((button) => button.textContent === "Cancel")).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it("re-runs the failed backend download when Retry is clicked", async () => {
    const packagedBridge = bridge({ environment: vi.fn(async (): Promise<RuntimeSetupEnvironment> => ({ available: false, mode: "packaged", reason: "Profile-baked runtime." })) });
    const bakedStatus: RuntimeSetupStatusResponse = {
      ...setupStatus,
      recommended_profile: "mlx",
      installed_runtime: { profile: "mlx", ready: false, runtime_available: true, model_groups: [{ id: "tts-mlx", runtime_ready: true, assets_ready: false, ready: false }, { id: "cleaner-mlx", runtime_ready: true, assets_ready: false, ready: false }] },
    };
    const runningSnapshot = { lifecycle: "running" as const, profile: "mlx", model_groups: ["tts-mlx"], total_models: 1, completed_models: 0, current_model_group: "tts-mlx", error: null };
    const idleSnapshot = { ...runningSnapshot, lifecycle: "idle" as const, current_model_group: null };
    const start = vi.fn()
      .mockRejectedValueOnce(new Error("network unreachable"))
      .mockResolvedValue(runningSnapshot);
    const api = {
      getRuntimeSetupStatus: vi.fn(async () => bakedStatus),
      getRuntimeModelDownloadStatus: vi.fn()
        .mockResolvedValueOnce(idleSnapshot)
        .mockResolvedValue(runningSnapshot),
      startRuntimeModelDownload: start,
      adoptRuntimeSetup: vi.fn(),
    } as unknown as ApiClient;
    await renderPage(packagedBridge, { apiClient: api, initialStatus: bakedStatus });

    await act(async () => findButton(container, "Install").click());
    expect(container.textContent).toContain("network unreachable");

    const retry = findButton(container, "Retry");
    await act(async () => retry.click());
    expect(start).toHaveBeenCalledTimes(2);
    expect(start).toHaveBeenLastCalledWith(
      { profile: "mlx", model_groups: ["tts-mlx", "cleaner-mlx"], cleaner_cuda_quantization: "4bit" },
      { timeoutMs: 15_000 },
    );
    expect(container.textContent).not.toContain("network unreachable");
    // The retry keeps the progress surface mounted instead of collapsing back
    // to the plan view, so a second click cannot land on a shifted button.
    expect(container.textContent).toContain("Installation");
  });

  it("re-runs a failed installer start when Retry is clicked in source-checkout mode", async () => {
    const start = vi.fn()
      .mockRejectedValueOnce(new Error("installer crashed"))
      .mockResolvedValue({ lifecycle: "running", profile: "base", modelGroups: [], stage: null, message: null, percent: null, restartRequired: false, error: null } satisfies RuntimeSetupRunnerStatus);
    const runtimeBridge = bridge({ start });
    await renderPage(runtimeBridge);

    await act(async () => findButton(container, "Use library only").click());
    expect(container.textContent).toContain("installer crashed");
    expect(findButton(container, "Copy diagnostics")).toBeTruthy();

    await act(async () => findButton(container, "Retry").click());
    expect(start).toHaveBeenCalledTimes(2);
    expect(start).toHaveBeenLastCalledWith({ profile: "base", modelGroups: [], cleanerCudaQuantization: "4bit" });
    expect(container.textContent).not.toContain("installer crashed");
    expect(container.textContent).not.toContain("Copy diagnostics");
  });

  it("explains an unreachable status check in words and keeps the raw error as a secondary detail", async () => {
    const api = { getRuntimeSetupStatus: vi.fn(() => new Promise<RuntimeSetupStatusResponse>(() => undefined)) } as unknown as ApiClient;
    await renderPage(bridge(), { apiClient: api, initialStatus: null, initialError: "Failed to fetch" });

    const notice = container.querySelector<HTMLElement>(".setup-recovery")!;
    expect(notice.querySelector("strong")?.textContent).toBe("Setup status unavailable");
    expect(notice.querySelector("p")?.textContent).toBe("Orpheus couldn't check this computer's setup. Retry, or use your library for now.");
    expect(notice.querySelector(".error-detail")?.textContent).toBe("Failed to fetch");
  });

  it("guards the status Retry against duplicate clicks while a refresh is in flight", async () => {
    let resolveStatus!: (value: RuntimeSetupStatusResponse) => void;
    const getStatus = vi.fn(() => new Promise<RuntimeSetupStatusResponse>((resolve) => { resolveStatus = resolve; }));
    const api = { getRuntimeSetupStatus: getStatus } as unknown as ApiClient;
    await renderPage(bridge(), { apiClient: api, initialStatus: null, initialError: "backend still starting" });

    const retry = findButton(container, "Retry");
    await act(async () => { retry.click(); retry.click(); });
    expect(getStatus).toHaveBeenCalledTimes(1);
    expect(retry.disabled).toBe(true);

    await act(async () => { resolveStatus(setupStatus); });
    expect(container.textContent).toContain("Library only");
  });

  it("ignores status replies from a replaced backend", async () => {
    let resolveOld!: (value: RuntimeSetupStatusResponse) => void;
    const oldClient = { getRuntimeSetupStatus: vi.fn(() => new Promise<RuntimeSetupStatusResponse>((resolve) => { resolveOld = resolve; })) } as unknown as ApiClient;
    const newClient = { getRuntimeSetupStatus: vi.fn(async () => setupStatus) } as unknown as ApiClient;
    const runtimeBridge = bridge();
    const onReady = vi.fn();
    await renderPage(runtimeBridge, { apiClient: oldClient, initialStatus: null, initialError: "Disconnected", onReady });
    await act(async () => findButton(container, "Retry").click());
    await renderPage(runtimeBridge, { apiClient: newClient, onReady });
    await act(async () => resolveOld({ ...setupStatus, configured: true, ready: true, state: "ready", selected_profile: "base" }));
    expect(onReady).not.toHaveBeenCalled();
    expect(container.textContent).not.toContain("Library-only mode is ready");
    expect(findButton(container, "Customize")).toBeTruthy();
  });

  it("does not restart download polling when an old backend acknowledges Install", async () => {
    const runtimeBridge = packagedSettingsBridge();
    const bakedStatus: RuntimeSetupStatusResponse = {
      ...setupStatus, recommended_profile: "mlx",
      installed_runtime: { profile: "mlx", ready: false, runtime_available: true, model_groups: [
        { id: "tts-mlx", runtime_ready: true, assets_ready: false, ready: false },
        { id: "cleaner-mlx", runtime_ready: true, assets_ready: false, ready: false },
      ] },
    };
    const idle = { lifecycle: "idle", profile: null, model_groups: [], completed_models: 0, total_models: 0 };
    let resolveStart!: () => void;
    const oldClient = {
      getRuntimeSetupStatus: vi.fn(async () => bakedStatus),
      getRuntimeModelDownloadStatus: vi.fn(async () => idle),
      startRuntimeModelDownload: vi.fn(() => new Promise<void>((resolve) => { resolveStart = resolve; })),
    } as unknown as ApiClient;
    const newClient = {
      getRuntimeSetupStatus: vi.fn(async () => bakedStatus),
      getRuntimeModelDownloadStatus: vi.fn(async () => idle),
      adoptRuntimeSetup: vi.fn(),
    } as unknown as ApiClient;
    await renderPage(runtimeBridge, { apiClient: oldClient, initialStatus: bakedStatus });
    await act(async () => findButton(container, "Install").click());
    await renderPage(runtimeBridge, { apiClient: newClient, initialStatus: bakedStatus });
    await act(async () => resolveStart());
    expect(newClient.getRuntimeModelDownloadStatus).toHaveBeenCalledOnce();
    expect(newClient.adoptRuntimeSetup).not.toHaveBeenCalled();
    expect(findButton(container, "Install").disabled).toBe(false);
  });

  it("does not replay a pending download adoption against a replacement backend", async () => {
    const runtimeBridge = packagedSettingsBridge();
    const snapshot = { lifecycle: "running", profile: "mlx", model_groups: ["tts-mlx"], total_models: 1, completed_models: 0, error: null };
    let resolveAdopt!: (value: RuntimeSetupStatusResponse) => void;
    const oldClient = {
      getRuntimeSetupStatus: vi.fn(async () => setupStatus),
      getRuntimeModelDownloadStatus: vi.fn().mockResolvedValueOnce(snapshot).mockResolvedValue({ ...snapshot, lifecycle: "complete", completed_models: 1 }),
      adoptRuntimeSetup: vi.fn(() => new Promise<RuntimeSetupStatusResponse>((resolve) => { resolveAdopt = resolve; })),
    } as unknown as ApiClient;
    const newClient = {
      getRuntimeSetupStatus: vi.fn(async () => setupStatus),
      getRuntimeModelDownloadStatus: vi.fn(async () => ({ ...snapshot, lifecycle: "idle" })),
      adoptRuntimeSetup: vi.fn(async () => setupStatus),
    } as unknown as ApiClient;
    const onReady = vi.fn();
    await renderPage(runtimeBridge, { apiClient: oldClient, onReady });
    expect(oldClient.adoptRuntimeSetup).toHaveBeenCalledOnce();
    await renderPage(runtimeBridge, { apiClient: newClient, onReady });
    await act(async () => resolveAdopt({ ...setupStatus, ready: true, configured: true, state: "ready" }));
    expect(newClient.adoptRuntimeSetup).not.toHaveBeenCalled();
    expect(onReady).not.toHaveBeenCalled();
    expect(container.textContent).not.toContain("Verifying and adopting");
  });

  it("ignores a replaced backend's library-only adoption", async () => {
    const runtimeBridge = packagedSettingsBridge();
    let resolveAdopt!: (value: RuntimeSetupStatusResponse) => void;
    const oldClient = { adoptRuntimeSetup: vi.fn(() => new Promise<RuntimeSetupStatusResponse>((resolve) => { resolveAdopt = resolve; })) } as unknown as ApiClient;
    const newClient = {} as ApiClient;
    const onReady = vi.fn();
    const onContinueLibraryOnly = vi.fn();
    await renderPage(runtimeBridge, { apiClient: oldClient, onReady, onContinueLibraryOnly });
    await act(async () => findButton(container, "Use library only").click());
    await renderPage(runtimeBridge, { apiClient: newClient, onReady, onContinueLibraryOnly });
    await act(async () => resolveAdopt({ ...setupStatus, configured: true, ready: true, state: "ready", selected_profile: "base" }));
    expect(onReady).not.toHaveBeenCalled();
    expect(onContinueLibraryOnly).not.toHaveBeenCalled();
  });

  it("marks progress failed when the download poll loses the backend", async () => {
    vi.useFakeTimers();
    try {
      const packagedBridge = bridge({ environment: vi.fn(async (): Promise<RuntimeSetupEnvironment> => ({ available: false, mode: "packaged", reason: "Profile-baked runtime." })) });
      const bakedStatus: RuntimeSetupStatusResponse = {
        ...setupStatus,
        recommended_profile: "mlx",
        installed_runtime: { profile: "mlx", ready: false, runtime_available: true, model_groups: [{ id: "tts-mlx", runtime_ready: true, assets_ready: false, ready: false }, { id: "cleaner-mlx", runtime_ready: true, assets_ready: false, ready: false }] },
      };
      const runningSnapshot = { lifecycle: "running" as const, profile: "mlx", model_groups: ["tts-mlx"], total_models: 1, completed_models: 0, current_model_group: "tts-mlx", error: null };
      const api = {
        getRuntimeSetupStatus: vi.fn(async () => bakedStatus),
        getRuntimeModelDownloadStatus: vi.fn().mockRejectedValue(new Error("connection refused")),
        startRuntimeModelDownload: vi.fn(async () => runningSnapshot),
        adoptRuntimeSetup: vi.fn(),
      } as unknown as ApiClient;
      await renderPage(packagedBridge, { apiClient: api, initialStatus: bakedStatus });

      await act(async () => findButton(container, "Install").click());
      await act(async () => { await vi.advanceTimersByTimeAsync(9_000); });
      expect(container.textContent).toContain("Lost connection to the installer");
      expect(() => findButton(container, "Retry")).not.toThrow();
    } finally {
      vi.useRealTimers();
    }
  });

  it("surfaces a failed backend download and re-enables the install action", async () => {
    const packagedBridge = bridge({ environment: vi.fn(async (): Promise<RuntimeSetupEnvironment> => ({ available: false, mode: "packaged", reason: "Profile-baked runtime." })) });
    const bakedStatus: RuntimeSetupStatusResponse = {
      ...setupStatus,
      recommended_profile: "mlx",
      installed_runtime: { profile: "mlx", ready: false, runtime_available: true, model_groups: [{ id: "tts-mlx", runtime_ready: true, assets_ready: false, ready: false }, { id: "cleaner-mlx", runtime_ready: true, assets_ready: false, ready: false }] },
    };
    const failedSnapshot = { lifecycle: "failed" as const, profile: "mlx", model_groups: ["tts-mlx"], total_models: 1, completed_models: 0, current_model_group: null, error: "disk full" };
    const adopt = vi.fn();
    const api = {
      getRuntimeSetupStatus: vi.fn(async () => bakedStatus),
      getRuntimeModelDownloadStatus: vi.fn(async () => failedSnapshot),
      startRuntimeModelDownload: vi.fn(async () => ({ ...failedSnapshot, lifecycle: "running" as const })),
      adoptRuntimeSetup: adopt,
    } as unknown as ApiClient;
    await renderPage(packagedBridge, { apiClient: api, initialStatus: bakedStatus });

    await act(async () => findButton(container, "Install").click());
    await act(async () => undefined);
    await act(async () => undefined);
    expect(container.textContent).toContain("disk full");
    expect(findButton(container, "Copy diagnostics")).toBeTruthy();
    expect(adopt).not.toHaveBeenCalled();
    expect(findButton(container, "Install").disabled).toBe(false);
  });
});
