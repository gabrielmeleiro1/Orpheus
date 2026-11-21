// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import type { ApiClient } from "../../api/client";
import type { RuntimeSetupStatusResponse } from "../../types/api";
import { readPersistedSettings, savePersistedSettings } from "../conversion/conversionOptionsShared";
import { SetupDangerZone } from "./SetupDangerZone";
import { syncSetupPreferences } from "./setupPreferences";

it.each([
  ["pending", "restores preferences before the status refresh completes"],
  ["failed", "restores preferences even when the status refresh fails"],
  ["residual", "preserves preferences when local models remain"],
  ["rejected", "preserves preferences when deletion fails"],
])("%s: %s", async (outcome) => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  localStorage.clear();
  savePersistedSettings({ cleaner_backend: "api", cleaner_model: "custom-model", voice: "af_heart" });
  syncSetupPreferences({
    configured: true, selected_profile: "cuda", cleaner_cuda_quantization: "8bit",
    selected_model_groups: [{ id: "cleaner-cuda" }],
  } as RuntimeSetupStatusResponse);
  savePersistedSettings({ ...readPersistedSettings(), cleaner_cuda_device: "cuda:1" });
  const saved = readPersistedSettings();
  let finishRefresh!: () => void;
  const refresh = new Promise<void>((resolve) => { finishRefresh = resolve; });
  const onDeleted = vi.fn(() => outcome === "failed" ? Promise.reject(new Error("Refresh failed")) : refresh);
  const onError = vi.fn();
  const client = {
    deleteRuntimeSetupData: vi.fn(async () => {
      if (outcome === "rejected") throw new Error("Deletion failed");
      return {
        deleted_model_groups: [], freed_bytes: 0, state_deleted: false,
        ...(outcome === "residual" ? { residual_model_groups: ["cleaner-cuda"] } : { setup_reset_id: "reset-1" }),
      };
    }),
  } as unknown as ApiClient;
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(<SetupDangerZone apiClient={client} disabled={false} onDeleted={onDeleted} onError={onError} onBusyChange={vi.fn()} />));
    await act(async () => { container.querySelector("details")!.open = true; });
    await act(async () => container.querySelector<HTMLButtonElement>(".setup-danger-actions button")!.click());
    await act(async () => [...container.querySelectorAll("button")].find((button) => button.textContent === "Delete")!.click());
    if (outcome === "pending" || outcome === "failed") {
      expect(readPersistedSettings()).toMatchObject({
        cleaner_backend: "api", cleaner_model: "custom-model", voice: "af_heart", cleaner_cuda_device: "cuda:1",
      });
      expect(readPersistedSettings()).not.toHaveProperty("setup_selection");
    } else {
      expect(readPersistedSettings()).toEqual(saved);
    }
    expect(onDeleted).toHaveBeenCalledTimes(outcome === "rejected" ? 0 : 1);
    if (outcome === "failed") expect(onError).toHaveBeenLastCalledWith("Refresh failed");
  } finally {
    await act(async () => { finishRefresh(); });
    await act(async () => root.unmount());
    container.remove();
    localStorage.clear();
  }
});
