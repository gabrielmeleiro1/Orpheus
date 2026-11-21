import { useEffect, useRef } from "react";

import type { ApiClient } from "../../api/client";

import { cleanerBackendUsesCudaFields } from "./cleanerBackendHelpers";
import { readPersistedSettings, savePersistedSettings } from "./conversionOptionsShared";
import { useCleanerOptions, type UseCleanerOptionsResult } from "./useCleanerOptions";
import { useParallelWorkers, type UseParallelWorkersResult } from "./useParallelWorkers";
import { useTtsOptions, type UseTtsOptionsResult } from "./useTtsOptions";

interface UseConversionOptionsArgs {
  apiClient: ApiClient | null;
  runtimeSelectionGeneration?: number;
}

export interface UseConversionOptionsResult
  extends Omit<UseTtsOptionsResult, "modelOptions" | "systemCapabilities">,
    Omit<UseCleanerOptionsResult, "cleanerOptionsHydrated">,
    UseParallelWorkersResult {}

export function useConversionOptions({
  apiClient,
  runtimeSelectionGeneration = 0,
}: UseConversionOptionsArgs): UseConversionOptionsResult {
  const persistedSettingsRef = useRef(readPersistedSettings());
  const settingsHydratedRef = useRef(false);

  useEffect(() => {
    settingsHydratedRef.current = false;
    persistedSettingsRef.current = readPersistedSettings();
  }, [runtimeSelectionGeneration]);

  const ttsOptions = useTtsOptions({
    apiClient,
    persistedSettingsRef,
    settingsHydratedRef,
    runtimeSelectionGeneration,
  });

  const cleanerOptions = useCleanerOptions({
    apiClient,
    modelOptions: ttsOptions.modelOptions,
    systemCapabilities: ttsOptions.systemCapabilities,
    persistedSettingsRef,
    runtimeSelectionGeneration,
  });

  const parallelWorkers = useParallelWorkers({
    apiClient,
    systemCapabilities: ttsOptions.systemCapabilities,
    persistedSettingsRef,
  });

  useEffect(() => {
    if (ttsOptions.loading || !settingsHydratedRef.current || !cleanerOptions.cleanerOptionsHydrated) {
      return;
    }

    // A persisted local backend can be temporarily unavailable while the
    // capability probe warms up. Keep the last CUDA tuning instead of
    // replacing it with the effective backend's empty/default values, so a
    // later backend switch can restore the user's device and quantization.
    const persistedPreferredBackend = String(
      persistedSettingsRef.current.preferred_cleaner_backend ?? "",
    ).trim();
    const preservePersistedCuda = Boolean(
      (cleanerOptions.cleanerBackendConfigured &&
        persistedPreferredBackend &&
        persistedPreferredBackend !== cleanerOptions.cleanerBackend) ||
        !cleanerBackendUsesCudaFields(cleanerOptions.selectedCleanerBackend),
    );
    const persistedCleanerCudaDevice = preservePersistedCuda
      ? String(persistedSettingsRef.current.cleaner_cuda_device ?? "").trim()
      : "";
    const persistedCleanerCudaQuantization = preservePersistedCuda
      ? String(persistedSettingsRef.current.cleaner_cuda_quantization ?? "").trim()
      : "";

    const nextSettings = {
      setup_selection: persistedSettingsRef.current.setup_selection,
      tts_engine: ttsOptions.selectedEngine || undefined,
      voice: ttsOptions.selectedVoice || undefined,
      speed: ttsOptions.speed,
      tts_quantization: ttsOptions.selectedQuantization || undefined,
      cleaner_backend: cleanerOptions.cleanerBackend || undefined,
      preferred_cleaner_backend: cleanerOptions.cleanerBackendConfigured
        ? String(
            persistedSettingsRef.current.preferred_cleaner_backend ??
              cleanerOptions.cleanerBackend,
          ).trim() || undefined
        : undefined,
      preferred_cleaner_model: cleanerOptions.cleanerBackendConfigured
        ? String(
            persistedSettingsRef.current.preferred_cleaner_model ??
              cleanerOptions.cleanerModel,
          ).trim() || undefined
        : undefined,
      cleaner_model: cleanerOptions.cleanerModel || undefined,
      cleaner_backend_configured: cleanerOptions.cleanerBackendConfigured,
      cleaner_cuda_device:
        persistedCleanerCudaDevice || cleanerOptions.cleanerCudaDevice || undefined,
      cleaner_cuda_quantization:
        persistedCleanerCudaQuantization || cleanerOptions.cleanerCudaQuantization || undefined,
      parallel_worker_mode: "manual" as const,
      parallel_manual_max: parallelWorkers.selectedParallelWorkerLimit,
    };

    persistedSettingsRef.current = nextSettings;
    savePersistedSettings(nextSettings);
  }, [
    cleanerOptions.cleanerBackend,
    cleanerOptions.cleanerBackendConfigured,
    cleanerOptions.cleanerOptionsHydrated,
    cleanerOptions.cleanerCudaDevice,
    cleanerOptions.cleanerCudaQuantization,
    cleanerOptions.cleanerModel,
    parallelWorkers.selectedParallelWorkerLimit,
    ttsOptions.selectedEngine,
    ttsOptions.selectedQuantization,
    ttsOptions.selectedVoice,
    ttsOptions.speed,
    ttsOptions.loading,
    runtimeSelectionGeneration,
  ]);

  const { modelOptions: _modelOptions, systemCapabilities: _systemCapabilities, ...ttsResult } = ttsOptions;
  const { cleanerOptionsHydrated: _cleanerOptionsHydrated, ...cleanerResult } = cleanerOptions;

  return {
    ...ttsResult,
    ...cleanerResult,
    ...parallelWorkers,
  };
}
