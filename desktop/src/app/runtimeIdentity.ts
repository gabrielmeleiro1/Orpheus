import type { ModelOptionsResponse, SystemCapabilitiesResponse } from "../types/api";
import { resolveCleanerBackendOptions } from "../features/conversion/cleanerBackendHelpers";
import type { PersistedSettings } from "../features/conversion/conversionOptionsShared";

export interface RuntimeIdentity {
  ttsLabel: string;
  cleanerLabel: string;
}

export function resolveRuntimeIdentity(input: {
  primaryRuntime: string | null;
  cleanerBackendId: string;
  cleanerBackendLabel: string | null;
  ttsEngineLabel: string | null;
}): RuntimeIdentity {
  const ttsLabel =
    String(input.primaryRuntime ?? "").trim() ||
    String(input.ttsEngineLabel ?? "").trim() ||
    "—";
  const cleanerLabel =
    String(input.cleanerBackendLabel ?? "").trim() ||
    String(input.cleanerBackendId ?? "").trim() ||
    "—";
  return { ttsLabel, cleanerLabel };
}

export function formatRuntimeIdentity(identity: RuntimeIdentity): string {
  return `TTS: ${identity.ttsLabel} · Cleaner: ${identity.cleanerLabel}`;
}

export function buildRuntimeIdentityLabelFromSnapshot(input: {
  capabilities: SystemCapabilitiesResponse | null;
  modelOptions: ModelOptionsResponse | null;
  settings: PersistedSettings;
}): string {
  const cleanerOptions = resolveCleanerBackendOptions(input.modelOptions);
  const cleanerId = String(input.settings.cleaner_backend ?? "").trim();
  const cleaner = cleanerOptions.find((option) => option.id === cleanerId) ?? null;
  const primaryRuntime =
    typeof input.capabilities?.primary_runtime === "string"
      ? input.capabilities.primary_runtime.trim()
      : "";
  return formatRuntimeIdentity(
    resolveRuntimeIdentity({
      primaryRuntime: primaryRuntime || null,
      cleanerBackendId: cleanerId,
      cleanerBackendLabel: cleaner?.display_name ?? null,
      ttsEngineLabel: null,
    }),
  );
}
