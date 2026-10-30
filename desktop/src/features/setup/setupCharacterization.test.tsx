// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient, type ApiClient } from "../../api/client";
import type { RuntimeSetupStatusResponse } from "../../types/api";
import { SetupPage, type SetupPageBridge } from "./SetupPage";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function availableStatus(): RuntimeSetupStatusResponse {
  return {
    schema_version: 1, configured: false, state: "unconfigured", state_error: null,
    selected_profile: null, recommended_profile: "mlx", selected_model_groups: [],
    packages: { ready: true, missing: [], incompatible: [] },
    probe: { ready: true, checks: [] }, ready: false, restart_required: false,
    profiles: [
      { id: "base", label: "Library only", description: "Library", compatible: true, reason: null, model_groups: [], risk: null },
      { id: "mlx", label: "Apple Silicon (MLX)", description: "Local", compatible: true, reason: null, model_groups: ["tts-mlx"], risk: null },
    ],
    installed_runtime: { profile: "mlx", ready: true, runtime_available: true, model_groups: [
      { id: "tts-mlx", runtime_ready: true, assets_ready: true, ready: true },
    ] },
    risk: null, backend: { instance_id: "backend", status: "running" },
  };
}

function checkingStatus(state: "pending" | "timeout"): RuntimeSetupStatusResponse {
  const status = availableStatus();
  return { ...status, checking: true, state: "checking", probe: { ready: false, checks: [
    { id: "tts-mlx", ready: false, state },
  ] }, installed_runtime: { ...status.installed_runtime!, checking: true, ready: false,
    runtime_available: false, model_groups: [
      { id: "tts-mlx", runtime_ready: false, assets_ready: true, ready: false, runtime_state: state },
    ],
  } };
}

function readyStatus(): RuntimeSetupStatusResponse {
  return { ...availableStatus(), configured: true, state: "ready", ready: true, selected_profile: "mlx",
    selected_model_groups: [{ id: "tts-mlx", runtime_ready: true, assets_ready: true, assets_reason: null, ready: true }],
  };
}

const json = (value: unknown) => new Response(JSON.stringify(value));

describe("setup readiness through pending checks", () => {
  let root: Root;
  let container: HTMLDivElement;
  let bridge: SetupPageBridge;
  let lookup: () => Promise<Response>;
  let adopt: () => Promise<Response>;
  let requests: Array<{ path: string; body: unknown }>;
  let onReady: ReturnType<typeof vi.fn<(status: RuntimeSetupStatusResponse) => void>>;
  let onContinueLibraryOnly: ReturnType<typeof vi.fn<() => void>>;
  let onReconnectBackend: ReturnType<typeof vi.fn<() => void>>;

  const client = () => createApiClient({ baseUrl: "http://setup.test", token: "session" });
  const primary = () => container.querySelector<HTMLButtonElement>(".setup-primary")!;
  async function mount(apiClient: ApiClient, status: RuntimeSetupStatusResponse) {
    await act(async () => root.render(<SetupPage apiClient={apiClient} initialStatus={status}
      initialError={null} onReady={onReady} onContinueLibraryOnly={onContinueLibraryOnly}
      onReconnectBackend={onReconnectBackend} bridge={bridge} />));
  }

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.useFakeTimers();
    window.localStorage.clear();
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    onReady = vi.fn();
    onContinueLibraryOnly = vi.fn();
    onReconnectBackend = vi.fn();
    requests = [];
    lookup = async () => json(availableStatus());
    adopt = async () => json(readyStatus());
    const idle = { lifecycle: "idle" as const, profile: null, modelGroups: [], stage: null,
      message: null, percent: null, restartRequired: false, error: null };
    bridge = {
      environment: async () => ({ available: false, mode: "packaged", reason: "Bundled runtime." }),
      status: async () => idle, start: async () => idle, cancel: async () => idle,
      restart: async () => {}, listen: async () => () => {},
    };
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const path = new URL(String(input)).pathname;
      requests.push({ path, body: init?.body ? JSON.parse(String(init.body)) : null });
      if (path === "/api/setup/status") return lookup();
      if (path === "/api/setup/adopt") return adopt();
      if (path === "/api/setup/models") return json({ models: [] });
      throw new Error(`Unexpected request: ${path}`);
    });
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.restoreAllMocks();
    vi.useRealTimers();
    window.localStorage.clear();
  });

  it.each(["pending", "timeout"] as const)("keeps a %s check disabled until assets can be adopted", async (state) => {
    const pending = checkingStatus(state);
    lookup = async () => json(pending);
    await mount(client(), pending);
    expect(container.textContent).toContain("Checking your hardware…");
    expect(primary().textContent).toContain("Checking");
    expect(primary().disabled).toBe(true);
    await act(async () => primary().click());
    expect(requests.filter(({ path }) => path === "/api/setup/adopt")).toHaveLength(0);
    await act(async () => vi.advanceTimersByTimeAsync(3000));
    expect(primary().disabled).toBe(true);
    expect(onReady).not.toHaveBeenCalled();
    lookup = async () => json(availableStatus());
    await act(async () => vi.advanceTimersByTimeAsync(3000));
    expect(container.textContent).not.toContain("Checking your hardware…");
    expect(primary().textContent).toBe("Continue");
    expect(primary().disabled).toBe(false);
    const statusRequests = requests.filter(({ path }) => path === "/api/setup/status").length;
    await act(async () => vi.advanceTimersByTimeAsync(9000));
    expect(requests.filter(({ path }) => path === "/api/setup/status")).toHaveLength(statusRequests);
    const saved = deferred<Response>();
    adopt = () => saved.promise;
    await act(async () => primary().click());
    expect(primary().disabled).toBe(true);
    expect(onReady).not.toHaveBeenCalled();
    expect(requests.filter(({ path }) => path === "/api/setup/adopt")).toEqual([
      { path: "/api/setup/adopt", body: { profile: "mlx", model_groups: ["tts-mlx"],
        cleaner_cuda_quantization: "4bit", accept_pytorch_risk: false } },
    ]);
    await act(async () => saved.resolve(json(readyStatus())));
    expect(onReady).toHaveBeenCalledExactlyOnceWith(readyStatus());
    expect(primary().textContent).toBe("Done");
    expect(primary().disabled).toBe(false);
  });

  it("keeps checking the new client when a retired client's ready response arrives", async () => {
    const oldStatus = deferred<Response>();
    lookup = () => oldStatus.promise;
    await mount(client(), checkingStatus("pending"));
    await act(async () => vi.advanceTimersByTimeAsync(3000));
    expect(requests.filter(({ path }) => path === "/api/setup/status")).toHaveLength(1);
    const replacement = checkingStatus("pending");
    replacement.backend.instance_id = "replacement";
    lookup = async () => json(replacement);
    await mount(client(), replacement);
    await act(async () => oldStatus.resolve(json(readyStatus())));
    expect(primary().disabled).toBe(true);
    expect(container.textContent).toContain("Checking your hardware…");
    expect(onReady).not.toHaveBeenCalled();
    await act(async () => vi.advanceTimersByTimeAsync(3000));
    expect(onReady).not.toHaveBeenCalled();
    const ready = { ...readyStatus(), backend: replacement.backend };
    lookup = async () => json(ready);
    await act(async () => vi.advanceTimersByTimeAsync(3000));
    expect(onReady).toHaveBeenCalledExactlyOnceWith(ready);
    expect(primary().textContent).toBe("Done");
    expect(primary().disabled).toBe(false);
  });
});
