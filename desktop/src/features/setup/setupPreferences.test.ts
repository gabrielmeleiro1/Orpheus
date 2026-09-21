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
  expect(readPersistedSettings()).toMatchObject({ preferred_cleaner_backend: "api", preferred_cleaner_model: "" });
});
