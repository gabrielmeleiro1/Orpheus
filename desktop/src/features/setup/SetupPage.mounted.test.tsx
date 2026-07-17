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
  });

  async function renderPage(runtimeBridge: SetupPageBridge, props: Partial<Parameters<typeof SetupPage>[0]> = {}) {
    const api = props.apiClient ?? { getRuntimeSetupStatus: vi.fn(async () => setupStatus) } as unknown as ApiClient;
    await act(async () => {
      root.render(<SetupPage apiClient={api} initialStatus={setupStatus} initialError={null} onReady={vi.fn()} onContinueLibraryOnly={vi.fn()} onReconnectBackend={vi.fn()} bridge={runtimeBridge} {...props} />);
    });
    return api;
  }

  it("uses native profile/model controls, exact payload, duplicate guard, focus, and unsubscribe", async () => {
    let resolveStart!: (value: Awaited<ReturnType<SetupPageBridge["start"]>>) => void;
    const start = vi.fn(() => new Promise<Awaited<ReturnType<SetupPageBridge["start"]>>>((resolve) => { resolveStart = resolve; }));
    const unlisten = vi.fn();
    const runtimeBridge = bridge({ start, listen: vi.fn(async () => unlisten) });
    await renderPage(runtimeBridge);

    const mlx = container.querySelector<HTMLInputElement>('input[type="radio"][value="mlx"]')!;
    await act(async () => mlx.click());
    const cleaner = [...container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')]
      .find((input) => input.parentElement?.textContent?.includes("Qwen Cleaner"))!;
    await act(async () => cleaner.click());
    const install = findButton(container, "Install selected runtime");
    await act(async () => { install.click(); install.click(); });
    expect(start).toHaveBeenCalledTimes(1);
    expect(start).toHaveBeenCalledWith({ profile: "mlx", modelGroups: ["tts-mlx", "cleaner-mlx"], acceptPytorchRisk: false });
    expect(document.activeElement?.id).toBe("setup-progress-heading");
    resolveStart({ lifecycle: "running", profile: "mlx", modelGroups: ["tts-mlx", "cleaner-mlx"], stage: null, message: null, percent: null, restartRequired: false, error: null });
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

    const install = findButton(container, "Use library-only mode");
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
    const install = findButton(container, "Use library-only mode");
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
    const retry = findButton(container, "Retry progress connection");
    expect(findButton(container, "Use library-only mode").disabled).toBe(true);

    await act(async () => retry.click());
    await act(async () => undefined);
    expect(listen).toHaveBeenCalledTimes(2);
    expect(findButton(container, "Use library-only mode").disabled).toBe(false);
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
    expect(container.querySelector("progress")?.getAttribute("value")).toBe("92");
    expect(container.textContent).toContain("Verifying setup");
    expect(container.querySelectorAll(".setup-stage.is-complete")).toHaveLength(2);

    await act(async () => {
      progressListeners[1]({ event: "stage_started", stage: "verify", message: "Verifying setup", percent: 92 });
    });
    expect(container.querySelector("progress")?.getAttribute("value")).toBe("92");
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
    expect(findButton(container, "Use library-only mode").disabled).toBe(true);
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
    expect(container.querySelector("progress")?.getAttribute("value")).toBe("100");
  });

  it("focuses the setup heading when the page mounts", async () => {
    await renderPage(bridge());
    expect(document.activeElement?.id).toBe("setup-heading");
  });

  it("never registers Tauri mutations when unavailable and keeps library-only recovery", async () => {
    const runtimeBridge = bridge({ environment: vi.fn(async (): Promise<RuntimeSetupEnvironment> => ({ available: false, mode: "browser", reason: "Desktop source checkout required." })) });
    const continueLibrary = vi.fn();
    await renderPage(runtimeBridge, { onContinueLibraryOnly: continueLibrary });
    expect(container.textContent).toContain("Desktop source checkout required.");
    expect(container.textContent).toContain(".venv/bin/python scripts/setup_runtime.py --profile base");
    expect(runtimeBridge.listen).not.toHaveBeenCalled();
    expect(runtimeBridge.status).not.toHaveBeenCalled();
    expect(findButton(container, "Use library-only mode").disabled).toBe(true);
    await act(async () => findButton(container, "Continue in library-only mode").click());
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
    await act(async () => findButton(container, "Continue in library-only mode").click());
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
    await act(async () => findButton(container, "Continue in library-only mode").click());
    expect(continueLibrary).toHaveBeenCalledOnce();
  });

  it("adopts an installed-ready runtime and reports the fresh status", async () => {
    const installedStatus: RuntimeSetupStatusResponse = {
      ...setupStatus,
      recommended_profile: "mlx",
      installed_runtime: { profile: "mlx", ready: true, model_groups: [{ id: "tts-mlx", ready: true }, { id: "cleaner-mlx", ready: false }] },
    };
    const readyStatus = { ...installedStatus, configured: true, state: "ready" as const, ready: true, installed_runtime: null };
    const adopt = vi.fn(async () => readyStatus);
    const onReady = vi.fn();
    const api = { getRuntimeSetupStatus: vi.fn(async () => installedStatus), adoptRuntimeSetup: adopt } as unknown as ApiClient;
    await renderPage(bridge(), { apiClient: api, initialStatus: installedStatus, onReady });

    expect(container.textContent).toContain("Installed MLX runtime ready");
    const cta = findButton(container, "Continue with installed runtime");
    expect(cta.disabled).toBe(false);
    await act(async () => cta.click());
    expect(adopt).toHaveBeenCalledWith(
      { profile: "mlx", model_groups: ["tts-mlx"], accept_pytorch_risk: false },
      { timeoutMs: 15_000 },
    );
    expect(onReady).toHaveBeenCalledWith(readyStatus);
  });

  it("surfaces an adopt failure without reporting ready", async () => {
    const installedStatus: RuntimeSetupStatusResponse = {
      ...setupStatus,
      installed_runtime: { profile: "mlx", ready: true, model_groups: [{ id: "tts-mlx", ready: true }] },
    };
    const adopt = vi.fn(async () => { throw new Error("API request failed (409): CUDA capability probe failed"); });
    const onReady = vi.fn();
    const api = { getRuntimeSetupStatus: vi.fn(async () => installedStatus), adoptRuntimeSetup: adopt } as unknown as ApiClient;
    await renderPage(bridge(), { apiClient: api, initialStatus: installedStatus, onReady });

    await act(async () => findButton(container, "Continue with installed runtime").click());
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
      installed_runtime: { profile: "cuda", ready: true, model_groups: [{ id: "tts-cpu-cuda", ready: true }] },
    };
    const readyStatus = { ...cudaInstalled, configured: true, state: "ready" as const, ready: true, installed_runtime: null };
    const adopt = vi.fn(async () => readyStatus);
    const api = { getRuntimeSetupStatus: vi.fn(async () => cudaInstalled), adoptRuntimeSetup: adopt } as unknown as ApiClient;
    await renderPage(bridge(), { apiClient: api, initialStatus: cudaInstalled });

    expect(findButton(container, "Continue with installed runtime").disabled).toBe(true);
    const risk = container.querySelector<HTMLInputElement>('.setup-risk input[type="checkbox"]')!;
    await act(async () => risk.click());
    expect(findButton(container, "Continue with installed runtime").disabled).toBe(false);
    await act(async () => findButton(container, "Continue with installed runtime").click());
    expect(adopt).toHaveBeenCalledWith(
      { profile: "cuda", model_groups: ["tts-cpu-cuda"], accept_pytorch_risk: true },
      { timeoutMs: 15_000 },
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

    await act(async () => findButton(container, "Continue in library-only mode").click());
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

    await act(async () => findButton(container, "Continue in library-only mode").click());
    expect(adopt).toHaveBeenCalledOnce();
    expect(continueLibrary).toHaveBeenCalledOnce();
  });

  it("arms then confirms deletion of local runtime data and reports freed space", async () => {
    const del = vi.fn(async () => ({ deleted_model_groups: ["tts-mlx"], freed_bytes: 734003200, state_deleted: true }));
    const api = { getRuntimeSetupStatus: vi.fn(async () => setupStatus), deleteRuntimeSetupData: del } as unknown as ApiClient;
    await renderPage(bridge(), { apiClient: api, settingsEntry: true });

    await act(async () => findButton(container, "Delete local runtime data").click());
    expect(del).not.toHaveBeenCalled();
    expect(findButton(container, "Confirm delete")).toBeTruthy();
    await act(async () => findButton(container, "Confirm delete").click());
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
});
