import { useCallback, useEffect, useMemo, useState, type MutableRefObject } from "react";

import type { ApiClient } from "../../api/client";
import type {
  ParallelWorkerRecommendation,
  SystemCapabilitiesResponse,
} from "../../types/api";

import type { PersistedSettings } from "./conversionOptionsShared";

const DEFAULT_PARALLEL_WORKER_LIMIT = 4;

export interface UseParallelWorkersArgs {
  apiClient: ApiClient | null;
  systemCapabilities: SystemCapabilitiesResponse | null;
  persistedSettingsRef: MutableRefObject<PersistedSettings>;
}

export interface UseParallelWorkersResult {
  parallelManualMax: string;
  setParallelManualMax: (value: string) => void;
  manualParallelWorkerUpperBound: number;
  selectedParallelWorkerLimit: number;
  parallelRecommendation: ParallelWorkerRecommendation | null;
  parallelWorkerStatus: string;
  parallelWorkerError: boolean;
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

function shortWorkerLine(recommendation: ParallelWorkerRecommendation): string {
  let headline = `Recommended for this machine: ${recommendation.recommended_parallel_chapters}`;
  if (recommendation.max_safe_parallel_chapters > recommendation.recommended_parallel_chapters) {
    headline += ` (up to ${recommendation.max_safe_parallel_chapters})`;
  }
  const free = formatBytes(recommendation.available_memory_bytes);
  return free === "-" ? headline : `${headline} · ${free} memory free`;
}

export function useParallelWorkers({
  apiClient,
  systemCapabilities,
  persistedSettingsRef,
}: UseParallelWorkersArgs): UseParallelWorkersResult {
  const [parallelManualMax, setParallelManualMaxState] = useState(String(DEFAULT_PARALLEL_WORKER_LIMIT));
  const [parallelRecommendation, setParallelRecommendation] = useState<ParallelWorkerRecommendation | null>(null);
  const [parallelWorkerStatus, setParallelWorkerStatus] = useState("Worker recommendation unavailable.");
  const [parallelWorkerError, setParallelWorkerError] = useState(false);

  const manualParallelWorkerUpperBound = useMemo(() => {
    return Math.max(
      1,
      parallelRecommendation?.requested_parallel_chapters ??
        parallelRecommendation?.max_safe_parallel_chapters ??
        DEFAULT_PARALLEL_WORKER_LIMIT,
    );
  }, [parallelRecommendation]);

  const selectedParallelWorkerLimit = useMemo(() => {
    return clampWorkerLimit(Number.parseInt(parallelManualMax, 10), manualParallelWorkerUpperBound);
  }, [manualParallelWorkerUpperBound, parallelManualMax]);

  useEffect(() => {
    if (!apiClient) {
      setParallelManualMaxState(String(DEFAULT_PARALLEL_WORKER_LIMIT));
      setParallelRecommendation(null);
      setParallelWorkerStatus("Worker recommendation unavailable.");
      setParallelWorkerError(false);
      return;
    }

    if (!systemCapabilities) {
      return;
    }

    const recommendation = systemCapabilities.parallel_worker_recommendation ?? null;
    setParallelRecommendation(recommendation);

    const persisted = persistedSettingsRef.current;
    if (Number.isFinite(persisted.parallel_manual_max) && (persisted.parallel_manual_max ?? 0) > 0) {
      setParallelManualMaxState(String(persisted.parallel_manual_max));
    } else if (recommendation) {
      setParallelManualMaxState(String(recommendation.requested_parallel_chapters));
    }

    if (recommendation) {
      setParallelWorkerStatus(shortWorkerLine(recommendation));
      setParallelWorkerError(!recommendation.memory_measurement_available);
    }
  }, [apiClient, persistedSettingsRef, systemCapabilities]);

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

  return {
    parallelManualMax,
    setParallelManualMax,
    manualParallelWorkerUpperBound,
    selectedParallelWorkerLimit,
    parallelRecommendation,
    parallelWorkerStatus,
    parallelWorkerError,
  };
}
