// @vitest-environment happy-dom
import { beforeEach, expect, it } from "vitest";
import type { RuntimeSetupStatusResponse } from "../../types/api";
import { readPersistedSettings, savePersistedSettings } from "../conversion/conversionOptionsShared";
import { syncSetupPreferences } from "./setupPreferences";

beforeEach(() => localStorage.clear());
const snapshot = (model = "cleaner-cuda-large", quantization = "8bit") => ({
  configured: true, selected_profile: "cuda", cleaner_cuda_quantization: quantization,
  selected_model_groups: [{ id: "tts-cpu-cuda" }, ...(model ? [{ id: model }] : [])],
}) as RuntimeSetupStatusResponse;

it("carries the installed model and precision into conversion while preserving voice settings", () => {
  savePersistedSettings({ voice: "am_adam", speed: 1.2 });
  syncSetupPreferences(snapshot());
  expect(readPersistedSettings()).toMatchObject({ voice: "am_adam", speed: 1.2, preferred_cleaner_model: "Qwen/Qwen2.5-3B-Instruct", preferred_cleaner_backend: "cuda", cleaner_cuda_quantization: "8bit", cleaner_cuda_device: "cuda" });
});

it("preserves later conversion tuning until the saved setup choice changes", () => {
  syncSetupPreferences(snapshot());
  savePersistedSettings({ ...readPersistedSettings(), cleaner_cuda_quantization: "fp16" });
  syncSetupPreferences(snapshot());
  expect(readPersistedSettings().cleaner_cuda_quantization).toBe("fp16");
  syncSetupPreferences(snapshot("cleaner-cuda-large", "4bit"));
  expect(readPersistedSettings()).toMatchObject({ cleaner_cuda_quantization: "4bit", cleaner_model: "Qwen/Qwen2.5-3B-Instruct" });
});

it("clears a deleted local cleaner preference", () => {
  syncSetupPreferences(snapshot());
  syncSetupPreferences(snapshot(""));
  expect(readPersistedSettings()).toMatchObject({ preferred_cleaner_backend: "basic", preferred_cleaner_model: "" });
});


it("preserves an explicit Settings backend after synchronizing the same no-cleaner setup", () => {
  syncSetupPreferences(snapshot(""));
  savePersistedSettings({ ...readPersistedSettings(), cleaner_backend: "api", preferred_cleaner_backend: "api", cleaner_model: "gpt-5-nano" });
  syncSetupPreferences(snapshot(""));
  expect(readPersistedSettings()).toMatchObject({ cleaner_backend: "api", preferred_cleaner_backend: "api", cleaner_model: "gpt-5-nano" });
});

it("restores pre-setup preferences only for an explicit reset and preserves later tuning", () => {
  savePersistedSettings({ voice: "am_adam", cleaner_backend: "api", cleaner_model: "custom-model" });
  syncSetupPreferences(snapshot());
  syncSetupPreferences(snapshot("cleaner-cuda", "4bit"));
  savePersistedSettings({ ...readPersistedSettings(), cleaner_cuda_device: "cuda:1" });
  expect(syncSetupPreferences({ configured: false })).toBe(false);
  const reset = { ...snapshot(), configured: false, setup_reset_id: "reset-1" };
  expect(syncSetupPreferences(reset)).toBe(true);
  expect(readPersistedSettings()).toMatchObject({ voice: "am_adam", cleaner_backend: "api", cleaner_model: "custom-model", cleaner_cuda_device: "cuda:1" });
  expect(readPersistedSettings()).not.toHaveProperty("preferred_cleaner_backend");
  expect(readPersistedSettings()).not.toHaveProperty("setup_selection");
  expect(syncSetupPreferences(reset)).toBe(false);
  syncSetupPreferences(snapshot());
  expect(readPersistedSettings().cleaner_backend).toBe("cuda");
});

it("keeps saved setup preferences when startup only reports a missing state file", () => {
  syncSetupPreferences(snapshot());
  const saved = readPersistedSettings();
  expect(syncSetupPreferences({ ...snapshot(), configured: false })).toBe(false);
  expect(readPersistedSettings()).toEqual(saved);
});

it("retains the pre-setup baseline when conversion settings are rewritten", () => {
  savePersistedSettings({ cleaner_backend: "api", cleaner_model: "custom-model" });
  syncSetupPreferences(snapshot());
  const saved = readPersistedSettings();
  savePersistedSettings({
    setup_selection: saved.setup_selection,
    cleaner_backend: saved.cleaner_backend,
    cleaner_model: saved.cleaner_model,
    voice: "af_heart",
  });
  syncSetupPreferences({ ...snapshot(), configured: false, setup_reset_id: "reset-2" });
  expect(readPersistedSettings()).toMatchObject({ cleaner_backend: "api", cleaner_model: "custom-model", voice: "af_heart" });
});
