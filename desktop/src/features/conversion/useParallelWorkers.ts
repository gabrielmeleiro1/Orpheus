import { useCallback, useEffect, useMemo, useState, type MutableRefObject } from "react";

import type { ApiClient } from "../../api/client";
import type {
  CleanerBackendOption,
  ParallelWorkerRecommendation,
  SystemCapabilitiesResponse,
} from "../../types/api";

import { buildCleanerRuntimePayloadFields } from "./cleanerBackendHelpers";
import { normalizeQuantizationValue } from "./conversionPanelHelpers";
import type { PersistedSettings } from "./conversionOptionsShared";

const DEFAULT_PARALLEL_WORKER_MODE = "manual";
const DEFAULT_PARALLEL_WORKER_LIMIT = 4;

export interface ParallelWarmupContext {
  selectedVoice: string;
  speed: number;
  selectedEngine: string;
  selectedQuantization: string;
  cleanerBackend: string;
  selectedCleanerBackend: CleanerBackendOption | null;
  cleanerModel: string;
  cleanerApiKey: string;
  cleanerCudaDevice: string;
  cleanerCudaQuantization: string;
}

export interface UseParallelWorkersArgs {
  apiClient: ApiClient | null;
  systemCapabilities: SystemCapabilitiesResponse | null;
  persistedSettingsRef: MutableRefObject<PersistedSettings>;
  warmupContext: ParallelWarmupContext;
}

