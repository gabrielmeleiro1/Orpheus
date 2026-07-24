// @vitest-environment happy-dom

import { act, StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useLibraryMaintenance, type UseLibraryMaintenanceResult } from "./useLibraryMaintenance";
import type { ApiClient } from "../../api/client";
import type { MaintenanceDiagnosticsResponse } from "../../types/api";

const { invokeMock } = vi.hoisted(() => ({ invokeMock: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke: invokeMock }));
const maintenanceNoop = () => undefined;

function Probe(): null {
  const result = useLibraryMaintenance({ apiClient: null, onAfterCleanup: vi.fn() });
  (globalThis as typeof globalThis & { maintenanceProbe?: UseLibraryMaintenanceResult }).maintenanceProbe = result;
  return null;
}

function CleanupProbe({ apiClient }: { apiClient: ApiClient }): null {
  const result = useLibraryMaintenance({ apiClient, onAfterCleanup: maintenanceNoop });
  (globalThis as typeof globalThis & { maintenanceProbe?: UseLibraryMaintenanceResult }).maintenanceProbe = result;
  return null;
}

function MaintenanceProbeWithCallback({ apiClient, onAfterCleanup }: { apiClient: ApiClient; onAfterCleanup: () => void }): null {
  const result = useLibraryMaintenance({ apiClient, onAfterCleanup });
  (globalThis as typeof globalThis & { maintenanceProbe?: UseLibraryMaintenanceResult }).maintenanceProbe = result;
  return null;
}

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });
  return { promise, resolve, reject };
}

