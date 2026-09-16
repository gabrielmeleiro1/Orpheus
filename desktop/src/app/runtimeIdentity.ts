import type { ModelOptionsResponse, SystemCapabilitiesResponse } from "../types/api";
import {
  filterAvailableCleanerBackendOptions,
  resolveCleanerBackendOptions,
  resolveInitialCleanerBackendId,
} from "../features/conversion/cleanerBackendHelpers";
import type { PersistedSettings } from "../features/conversion/conversionOptionsShared";

export interface RuntimeIdentity {
  ttsLabel: string;
  cleanerLabel: string;
}

// The backend reports runtime ids in lowercase ("mlx"); the header shows them
// as proper names.
const RUNTIME_ID_LABELS: Record<string, string> = { mlx: "MLX", cuda: "CUDA", cpu: "CPU" };

export function resolveRuntimeIdentity(input: {
  primaryRuntime: string | null;
  cleanerBackendId: string;
  cleanerBackendLabel: string | null;
  ttsEngineLabel: string | null;
}): RuntimeIdentity {
  const rawTts =
    String(input.primaryRuntime ?? "").trim() ||
    String(input.ttsEngineLabel ?? "").trim();
  const ttsLabel = rawTts ? RUNTIME_ID_LABELS[rawTts.toLowerCase()] ?? rawTts : "—";
  const cleanerLabel =
    String(input.cleanerBackendLabel ?? "").trim() ||
    String(input.cleanerBackendId ?? "").trim() ||
    "—";
  return { ttsLabel, cleanerLabel };
}

export function formatRuntimeIdentity(identity: RuntimeIdentity): string {
  return `TTS: ${identity.ttsLabel} · Cleaner: ${identity.cleanerLabel}`;
}

export function buildRuntimeIdentityFromSnapshot(input: {
  capabilities: SystemCapabilitiesResponse | null;
  modelOptions: ModelOptionsResponse | null;
  settings: PersistedSettings;
}): RuntimeIdentity {
  // Mirror the conversion views' selection policy exactly: a persisted backend
  // only counts when the user explicitly configured it AND it is still
  // available; otherwise the app auto-picks the preferred (local-first)
  // available backend, so the header must report that same choice.
  const availableOptions = filterAvailableCleanerBackendOptions(
    resolveCleanerBackendOptions(input.modelOptions),
    input.capabilities,
  );
  const persistedBackend = input.settings.cleaner_backend_configured
    ? String(input.settings.cleaner_backend ?? "").trim()
    : "";
  const cleanerId = resolveInitialCleanerBackendId(availableOptions, persistedBackend).backendId;
  const cleaner = availableOptions.find((option) => option.id === cleanerId) ?? null;
  const primaryRuntime =
    typeof input.capabilities?.primary_runtime === "string"
      ? input.capabilities.primary_runtime.trim()
      : "";
  return resolveRuntimeIdentity({
    primaryRuntime: primaryRuntime || null,
    cleanerBackendId: cleanerId,
    cleanerBackendLabel: cleaner?.display_name ?? null,
    ttsEngineLabel: null,
  });
}

export function buildRuntimeIdentityLabelFromSnapshot(input: Parameters<typeof buildRuntimeIdentityFromSnapshot>[0]): string {
  return formatRuntimeIdentity(buildRuntimeIdentityFromSnapshot(input));
}