export interface UseParallelWorkersResult {
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

function formatBytes(bytes: number | null | undefined): string {
  if (!Number.isFinite(bytes ?? NaN) || (bytes ?? 0) <= 0) {
    return "-";
  }
  return `${((bytes ?? 0) / 1024 ** 3).toFixed(1)} GiB`;
}

function clampWorkerLimit(value: number, maxSafe: number): number {
  const parsed = Number.isFinite(value) ? Math.floor(value) : 1;
  return Math.max(1, Math.min(Math.max(1, maxSafe), parsed));
}

export function useParallelWorkers({
  apiClient,
  systemCapabilities,
  persistedSettingsRef,
  warmupContext,
}: UseParallelWorkersArgs): UseParallelWorkersResult {
  const [parallelWorkerMode, setParallelWorkerModeState] = useState<"auto" | "manual">(DEFAULT_PARALLEL_WORKER_MODE);
  const [parallelManualMax, setParallelManualMaxState] = useState(String(DEFAULT_PARALLEL_WORKER_LIMIT));
  const [parallelRecommendation, setParallelRecommendation] = useState<ParallelWorkerRecommendation | null>(null);
  const [parallelWorkerStatus, setParallelWorkerStatus] = useState("Worker recommendation unavailable.");
  const [parallelWorkerError, setParallelWorkerError] = useState(false);
  const [parallelWarmupLoading, setParallelWarmupLoading] = useState(false);

  const manualParallelWorkerUpperBound = useMemo(() => {
    return Math.max(
      1,
      parallelRecommendation?.requested_parallel_chapters ??
        parallelRecommendation?.max_safe_parallel_chapters ??
        DEFAULT_PARALLEL_WORKER_LIMIT,
    );
  }, [parallelRecommendation]);

  const selectedParallelWorkerLimit = useMemo(() => {
    const maxSafe = parallelRecommendation?.max_safe_parallel_chapters ?? 1;
    if (parallelWorkerMode === "auto") {
      return clampWorkerLimit(parallelRecommendation?.recommended_parallel_chapters ?? 1, maxSafe);
    }
    return clampWorkerLimit(Number.parseInt(parallelManualMax, 10), manualParallelWorkerUpperBound);
  }, [manualParallelWorkerUpperBound, parallelManualMax, parallelRecommendation, parallelWorkerMode]);

  useEffect(() => {
    if (!apiClient) {
      setParallelWorkerModeState(DEFAULT_PARALLEL_WORKER_MODE);
      setParallelManualMaxState(String(DEFAULT_PARALLEL_WORKER_LIMIT));
      setParallelRecommendation(null);
      setParallelWorkerStatus("Worker recommendation unavailable.");
      setParallelWorkerError(false);
      setParallelWarmupLoading(false);
      return;
    }

    if (!systemCapabilities) {
      return;
    }

    const recommendation = systemCapabilities.parallel_worker_recommendation ?? null;
    setParallelRecommendation(recommendation);

    const persisted = persistedSettingsRef.current;
    if (persisted.parallel_worker_mode === "auto") {
      setParallelWorkerModeState("auto");
    } else {
      setParallelWorkerModeState(DEFAULT_PARALLEL_WORKER_MODE);
    }
    if (Number.isFinite(persisted.parallel_manual_max) && (persisted.parallel_manual_max ?? 0) > 0) {
      setParallelManualMaxState(String(persisted.parallel_manual_max));
    } else if (recommendation) {
      setParallelManualMaxState(String(recommendation.requested_parallel_chapters));
    }

    if (recommendation) {
      setParallelWorkerStatus(
        `Recommended ${recommendation.recommended_parallel_chapters}; max ${recommendation.max_safe_parallel_chapters}. ${recommendation.calculation}`,
      );
      setParallelWorkerError(!recommendation.memory_measurement_available);
    }
  }, [apiClient, persistedSettingsRef, systemCapabilities]);

  const setParallelWorkerMode = useCallback((value: "auto" | "manual") => {
    setParallelWorkerModeState(value);
  }, []);

  const setParallelManualMax = useCallback(
    (value: string) => {
      const digitsOnly = value.replace(/[^\d]/g, "");
      if (!digitsOnly) {
        setParallelManualMaxState("");
        return;
      }
      const next = clampWorkerLimit(Number.parseInt(digitsOnly, 10), manualParallelWorkerUpperBound);
      setParallelManualMaxState(String(next));
    },
    [manualParallelWorkerUpperBound],
  );

  const runParallelWarmup = useCallback(async () => {
    if (!apiClient) {
      setParallelWorkerError(true);
      setParallelWorkerStatus("Backend is not connected.");
      return;
    }

    const {
      selectedVoice,
      speed,
      selectedEngine,
      selectedQuantization,
      cleanerBackend,
      selectedCleanerBackend,
      cleanerModel,
      cleanerApiKey,
      cleanerCudaDevice,
      cleanerCudaQuantization,
    } = warmupContext;

    setParallelWarmupLoading(true);
    setParallelWorkerError(false);
    setParallelWorkerStatus("Running model warm-up memory calibration...");
    try {
      const quantization = normalizeQuantizationValue(selectedQuantization);
      const requestedWarmupMax =
        parallelWorkerMode === "manual"
          ? selectedParallelWorkerLimit
          : parallelRecommendation?.requested_parallel_chapters ??
            parallelRecommendation?.max_safe_parallel_chapters ??
            Math.max(DEFAULT_PARALLEL_WORKER_LIMIT, Number.parseInt(parallelManualMax, 10) || 1);
      const cleanerFields = buildCleanerRuntimePayloadFields({
        selectedBackend: selectedCleanerBackend,
        cleanerBackend,
        cleanerModel,
        cleanerApiKey,
        cleanerCudaDevice,
        cleanerCudaQuantization,
      });
      const recommendation = await apiClient.warmupParallelWorkers({
        voice: selectedVoice || "am_adam",
        speed,
        tts_engine: selectedEngine || null,
        tts_quantization: quantization,
        ...cleanerFields,
        max_parallel_chapters: requestedWarmupMax,
      });
      setParallelRecommendation(recommendation);
      setParallelManualMaxState(String(recommendation.requested_parallel_chapters));
      setParallelWorkerStatus(
        `Warm-up estimate ${formatBytes(recommendation.estimated_per_worker_memory_bytes)} per worker. Recommended ${recommendation.recommended_parallel_chapters}; max ${recommendation.max_safe_parallel_chapters}. ${recommendation.reason}`,
      );
      setParallelWorkerError(!recommendation.memory_measurement_available);
    } catch (warmupError: unknown) {
      setParallelWorkerError(true);
      setParallelWorkerStatus(warmupError instanceof Error ? warmupError.message : "Parallel warm-up failed.");
    } finally {
      setParallelWarmupLoading(false);
    }
  }, [
    apiClient,
    parallelManualMax,
    parallelRecommendation,
    parallelWorkerMode,
    selectedParallelWorkerLimit,
    warmupContext,
  ]);

  return {
    parallelWorkerMode,
    setParallelWorkerMode,
    parallelManualMax,
    setParallelManualMax,
    manualParallelWorkerUpperBound,
    selectedParallelWorkerLimit,
    parallelRecommendation,
    parallelWorkerStatus,
    parallelWorkerError,
    parallelWarmupLoading,
    runParallelWarmup,
  };
}