function MaintenanceProbeWithContext({
  apiClient,
  getReloadContextGeneration,
}: {
  apiClient: ApiClient;
  getReloadContextGeneration: () => number;
}): null {
  const result = useLibraryMaintenance({
    apiClient,
    onAfterCleanup: maintenanceNoop,
    getReloadContextGeneration,
  });
  (globalThis as typeof globalThis & { maintenanceProbe?: UseLibraryMaintenanceResult }).maintenanceProbe = result;
  return null;
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

  it("ignores an app-data-folder IPC failure after the API context changes", async () => {
    const invokeRequest = deferred<void>();
    invokeMock.mockReturnValueOnce(invokeRequest.promise);
    const oldClient = {} as ApiClient;
    const newClient = {} as ApiClient;
    const onAfterCleanup = vi.fn();

    await act(async () => {
      root = createRoot(container);
      root.render(<MaintenanceProbeWithCallback apiClient={oldClient} onAfterCleanup={onAfterCleanup} />);
    });
    let openPromise!: Promise<void>;
    await act(async () => {
      openPromise = (globalThis as typeof globalThis & { maintenanceProbe: UseLibraryMaintenanceResult })
        .maintenanceProbe.openAppDataFolder();
      await Promise.resolve();
    });
    await act(async () => {
      root.render(<MaintenanceProbeWithCallback apiClient={newClient} onAfterCleanup={onAfterCleanup} />);
    });
    await act(async () => {
      invokeRequest.reject(new Error("stale finder failure"));
      await openPromise;
    });

    expect(
      (globalThis as typeof globalThis & { maintenanceProbe: UseLibraryMaintenanceResult })
        .maintenanceProbe.maintenanceMessage,
    ).toBe("Maintenance idle");
  });

  it("does not report an app-data-folder IPC failure after StrictMode unmount", async () => {
    const invokeRequest = deferred<void>();
    invokeMock.mockReturnValueOnce(invokeRequest.promise);
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const apiClient = {} as ApiClient;

    await act(async () => {
      root = createRoot(container);
      root.render(
        <StrictMode>
          <MaintenanceProbeWithCallback apiClient={apiClient} onAfterCleanup={maintenanceNoop} />
        </StrictMode>,
      );
    });
    let openPromise!: Promise<void>;
    await act(async () => {
      openPromise = (globalThis as typeof globalThis & { maintenanceProbe: UseLibraryMaintenanceResult })
        .maintenanceProbe.openAppDataFolder();
      await Promise.resolve();
    });
    await act(async () => root.unmount());
    await act(async () => {
      invokeRequest.reject(new Error("unmounted finder failure"));
      await openPromise;
    });

    expect(consoleError).not.toHaveBeenCalled();
    consoleError.mockRestore();
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

  it("invalidates cleanup continuation when the API client identity changes", async () => {
    const cleanupRequest = deferred<{ deleted_files: number; deleted_rows: number }>();
    const oldDiagnostics = vi.fn(async () => ({
      database_path: "old",
      output_path: "old",
      temp_path: "old",
      app_data_path: "old",
      audit: {},
    }));
    const oldClient = {
      cleanupStorage: vi.fn(() => cleanupRequest.promise),
      getMaintenanceDiagnostics: oldDiagnostics,
    } as unknown as ApiClient;
    const newClient = {
      cleanupStorage: vi.fn(async () => ({ deleted_files: 0, deleted_rows: 0 })),
      getMaintenanceDiagnostics: vi.fn(),
    } as unknown as ApiClient;
    const onAfterCleanup = vi.fn();

    await act(async () => {
      root = createRoot(container);
      root.render(<MaintenanceProbeWithCallback apiClient={oldClient} onAfterCleanup={onAfterCleanup} />);
    });
    let cleanupPromise!: Promise<void>;
    await act(async () => {
      cleanupPromise = (globalThis as typeof globalThis & { maintenanceProbe: UseLibraryMaintenanceResult })
        .maintenanceProbe.cleanupStorage();
      await Promise.resolve();
    });
    await act(async () => {
      root.render(<MaintenanceProbeWithCallback apiClient={newClient} onAfterCleanup={onAfterCleanup} />);
    });
    await act(async () => {
      cleanupRequest.resolve({ deleted_files: 0, deleted_rows: 0 });
      await cleanupPromise;
    });

    expect(oldDiagnostics).not.toHaveBeenCalled();
    expect(onAfterCleanup).not.toHaveBeenCalled();
  });

  it("invalidates cleanup continuation when the reload callback generation changes", async () => {
    const cleanupRequest = deferred<{ deleted_files: number; deleted_rows: number }>();
    const diagnostics = vi.fn(async () => ({
      database_path: "db",
      output_path: "out",
      temp_path: "tmp",
      app_data_path: "app",
      audit: {},
    }));
    const apiClient = {
      cleanupStorage: vi.fn(() => cleanupRequest.promise),
      getMaintenanceDiagnostics: diagnostics,
    } as unknown as ApiClient;
    const oldReload = vi.fn();
    const newReload = vi.fn();

    await act(async () => {
      root = createRoot(container);
      root.render(<MaintenanceProbeWithCallback apiClient={apiClient} onAfterCleanup={oldReload} />);
    });
    let cleanupPromise!: Promise<void>;
    await act(async () => {
      cleanupPromise = (globalThis as typeof globalThis & { maintenanceProbe: UseLibraryMaintenanceResult })
        .maintenanceProbe.cleanupStorage();
      await Promise.resolve();
    });
    await act(async () => {
      root.render(<MaintenanceProbeWithCallback apiClient={apiClient} onAfterCleanup={newReload} />);
    });
    await act(async () => {
      cleanupRequest.resolve({ deleted_files: 0, deleted_rows: 0 });
      await cleanupPromise;
    });

    expect(diagnostics).not.toHaveBeenCalled();
    expect(oldReload).not.toHaveBeenCalled();
    expect(newReload).not.toHaveBeenCalled();
  });

  it("ignores a cleanup failure after a newer list context starts", async () => {
    const cleanupRequest = deferred<{ deleted_files: number; deleted_rows: number }>();
    const apiClient = {
      cleanupStorage: vi.fn(() => cleanupRequest.promise),
    } as unknown as ApiClient;
    let reloadContextGeneration = 1;
    const getReloadContextGeneration = () => reloadContextGeneration;

    await act(async () => {
      root = createRoot(container);
      root.render(
        <MaintenanceProbeWithContext
          apiClient={apiClient}
          getReloadContextGeneration={getReloadContextGeneration}
        />,
      );
    });
    let cleanupPromise!: Promise<void>;
    await act(async () => {
      cleanupPromise = (globalThis as typeof globalThis & { maintenanceProbe: UseLibraryMaintenanceResult })
        .maintenanceProbe.cleanupStorage();
      await Promise.resolve();
    });
    reloadContextGeneration += 1;
    await act(async () => {
      cleanupRequest.reject(new Error("stale cleanup failed"));
      await cleanupPromise;
    });

    expect(
      (globalThis as typeof globalThis & { maintenanceProbe: UseLibraryMaintenanceResult })
        .maintenanceProbe.maintenanceMessage,
    ).toBe("Maintenance idle");
  });

});
