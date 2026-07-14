import { beforeEach, describe, expect, it, vi } from "vitest";

const invoke = vi.fn();
const listen = vi.fn();
const isTauriRuntime = vi.fn(() => true);
vi.mock("@tauri-apps/api/core", () => ({ invoke }));
vi.mock("@tauri-apps/api/event", () => ({ listen }));
vi.mock("../../platform/tauriRuntime", () => ({ isTauriRuntime }));

describe("runtime setup bridge", () => {
  beforeEach(() => {
    invoke.mockReset();
    listen.mockReset();
    isTauriRuntime.mockReset();
    isTauriRuntime.mockReturnValue(true);
  });

  it("serializes only the allowlisted setup request shape", async () => {
    invoke.mockResolvedValue({ lifecycle: "running", profile: "mlx", modelGroups: ["tts-mlx"], restartRequired: false, error: null });
    const { startRuntimeSetup } = await import("./setupBridge");
    await startRuntimeSetup({ profile: "mlx", modelGroups: ["tts-mlx"], acceptPytorchRisk: false });
    expect(invoke).toHaveBeenCalledWith("start_runtime_setup", {
      request: { profile: "mlx", modelGroups: ["tts-mlx"], acceptPytorchRisk: false },
    });
  });

  it("reads authoritative status and returns the event unsubscribe handle", async () => {
    const unlisten = vi.fn();
    invoke.mockResolvedValue({ lifecycle: "idle" });
    listen.mockResolvedValue(unlisten);
    const { getRuntimeSetupRunnerStatus, listenRuntimeSetupProgress } = await import("./setupBridge");
    await getRuntimeSetupRunnerStatus();
    const release = await listenRuntimeSetupProgress(vi.fn());
    expect(invoke).toHaveBeenCalledWith("runtime_setup_status");
    expect(listen).toHaveBeenCalledWith("runtime-setup-progress", expect.any(Function));
    release();
    expect(unlisten).toHaveBeenCalledOnce();
  });

  it("uses the setup-authorized restart command", async () => {
    invoke.mockResolvedValue(undefined);
    const { restartOrpheusBackend } = await import("./setupBridge");
    await restartOrpheusBackend();
    expect(invoke).toHaveBeenCalledWith("restart_backend_after_setup");
  });

  it("cancels through the fixed setup command", async () => {
    invoke.mockResolvedValue({ lifecycle: "cancelled" });
    const { cancelRuntimeSetup } = await import("./setupBridge");
    await cancelRuntimeSetup();
    expect(invoke).toHaveBeenCalledWith("cancel_runtime_setup");
  });

  it("reports browser setup as unavailable without invoking Tauri", async () => {
    isTauriRuntime.mockReturnValue(false);
    const { getRuntimeSetupEnvironment } = await import("./setupBridge");
    await expect(getRuntimeSetupEnvironment()).resolves.toMatchObject({ available: false, mode: "browser" });
    expect(invoke).not.toHaveBeenCalled();
  });
});
