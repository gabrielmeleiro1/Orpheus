import { useEffect, useRef, useState } from "react";

import type { ApiClient } from "../../api/client";
import type { ParallelWorkerRecommendation, TermSelectOption } from "../../types/api";

import { readPersistedSettings, savePersistedSettings } from "./conversionOptionsShared";
import { useCleanerOptions } from "./useCleanerOptions";
import { useParallelWorkers } from "./useParallelWorkers";
import { useTtsOptions } from "./useTtsOptions";

interface UseConversionOptionsArgs {
  apiClient: ApiClient | null;
  conversionMode: string;
}

export interface UseConversionOptionsResult {
  loading: boolean;
  error: string;
  selectedEngine: string;
  setSelectedEngine: (value: string) => void;
  selectedVoice: string;
  setSelectedVoice: (value: string) => void;
  engineOptions: TermSelectOption[];
  filteredVoiceOptions: TermSelectOption[];
  voiceFilter: string;
  setVoiceFilter: (value: string) => void;
  speedInput: string;
  speed: number;
  setSpeed: (value: string) => void;
  selectedQuantization: string;
  setSelectedQuantization: (value: string) => void;
  quantizationOptions: TermSelectOption[];
  cleanerBackend: string;
  setCleanerBackend: (value: string) => void;
  cleanerBackendOptions: TermSelectOption[];
  cleanerBackendConfigured: boolean;
  cleanerModel: string;
  setCleanerModel: (value: string) => void;
  cleanerModelOptions: TermSelectOption[];
  cleanerApiKey: string;
  setCleanerApiKey: (value: string) => void;
  fetchOpenAiModelIds: () => Promise<void>;
  openAiModelFetchStatus: string;
  openAiModelFetchLoading: boolean;
  openAiModelFetchError: boolean;
  cleanerRuntimeStatus: string;
  cleanerRuntimeError: boolean;
  parallelWorkerMode: "auto" | "manual";
  setParallelWorkerMode: (value: "auto" | "manual") => void;
  parallelManualMax: string;
  setParallelManualMax: (value: string) => void;
  manualParallelWorkerUpperBound: number;
  selectedParallelWorkerLimit: number;
  parallelRecommendation: ParallelWorkerRecommendation | null;
  parallelWorkerStatus: string;
  parallelWorkerError: boolean;
  parallelWarmupLoading: boolean;
  runParallelWarmup: () => Promise<void>;
}

