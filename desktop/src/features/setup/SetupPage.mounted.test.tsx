// @vitest-environment happy-dom

import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import type { RuntimeSetupStatusResponse } from "../../types/api";
import { SetupPage, type SetupPageBridge } from "./SetupPage";
import type { RuntimeSetupEnvironment, RuntimeSetupRunnerStatus } from "./setupBridge";
import type { SetupProgressEvent } from "./setupModel";

const setupStatus: RuntimeSetupStatusResponse = {
  schema_version: 1, configured: false, state: "unconfigured", state_error: null,
  selected_profile: null, selected_model_groups: [],
  packages: { ready: false, missing: [], incompatible: [] },
  probe: { ready: false, checks: [] }, ready: false, restart_required: false,
  profiles: [
    { id: "base", label: "Library only", description: "Library.", compatible: true, reason: null, model_groups: [], risk: null },
    { id: "mlx", label: "Apple Silicon (MLX)", description: "Local.", compatible: true, reason: null, model_groups: ["tts-mlx", "cleaner-mlx"], risk: null },
    { id: "cuda", label: "NVIDIA CUDA", description: "GPU.", compatible: false, reason: "Unavailable.", model_groups: [], risk: null },
  ], risk: null, backend: { instance_id: "one", status: "running" },
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

  async function renderPage(runtimeBridge: SetupPageBridge, props: Partial<Parameters<typeof SetupPage>[0]> = {}) {
    const api = props.apiClient ?? { getRuntimeSetupStatus: vi.fn(async () => setupStatus) } as unknown as ApiClient;
    await act(async () => {
      root.render(<SetupPage apiClient={api} initialStatus={setupStatus} initialError={null} onReady={vi.fn()} onContinueLibraryOnly={vi.fn()} onReconnectBackend={vi.fn()} bridge={runtimeBridge} {...props} />);
    });
    return api;
  }

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
  function cudaCatalogStatus(): RuntimeSetupStatusResponse {
    const groups = ["tts-cpu-cuda", "cleaner-cuda", "cleaner-cuda-large"];
    return {
      ...setupStatus, recommended_profile: "cuda",
      profiles: setupStatus.profiles.map((profile) => profile.id === "cuda" ? { ...profile, compatible: true, reason: null, model_groups: groups } : profile),
      installed_runtime: { profile: "cuda", runtime_available: true, ready: false, model_groups: groups.map((id) => ({ id, runtime_ready: true, assets_ready: false, ready: false })) },
    };
  }

  it.each([false, true])("scrolls installation into view once, respecting reduced motion (%s)", async (reducedMotion) => {
    const scroll = vi.spyOn(Element.prototype, "scrollIntoView").mockImplementation(() => undefined);
    vi.spyOn(window, "matchMedia").mockReturnValue({ matches: reducedMotion } as MediaQueryList);
    let onProgress!: (event: SetupProgressEvent) => void;
    const runtimeBridge = bridge({ listen: vi.fn(async (listener) => { onProgress = listener; return vi.fn(); }) });
    await renderPage(runtimeBridge);
    expect(scroll).not.toHaveBeenCalled();
    await act(async () => container.querySelector<HTMLButtonElement>(".setup-primary")!.click());
    expect(scroll).toHaveBeenCalledExactlyOnceWith({ behavior: reducedMotion ? "instant" : "smooth", block: "start" });
    expect(scroll.mock.instances[0]).toBe(container.querySelector(".setup-progress"));
    const bar = container.querySelector('[role="progressbar"]');
    expect(bar?.classList.contains("pbar-active")).toBe(true);
    expect(bar?.hasAttribute("aria-valuenow")).toBe(false);
    await act(async () => onProgress({ stage: "packages", message: "Installing", percent: 25 } as SetupProgressEvent));
    expect(scroll).toHaveBeenCalledTimes(1);
  });

  it("shows model and precision choices on the first page and downloads only the chosen pair", async () => {
    const start = vi.fn(async () => ({ lifecycle: "running" }));
    await renderPage(packagedSettingsBridge(), { initialStatus: cudaCatalogStatus(), apiClient: { startRuntimeModelDownload: start } as unknown as ApiClient });
    expect(container.textContent).toContain("Quantization stores model weights");
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
    expect(adopt).toHaveBeenCalledExactlyOnceWith({ profile: "cuda", model_groups: ["tts-cpu-cuda", "cleaner-cuda"], cleaner_cuda_quantization: "fp16", accept_pytorch_risk: false }, { timeoutMs: 120_000 });
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
    expect(voiceRow.textContent).toContain("Already downloaded");
    const noCleaner = () => container.querySelector<HTMLInputElement>('input[name="setup-cleaner"]')!;
    await act(async () => noCleaner().click());
    const refreshed = { ...initialStatus, installed_runtime: { ...initialStatus.installed_runtime! } };
    await renderPage(runtimeBridge, { initialStatus: refreshed, apiClient });
    expect(noCleaner().checked).toBe(true);
    await act(async () => findButton(container, "Continue").click());
    expect(adopt).toHaveBeenCalledExactlyOnceWith({ profile: "cuda", model_groups: ["tts-cpu-cuda"], cleaner_cuda_quantization: "4bit", accept_pytorch_risk: false }, { timeoutMs: 120_000 });
    const primary = container.querySelector<HTMLButtonElement>(".setup-primary")!;
    expect(primary.disabled).toBe(true);
    expect(primary.textContent).toContain("Continue…");
    expect(primary.querySelector(".braille-spinner")).not.toBeNull();
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
    await renderPage(packagedSettingsBridge(), { initialStatus, apiClient: api, settingsEntry: true, onReady, onExitSettings: vi.fn() });
    expect(findButton(container, "Done")).toBeTruthy();
    if (profile === "base") {
      await act(async () => container.querySelector<HTMLInputElement>('input[value="base"]')!.click());
    } else {
      await act(async () => [...container.querySelectorAll<HTMLInputElement>('input')]
        .find((input) => input.parentElement?.textContent?.includes("Llama 3.2 · 3B"))!.click());
    }
    const save = findButton(container, "Save runtime settings");
    await act(async () => { save.click(); save.click(); });
    expect(adopt).toHaveBeenCalledTimes(1);
    expect(adopt).toHaveBeenCalledWith({ profile, model_groups: profile === "base" ? [] : ["tts-mlx", "cleaner-mlx"], accept_pytorch_risk: false, cleaner_cuda_quantization: "4bit" }, { timeoutMs: 120_000 });
    expect(findButton(container, "Back to dashboard").disabled).toBe(true);
    expect(container.querySelector(".setup-primary")!.textContent).toContain("Saving…");
    expect(container.querySelector(".setup-primary .braille-spinner")).not.toBeNull();
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
      settingsEntry: true, apiClient: { adoptRuntimeSetup: adopt } as unknown as ApiClient,
    });
    await act(async () => container.querySelector<HTMLInputElement>('input[value="mlx"]')!.click());
    await act(async () => findButton(container, "Save runtime settings").click());
    expect(adopt).toHaveBeenCalledWith({ profile: "mlx", model_groups: ["tts-mlx", "cleaner-mlx"], accept_pytorch_risk: false, cleaner_cuda_quantization: "4bit" }, { timeoutMs: 120_000 });
    expect(runtimeBridge.start).not.toHaveBeenCalled();
  });

  it("keeps a failed Settings save retryable without silently switching to library-only", async () => {
    const adopt = vi.fn().mockRejectedValueOnce(new Error("Backend disconnected")).mockResolvedValue(readySettings());
    const onReady = vi.fn();
    const onContinueLibraryOnly = vi.fn();
    await renderPage(packagedSettingsBridge(), { initialStatus: readySettings(), settingsEntry: true, apiClient: { adoptRuntimeSetup: adopt } as unknown as ApiClient, onReady, onContinueLibraryOnly });
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
    await renderPage(packagedSettingsBridge(), { initialStatus: readySettings(false), settingsEntry: true, apiClient: { startRuntimeModelDownload: start, adoptRuntimeSetup: adopt } as unknown as ApiClient });
    await act(async () => [...container.querySelectorAll<HTMLInputElement>('input')]
      .find((input) => input.parentElement?.textContent?.includes("Llama 3.2 · 3B"))!.click());
    await act(async () => container.querySelector<HTMLButtonElement>('.setup-primary')!.click());
    expect(start).toHaveBeenCalledWith({ profile: "mlx", model_groups: ["tts-mlx", "cleaner-mlx"], cleaner_cuda_quantization: "4bit" }, { timeoutMs: 15_000 });
    expect(adopt).not.toHaveBeenCalled();
  });

  it("does not offer downloads for an unavailable cleaner runtime", async () => {
    const initialStatus = readySettings(false);
    initialStatus.installed_runtime!.model_groups[1].runtime_ready = false;
    await renderPage(packagedSettingsBridge(), { initialStatus, settingsEntry: true });
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
    await renderPage(packagedSettingsBridge(), { initialStatus, settingsEntry: true, apiClient: api, onReady });
    await act(async () => container.querySelector<HTMLInputElement>('input[value="base"]')!.click());
    await act(async () => findButton(container, "Check runtime").click());
    expect(api.getRuntimeSetupStatus).toHaveBeenCalledOnce();
    expect(container.querySelector<HTMLInputElement>('input[value="base"]')!.checked).toBe(true);
    expect(findButton(container, "Save runtime settings").disabled).toBe(false);
    expect(onReady).not.toHaveBeenCalled();
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
    await renderPage(packagedSettingsBridge(), { initialStatus: currentStatus, settingsEntry: true, apiClient: api });
    const deleteButton = container.querySelector<HTMLButtonElement>('[aria-label="Delete Llama 3.2 · 3B"]')!;
    await act(async () => deleteButton.click());
    expect(remove).not.toHaveBeenCalled();
    await act(async () => findButton(container, "Keep model").click());
    expect(container.textContent).not.toContain("Your books and audio will stay");
    await act(async () => deleteButton.click());
    await act(async () => { findButton(container, "Delete model").click(); findButton(container, "Delete model").click(); });
    expect(remove).toHaveBeenCalledExactlyOnceWith("cleaner-mlx", { timeoutMs: 120_000 });
    expect(container.querySelector<HTMLFieldSetElement>('fieldset')!.disabled).toBe(true);
    removed = true;
    currentStatus = readySettings(false);
    await act(async () => resolveDelete({ deleted_model_groups: ["cleaner-mlx"], freed_bytes: 2_000_000_000, state_deleted: false }));
    expect(container.textContent).toContain("Llama 3.2 · 3B deleted. Freed");
    expect(container.querySelector('[aria-label="Delete Kokoro TTS"]')).not.toBeNull();
    const install = container.querySelector<HTMLButtonElement>('[aria-label="Install Llama 3.2 · 3B"]')!;
    expect(install.textContent).toBe("Install again");
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
    await renderPage(packagedSettingsBridge(), { initialStatus: readySettings(false), settingsEntry: true, apiClient: api, onReady });
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Install Llama 3.2 · 3B"]')!.click());
    await act(async () => undefined);
    expect(adopt).not.toHaveBeenCalled();
    expect(api.getRuntimeSetupStatus).toHaveBeenCalled();
    expect(onReady).toHaveBeenCalledWith(expect.objectContaining({ selected_model_groups: [expect.objectContaining({ id: "tts-mlx" })] }));
    expect(container.textContent).toContain("Model installed");
  });

  it("keeps an installed model and deletion controls visible after a delete failure", async () => {
    const remove = vi.fn(async () => { throw new Error("Wait for the current conversion to finish"); });
    const api = { deleteRuntimeModel: remove } as unknown as ApiClient;
    await renderPage(packagedSettingsBridge(), { initialStatus: readySettings(), settingsEntry: true, apiClient: api });
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Delete Llama 3.2 · 3B"]')!.click());
    await act(async () => findButton(container, "Delete model").click());
    expect(container.textContent).toContain("Wait for the current conversion to finish");
    expect(container.querySelector<HTMLButtonElement>('[aria-label="Delete Llama 3.2 · 3B"]')!.disabled).toBe(false);
    expect(container.querySelector('[aria-label="Install Llama 3.2 · 3B"]')).toBeNull();
    expect(findButton(container, "Done").disabled).toBe(false);
  });

  it("explains why an empty local model selection cannot be saved", async () => {
    await renderPage(packagedSettingsBridge(), { initialStatus: readySettings(), settingsEntry: true });
    await act(async () => [...container.querySelectorAll<HTMLInputElement>('input')]
      .find((input) => input.parentElement?.textContent?.includes("Kokoro TTS"))!.click());
    expect(findButton(container, "Continue").disabled).toBe(true);
    expect(container.textContent).toContain("Select the Kokoro voice model");
  });

  it("requires Kokoro even when a cleaner remains selected", async () => {
    const start = vi.fn();
    await renderPage(packagedSettingsBridge(), { initialStatus: cudaCatalogStatus(), apiClient: { startRuntimeModelDownload: start } as unknown as ApiClient });
    await act(async () => container.querySelector<HTMLInputElement>('.setup-model-list input[type="checkbox"]')!.click());
    expect(container.querySelector<HTMLInputElement>('input[name="setup-cleaner"]:checked')?.parentElement?.textContent).toContain("1.5B");
    expect(findButton(container, "Continue").disabled).toBe(true);
    expect(container.textContent).toContain("Select the Kokoro voice model");
    expect(start).not.toHaveBeenCalled();
  });

  it("shows the compatibility reason directly on disabled runtime cards", async () => {
    await renderPage(bridge(), { settingsEntry: true });
    const cuda = container.querySelector<HTMLInputElement>('input[value="cuda"]')!;
    expect(cuda.disabled).toBe(true);
    expect(cuda.closest("label")?.textContent).toContain("Not compatible: Unavailable.");
  });

  it("lets Settings close while a resumed download is stopping", async () => {
    const exit = vi.fn();
    const snapshot = { lifecycle: "stopping", profile: "mlx", model_groups: ["tts-mlx"], total_models: 1, completed_models: 0 };
    await renderPage(packagedSettingsBridge(), { settingsEntry: true, onExitSettings: exit, apiClient: { getRuntimeModelDownloadStatus: vi.fn(async () => snapshot) } as unknown as ApiClient });
    expect(container.textContent).toContain("Stopping download");
    expect(findButton(container, "Back to dashboard").disabled).toBe(false);
    await act(async () => findButton(container, "Back to dashboard").click());
    expect(exit).toHaveBeenCalledOnce();
  });

  it("shows damaged models and preserves Delete after a partial removal", async () => {
    const inventory = { models: [{ id: "tts-mlx", downloaded: true, size_bytes: 42, damaged: true }] };
    const api = { getRuntimeModelInventory: vi.fn(async () => inventory), getRuntimeSetupStatus: vi.fn(async () => readySettings()), deleteRuntimeModel: vi.fn(async () => ({ freed_bytes: 10, residual_model_groups: ["tts-mlx"], message: "Couldn't delete some files — they may be in use." })) } as unknown as ApiClient;
    await renderPage(packagedSettingsBridge(), { settingsEntry: true, initialStatus: readySettings(), apiClient: api });
    expect(container.textContent).toContain("Damaged");
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Delete Kokoro TTS"]')!.click());
    await act(async () => findButton(container, "Delete model").click());
    expect(container.textContent).toContain("Couldn't delete some files");
    expect(container.querySelector('[aria-label="Delete Kokoro TTS"]')).not.toBeNull();
  });

  it("ignores a save response from a disconnected backend", async () => {
    const initialStatus = readySettings();
    let resolveSave!: (status: RuntimeSetupStatusResponse) => void;
    const oldApi = { adoptRuntimeSetup: vi.fn(() => new Promise<RuntimeSetupStatusResponse>((resolve) => { resolveSave = resolve; })) } as unknown as ApiClient;
    const newApi = {} as ApiClient;
    const runtimeBridge = packagedSettingsBridge();
    const onReady = vi.fn();
    await renderPage(runtimeBridge, { initialStatus, settingsEntry: true, apiClient: oldApi, onReady });
    await act(async () => container.querySelector<HTMLInputElement>('input[value="base"]')!.click());
    await act(async () => findButton(container, "Save runtime settings").click());
    await renderPage(runtimeBridge, { initialStatus, settingsEntry: true, apiClient: newApi, onReady });
    await act(async () => resolveSave({ ...initialStatus, selected_profile: "base", selected_model_groups: [] }));
    expect(onReady).not.toHaveBeenCalled();
    expect(container.querySelector<HTMLInputElement>('input[value="mlx"]')!.checked).toBe(true);
    expect(findButton(container, "Done").disabled).toBe(false);
  });

  it("uses native profile/model controls, exact payload, duplicate guard, focus, and unsubscribe", async () => {
    let resolveStart!: (value: Awaited<ReturnType<SetupPageBridge["start"]>>) => void;
    const start = vi.fn(() => new Promise<Awaited<ReturnType<SetupPageBridge["start"]>>>((resolve) => { resolveStart = resolve; }));
    const unlisten = vi.fn();
    const runtimeBridge = bridge({ start, listen: vi.fn(async () => unlisten) });
    await renderPage(runtimeBridge);

    await act(async () => findButton(container, "Change runtime").click());
    const mlx = container.querySelector<HTMLInputElement>('input[type="radio"][value="mlx"]')!;
    await act(async () => mlx.click());
    const cleaner = [...container.querySelectorAll<HTMLInputElement>('input[name="setup-cleaner"]')]
      .find((input) => input.parentElement?.textContent?.includes("No local cleaner"))!;
    await act(async () => cleaner.click());
    const install = findButton(container, "Install");
    await act(async () => { install.click(); install.click(); });
    expect(start).toHaveBeenCalledTimes(1);
    expect(start).toHaveBeenCalledWith({ profile: "mlx", modelGroups: ["tts-mlx"], acceptPytorchRisk: false, cleanerCudaQuantization: "4bit" });
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
    const retry = findButton(container, "Retry progress connection");
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

  it("cancels an authoritative running setup", async () => {
    const runtimeBridge = bridge({ status: vi.fn(async (): Promise<RuntimeSetupRunnerStatus> => ({ lifecycle: "running", profile: "base", modelGroups: [], stage: null, message: null, percent: null, restartRequired: false, error: null })) });
    await renderPage(runtimeBridge);
    await act(async () => findButton(container, "Cancel").click());
    expect(runtimeBridge.cancel).toHaveBeenCalledOnce();
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
      expect(findButton(container, "Cancel")).toBeTruthy();
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
    expect(container.querySelector('[role="progressbar"]')?.classList.contains("pbar-done")).toBe(true);
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

    expect(container.textContent).toContain("Installed MLX runtime ready");
    await act(async () => container.querySelector<HTMLInputElement>('input[name="setup-cleaner"]')!.click());
    const cta = findButton(container, "Continue");
    expect(cta.disabled).toBe(false);
    await act(async () => cta.click());
    expect(adopt).toHaveBeenCalledWith(
      { profile: "mlx", model_groups: ["tts-mlx"], accept_pytorch_risk: false, cleaner_cuda_quantization: "4bit" },
      { timeoutMs: 120_000 },
    );
    expect(onReady).toHaveBeenCalledWith(readyStatus);
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

  it("requires the risk acknowledgement before adopting an installed CUDA runtime", async () => {
    const cudaInstalled: RuntimeSetupStatusResponse = {
      ...setupStatus,
      recommended_profile: "cuda",
      profiles: setupStatus.profiles.map((profile) => profile.id === "cuda"
        ? { ...profile, compatible: true, reason: null, model_groups: ["tts-cpu-cuda"], risk: { id: "pytorch", summary: "Use trusted models only.", accepted: false } }
        : profile),
      installed_runtime: { profile: "cuda", ready: true, runtime_available: true, model_groups: [{ id: "tts-cpu-cuda", runtime_ready: true, assets_ready: true, ready: true }] },
    };
    const readyStatus = { ...cudaInstalled, configured: true, state: "ready" as const, ready: true, installed_runtime: null };
    const adopt = vi.fn(async () => readyStatus);
    const api = { getRuntimeSetupStatus: vi.fn(async () => cudaInstalled), adoptRuntimeSetup: adopt } as unknown as ApiClient;
    await renderPage(bridge(), { apiClient: api, initialStatus: cudaInstalled });

    expect(findButton(container, "Continue").disabled).toBe(true);
    const risk = container.querySelector<HTMLInputElement>('.setup-risk input[type="checkbox"]')!;
    await act(async () => risk.click());
    expect(findButton(container, "Continue").disabled).toBe(false);
    await act(async () => findButton(container, "Continue").click());
    expect(adopt).toHaveBeenCalledWith(
      { profile: "cuda", model_groups: ["tts-cpu-cuda"], accept_pytorch_risk: true, cleaner_cuda_quantization: "4bit" },
      { timeoutMs: 120_000 },
    );
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
      { profile: "base", model_groups: [], accept_pytorch_risk: false },
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

  it("shows a configured packaged runtime as ready and exits settings without reinstalling", async () => {
    const ready = readySettings();
    const exit = vi.fn();
    const packagedBridge = bridge({ environment: vi.fn(async () => ({ available: false, mode: "packaged" as const, reason: "Packaged runtime" })) });
    await renderPage(packagedBridge, { initialStatus: ready, settingsEntry: true, onExitSettings: exit });
    expect(container.textContent).toContain("Apple Silicon (MLX) runtime is ready");
    expect(container.textContent).not.toContain("Audio creation is unavailable");
    expect(container.querySelector(".setup-environment")).toBeNull();
    expect(container.querySelector(".setup-intro #setup-back-btn")).not.toBeNull();
    await act(async () => findButton(container, "Done").click());
    expect(exit).toHaveBeenCalledOnce();
    expect(packagedBridge.start).not.toHaveBeenCalled();
  });

  it("arms then confirms deletion of local runtime data and reports freed space", async () => {
    const del = vi.fn(async () => ({ deleted_model_groups: ["tts-mlx"], freed_bytes: 734003200, state_deleted: true }));
    const api = { getRuntimeSetupStatus: vi.fn(async () => setupStatus), deleteRuntimeSetupData: del } as unknown as ApiClient;
    await renderPage(bridge(), { apiClient: api, settingsEntry: true });

    expect(container.querySelector("h1")?.textContent).toBe("Runtime setup");
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
    expect(document.activeElement?.textContent).toBe("Keep");
    await act(async () => findButton(container, "Delete local runtime data").click());
    expect(del).not.toHaveBeenCalled();
    await act(async () => {
      const confirm = container.querySelector<HTMLButtonElement>(".setup-model-confirm .btn-danger")!;
      confirm.click();
      confirm.click();
    });
    expect(del).toHaveBeenCalledOnce();
    expect(container.textContent).toContain("Freed 700 MB.");
  });

  it("hides the delete affordance outside Settings and during an install", async () => {
    const api = { getRuntimeSetupStatus: vi.fn(async () => setupStatus), deleteRuntimeSetupData: vi.fn() } as unknown as ApiClient;
    await renderPage(bridge(), { apiClient: api });
    expect(container.textContent).not.toContain("Delete local runtime data");

    const runningBridge = bridge({ status: vi.fn(async (): Promise<RuntimeSetupRunnerStatus> => ({ lifecycle: "running", profile: "base", modelGroups: [], stage: null, message: null, percent: null, restartRequired: false, error: null })) });
    await act(async () => root.unmount());
    root = createRoot(container);
    await renderPage(runningBridge, { apiClient: api, settingsEntry: true });
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
      { profile: "mlx", model_groups: ["tts-mlx", "cleaner-mlx"], accept_pytorch_risk: false, cleaner_cuda_quantization: "4bit" },
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
      { profile: "mlx", model_groups: ["tts-mlx"], accept_pytorch_risk: false, cleaner_cuda_quantization: "4bit" },
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
    expect(start).toHaveBeenLastCalledWith({ profile: "base", modelGroups: [], acceptPytorchRisk: false, cleanerCudaQuantization: "4bit" });
    expect(container.textContent).not.toContain("installer crashed");
    expect(container.textContent).not.toContain("Copy diagnostics");
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
    expect(findButton(container, "Change runtime")).toBeTruthy();
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
