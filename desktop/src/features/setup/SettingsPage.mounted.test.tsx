// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import type { RuntimeSetupStatusResponse } from "../../types/api";
import type { ApiClient } from "../../api/client";
import { savePersistedSettings } from "../conversion/conversionOptionsShared";
import { SettingsPage } from "./SettingsPage";

it("shows models, storage, version and opens only the selected storage folder", async () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const requestJson = vi.fn(async () => ({ output_path: "C:/books", app_data_path: "C:/Orpheus" }));
  const manage = vi.fn();
  const client = { requestJson, getRuntimeModelInventory: vi.fn(async () => ({ models: [{ id: "tts-cpu-cuda", downloaded: true, size_bytes: 10 }, { id: "cleaner-cuda", downloaded: true }, { id: "cleaner-cuda-large", downloaded: true }] })) } as unknown as ApiClient;
  const status = { configured: true, selected_profile: "cuda", selected_model_groups: [{ id: "tts-cpu-cuda", assets_ready: true }], profiles: [] } as unknown as RuntimeSetupStatusResponse;
  try {
    await act(async () => root.render(<SettingsPage active apiClient={client} status={status} onManageModels={manage}><div>Existing cleaner controls</div></SettingsPage>));
    expect(container.textContent).toContain("Voice & models");
    expect(container.textContent).toContain("Kokoro TTS");
    expect(container.querySelector("h1")).toBeNull();
    expect(container.textContent).toContain("Basic cleanup (no model)");
    expect(container.textContent).not.toContain("Qwen");
    expect(client.getRuntimeModelInventory).not.toHaveBeenCalled();
    await act(async () => savePersistedSettings({ cleaner_backend_configured: true, cleaner_backend: "cuda", cleaner_model: "Qwen/Qwen2.5-3B-Instruct" }));
    expect(container.textContent).toContain("Qwen 2.5 · 3B");
    expect(container.textContent).not.toContain("1.5B");
    await act(async () => savePersistedSettings({ cleaner_backend_configured: true, cleaner_backend: "basic" }));
    expect(container.textContent).toContain("Basic cleanup (no model)");
    expect(container.textContent).toContain("Existing cleaner controls");
    expect(container.textContent).toContain("C:/books");
    expect(container.textContent).toContain("C:/Orpheus");
    expect(container.textContent).toContain("Orpheus 1.0.0");
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Open output folder"]')!.click());
    expect(requestJson).toHaveBeenCalledWith("/api/system/storage/output/open", { timeoutMs: 8_000, method: "POST" });
    await act(async () => [...container.querySelectorAll('button')].find((b) => b.textContent === "Manage models")!.click());
    expect(manage).toHaveBeenCalledOnce();
  } finally {
    await act(async () => root.unmount());
    container.remove();
    localStorage.clear();
  }
});