export function useConversionOptions({ apiClient, conversionMode }: UseConversionOptionsArgs): UseConversionOptionsResult {
  const persistedSettingsRef = useRef(readPersistedSettings());
  const settingsHydratedRef = useRef(false);
  const [conversionModeError, setConversionModeError] = useState("");

  const ttsOptions = useTtsOptions({ apiClient, persistedSettingsRef, settingsHydratedRef });

  const cleanerOptions = useCleanerOptions({
    apiClient,
    modelOptions: ttsOptions.modelOptions,
    systemCapabilities: ttsOptions.systemCapabilities,
    persistedSettingsRef,
  });

  const parallelWorkers = useParallelWorkers({
    apiClient,
    systemCapabilities: ttsOptions.systemCapabilities,
    persistedSettingsRef,
    warmupContext: {
      selectedVoice: ttsOptions.selectedVoice,
      speed: ttsOptions.speed,
      selectedEngine: ttsOptions.selectedEngine,
      selectedQuantization: ttsOptions.selectedQuantization,
      cleanerBackend: cleanerOptions.cleanerBackend,
      cleanerModel: cleanerOptions.cleanerModel,
      cleanerApiKey: cleanerOptions.cleanerApiKey,
    },
  });

  useEffect(() => {
    if (!settingsHydratedRef.current) {
      return;
    }

    const nextSettings = {
      tts_engine: ttsOptions.selectedEngine || undefined,
      voice: ttsOptions.selectedVoice || undefined,
      speed: ttsOptions.speed,
      tts_quantization: ttsOptions.selectedQuantization || undefined,
      cleaner_backend: cleanerOptions.cleanerBackend || undefined,
      cleaner_model: cleanerOptions.cleanerModel || undefined,
      cleaner_backend_configured: cleanerOptions.cleanerBackendConfigured,
      parallel_worker_mode: parallelWorkers.parallelWorkerMode,
      parallel_manual_max: parallelWorkers.selectedParallelWorkerLimit,
    };

    persistedSettingsRef.current = nextSettings;
    savePersistedSettings(nextSettings);
  }, [
    cleanerOptions.cleanerBackend,
    cleanerOptions.cleanerBackendConfigured,
    cleanerOptions.cleanerModel,
    parallelWorkers.parallelWorkerMode,
    parallelWorkers.selectedParallelWorkerLimit,
    ttsOptions.selectedEngine,
    ttsOptions.selectedQuantization,
    ttsOptions.selectedVoice,
    ttsOptions.speed,
  ]);

  useEffect(() => {
    if (conversionMode !== "audiobook" && conversionMode !== "document_audio") {
      setConversionModeError("invalid conversion mode");
    } else {
      setConversionModeError("");
    }
  }, [conversionMode]);

  const error = conversionModeError || ttsOptions.error;

  return {
    loading: ttsOptions.loading,
    error,
    selectedEngine: ttsOptions.selectedEngine,
    setSelectedEngine: ttsOptions.setSelectedEngine,
    selectedVoice: ttsOptions.selectedVoice,
    setSelectedVoice: ttsOptions.setSelectedVoice,
    engineOptions: ttsOptions.engineOptions,
    filteredVoiceOptions: ttsOptions.filteredVoiceOptions,
    voiceFilter: ttsOptions.voiceFilter,
    setVoiceFilter: ttsOptions.setVoiceFilter,
    speedInput: ttsOptions.speedInput,
    speed: ttsOptions.speed,
    setSpeed: ttsOptions.setSpeed,
    selectedQuantization: ttsOptions.selectedQuantization,
    setSelectedQuantization: ttsOptions.setSelectedQuantization,
    quantizationOptions: ttsOptions.quantizationOptions,
    cleanerBackend: cleanerOptions.cleanerBackend,
    setCleanerBackend: cleanerOptions.setCleanerBackend,
    cleanerBackendOptions: cleanerOptions.cleanerBackendOptions,
    cleanerBackendConfigured: cleanerOptions.cleanerBackendConfigured,
    cleanerModel: cleanerOptions.cleanerModel,
    setCleanerModel: cleanerOptions.setCleanerModel,
    cleanerModelOptions: cleanerOptions.cleanerModelOptions,
    cleanerApiKey: cleanerOptions.cleanerApiKey,
    setCleanerApiKey: cleanerOptions.setCleanerApiKey,
    fetchOpenAiModelIds: cleanerOptions.fetchOpenAiModelIds,
    openAiModelFetchStatus: cleanerOptions.openAiModelFetchStatus,
    openAiModelFetchLoading: cleanerOptions.openAiModelFetchLoading,
    openAiModelFetchError: cleanerOptions.openAiModelFetchError,
    cleanerRuntimeStatus: cleanerOptions.cleanerRuntimeStatus,
    cleanerRuntimeError: cleanerOptions.cleanerRuntimeError,
    parallelWorkerMode: parallelWorkers.parallelWorkerMode,
    setParallelWorkerMode: parallelWorkers.setParallelWorkerMode,
    parallelManualMax: parallelWorkers.parallelManualMax,
    setParallelManualMax: parallelWorkers.setParallelManualMax,
    manualParallelWorkerUpperBound: parallelWorkers.manualParallelWorkerUpperBound,
    selectedParallelWorkerLimit: parallelWorkers.selectedParallelWorkerLimit,
    parallelRecommendation: parallelWorkers.parallelRecommendation,
    parallelWorkerStatus: parallelWorkers.parallelWorkerStatus,
    parallelWorkerError: parallelWorkers.parallelWorkerError,
    parallelWarmupLoading: parallelWorkers.parallelWarmupLoading,
    runParallelWarmup: parallelWorkers.runParallelWarmup,
  };
}
