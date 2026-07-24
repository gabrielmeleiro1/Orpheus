// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useLibraryMaintenance, type UseLibraryMaintenanceResult } from "./useLibraryMaintenance";
import type { ApiClient } from "../../api/client";
import type { MaintenanceDiagnosticsResponse } from "../../types/api";

const { invokeMock } = vi.hoisted(() => ({ invokeMock: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke: invokeMock }));

function Probe(): null {
  const result = useLibraryMaintenance({ apiClient: null, onAfterCleanup: vi.fn() });
  (globalThis as typeof globalThis & { maintenanceProbe?: UseLibraryMaintenanceResult }).maintenanceProbe = result;
  return null;
}

function CleanupProbe({ apiClient }: { apiClient: ApiClient }): null {
  const result = useLibraryMaintenance({ apiClient, onAfterCleanup: vi.fn() });
  (globalThis as typeof globalThis & { maintenanceProbe?: UseLibraryMaintenanceResult }).maintenanceProbe = result;
  return null;
}

function MaintenanceProbeWithCallback({ apiClient, onAfterCleanup }: { apiClient: ApiClient; onAfterCleanup: () => void }): null {
  const result = useLibraryMaintenance({ apiClient, onAfterCleanup });
  (globalThis as typeof globalThis & { maintenanceProbe?: UseLibraryMaintenanceResult }).maintenanceProbe = result;
  return null;
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((promiseResolve) => {
    resolve = promiseResolve;
  });
  return { promise, resolve };
}


describe("useLibraryMaintenance", () => {
  let root: Root;
  let container: HTMLDivElement;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    Object.defineProperty(window, "__TAURI_INTERNALS__", { configurable: true, value: {} });
    container = document.createElement("div");
    document.body.append(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    Reflect.deleteProperty(window, "__TAURI_INTERNALS__");
    delete (globalThis as typeof globalThis & { maintenanceProbe?: unknown }).maintenanceProbe;
    vi.clearAllMocks();
  });

  it("surfaces an app-data-folder IPC failure", async () => {
    invokeMock.mockRejectedValueOnce(new Error("finder unavailable"));
    await act(async () => {
      root = createRoot(container);
      root.render(<Probe />);
    });

    await act(async () => {
      await (globalThis as typeof globalThis & { maintenanceProbe: UseLibraryMaintenanceResult })
        .maintenanceProbe.openAppDataFolder();
    });

    expect(
      (globalThis as typeof globalThis & { maintenanceProbe: UseLibraryMaintenanceResult })
        .maintenanceProbe.maintenanceMessage,
    ).toBe("Could not open app data folder: finder unavailable");
  });

  it("runs an authorized cleanup without consulting the browser confirm dialog", async () => {
    const cleanupStorage = vi.fn().mockRejectedValue(new Error("cleanup failed"));
    const browserConfirm = vi.fn(() => false);
    Object.defineProperty(window, "confirm", { configurable: true, value: browserConfirm });
    const apiClient = { cleanupStorage } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<CleanupProbe apiClient={apiClient} />);
    });
    await act(async () => {
      await (globalThis as typeof globalThis & { maintenanceProbe: UseLibraryMaintenanceResult })
        .maintenanceProbe.cleanupStorage();
    });

    expect(browserConfirm).not.toHaveBeenCalled();
    expect(cleanupStorage).toHaveBeenCalledTimes(1);
  });

  it("does not run cleanup refresh after a newer maintenance request supersedes it", async () => {
    const diagnosticsA = deferred<MaintenanceDiagnosticsResponse>();
    const diagnostics = {
      database_path: "db",
      output_path: "out",
      temp_path: "tmp",
      app_data_path: "app",
      audit: {},
    } as MaintenanceDiagnosticsResponse;
    let diagnosticsCalls = 0;
    const getMaintenanceDiagnostics = vi.fn(() => {
      diagnosticsCalls += 1;
      return diagnosticsCalls === 1 ? diagnosticsA.promise : Promise.resolve(diagnostics);
    });
    const cleanupStorage = vi.fn(async () => ({ deleted_files: 0, deleted_rows: 0 }));
    const onAfterCleanup = vi.fn();
    const apiClient = { cleanupStorage, getMaintenanceDiagnostics } as unknown as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(<MaintenanceProbeWithCallback apiClient={apiClient} onAfterCleanup={onAfterCleanup} />);
    });
    let cleanupPromise!: Promise<void>;
    await act(async () => {
      cleanupPromise = (globalThis as typeof globalThis & { maintenanceProbe: UseLibraryMaintenanceResult }).maintenanceProbe.cleanupStorage();
      await Promise.resolve();
    });
    await act(async () => {
      await (globalThis as typeof globalThis & { maintenanceProbe: UseLibraryMaintenanceResult }).maintenanceProbe.loadMaintenanceDiagnostics();
    });
    await act(async () => {
      diagnosticsA.resolve(diagnostics);
      await cleanupPromise;
    });

    expect(onAfterCleanup).not.toHaveBeenCalled();
  });

});
