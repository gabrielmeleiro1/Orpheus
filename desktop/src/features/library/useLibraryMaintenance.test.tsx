// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useLibraryMaintenance, type UseLibraryMaintenanceResult } from "./useLibraryMaintenance";

const { invokeMock } = vi.hoisted(() => ({ invokeMock: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke: invokeMock }));

function Probe(): null {
  const result = useLibraryMaintenance({ apiClient: null, onAfterCleanup: vi.fn() });
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
});
