import type { RuntimeSetupStatusResponse } from "../../types/api";
import { type PersistedSettings, readPersistedSettings, savePersistedSettings } from "../conversion/conversionOptionsShared";
import { MODEL_GROUP_DETAILS } from "./setupModel";

const setupKeys = [
  "cleaner_backend", "preferred_cleaner_backend", "cleaner_backend_configured",
  "cleaner_model", "preferred_cleaner_model", "cleaner_cuda_device", "cleaner_cuda_quantization",
] as const;

const ownershipKey = "orpheus.desktop.setup-preferences.v1";
type SetupOwnership = {
  baseline?: PersistedSettings;
  applied?: PersistedSettings;
  resetId?: string;
};

type SetupPreferenceStatus = ({ configured: false } | Pick<RuntimeSetupStatusResponse,
  "configured" | "selected_profile" | "selected_model_groups" | "cleaner_cuda_quantization"
>) & { setup_reset_id?: string };

function readOwnership(): SetupOwnership {
  try {
    const value = JSON.parse(window.localStorage.getItem(ownershipKey) ?? "{}");
    return value && typeof value === "object" ? value : {};
  } catch {
    return {};
  }
}

function saveOwnership(ownership: SetupOwnership): void {
  try {
    window.localStorage.setItem(ownershipKey, JSON.stringify(ownership));
  } catch {
    // Ignore storage failures, matching conversion settings persistence.
  }
}

/** Apply a changed setup choice once; preserve later per-conversion preferences. */
export function syncSetupPreferences(status: SetupPreferenceStatus): boolean {
  const previous = readPersistedSettings();
  const ownership = readOwnership();
  let reset = false;
  if (status.setup_reset_id && status.setup_reset_id !== ownership.resetId) {
    for (const key of setupKeys) {
      if (ownership.applied && previous[key] === ownership.applied[key]) {
        delete previous[key];
        Object.assign(previous, { [key]: ownership.baseline?.[key] });
      }
    }
    delete previous.setup_selection;
    delete ownership.baseline;
    delete ownership.applied;
    ownership.resetId = status.setup_reset_id;
    saveOwnership(ownership);
    savePersistedSettings(previous);
    reset = true;
  }
  if (!status.configured) return reset;
  const cleaner = status.selected_model_groups.find(({ id }) => MODEL_GROUP_DETAILS[id]?.kind === "cleaner");
  const backend = cleaner ? status.selected_profile ?? "basic" : "basic";
  const model = cleaner ? MODEL_GROUP_DETAILS[cleaner.id].model : "";
  const quantization = status.cleaner_cuda_quantization ?? "4bit";
  const signature = JSON.stringify([status.selected_profile, model, quantization, backend]);
  if (previous.setup_selection === signature) return reset;
  const applied: PersistedSettings = {
    cleaner_backend: backend,
    preferred_cleaner_backend: backend,
    cleaner_backend_configured: true,
    cleaner_model: model,
    preferred_cleaner_model: model,
    cleaner_cuda_device: "cuda",
    cleaner_cuda_quantization: quantization,
  };
  saveOwnership({
    ...ownership,
    baseline: ownership.baseline ?? Object.fromEntries(setupKeys.map((key) => [key, previous[key]])),
    applied,
  });
  const next: PersistedSettings = {
    ...previous,
    ...applied,
    setup_selection: signature,
  };
  savePersistedSettings(next);
  return true;
}
