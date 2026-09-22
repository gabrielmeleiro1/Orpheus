import type { RuntimeSetupStatusResponse } from "../../types/api";
import { readPersistedSettings, savePersistedSettings } from "../conversion/conversionOptionsShared";
import { MODEL_GROUP_DETAILS } from "./setupModel";

/** Apply a changed setup choice once; preserve later per-conversion preferences. */
export function syncSetupPreferences(status: RuntimeSetupStatusResponse): void {
  if (!status.configured) return;
  const cleaner = status.selected_model_groups.find(({ id }) => MODEL_GROUP_DETAILS[id]?.kind === "cleaner");
  const backend = cleaner ? status.selected_profile ?? "basic" : "basic";
  const model = cleaner ? MODEL_GROUP_DETAILS[cleaner.id].model : "";
  const quantization = status.cleaner_cuda_quantization ?? "4bit";
  const signature = JSON.stringify([status.selected_profile, model, quantization, backend]);
  const previous = readPersistedSettings();
  if (previous.setup_selection === signature) return;
  savePersistedSettings({
    ...previous,
    setup_selection: signature,
    cleaner_backend: backend,
    preferred_cleaner_backend: backend,
    cleaner_backend_configured: true,
    cleaner_model: model,
    preferred_cleaner_model: model,
    cleaner_cuda_device: "cuda",
    cleaner_cuda_quantization: quantization,
  });
}
