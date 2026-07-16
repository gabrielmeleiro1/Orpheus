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
});
