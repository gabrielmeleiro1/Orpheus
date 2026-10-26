import { useCallback, useEffect, useMemo, useState, type MutableRefObject } from "react";

import type { ApiClient } from "../../api/client";
import type {
  ParallelWorkerRecommendation,
  SystemCapabilitiesResponse,
} from "../../types/api";

import type { PersistedSettings } from "./conversionOptionsShared";

// Keep the first render safe before the backend has returned its memory
// recommendation. A measured recommendation may raise this value later.
const DEFAULT_PARALLEL_WORKER_LIMIT = 1;

interface UseParallelWorkersArgs {
  apiClient: ApiClient | null;
  systemCapabilities: SystemCapabilitiesResponse | null;
  persistedSettingsRef: MutableRefObject<PersistedSettings>;
}

export interface UseParallelWorkersResult {
  parallelManualMax: string;
  setParallelManualMax: (value: string) => void;
  manualParallelWorkerUpperBound: number;
  selectedParallelWorkerLimit: number;
  /** How many of the selected chapters this computer's memory lets run at once. */
  usableParallelWorkerLimit: number;
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

function shortWorkerLine(recommendation: ParallelWorkerRecommendation, selected: number): string {
  const maxSafe = Math.max(1, recommendation.max_safe_parallel_chapters);
  // A larger request is kept, but only what memory allows will run; say which.
  if (maxSafe <= 1) {
    return selected > 1
      ? `Runs 1 of the ${selected} requested: this computer has memory for one chapter at a time.`
      : "This computer has memory for one chapter at a time.";
  }
  if (selected > maxSafe) {
    return `Runs ${maxSafe} of the ${selected} requested: this computer has memory for ${maxSafe} chapters at a time.`;
  }
  let headline = `Recommended: ${recommendation.recommended_parallel_chapters}`;
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

  const parallelWorkerError = useMemo(() => {
    return parallelRecommendation ? !parallelRecommendation.memory_measurement_available : false;
  }, [parallelRecommendation]);

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

  const usableParallelWorkerLimit = parallelRecommendation
    ? Math.min(selectedParallelWorkerLimit, Math.max(1, parallelRecommendation.max_safe_parallel_chapters))
    : selectedParallelWorkerLimit;

  const parallelWorkerStatus = useMemo(() => {
    return parallelRecommendation
      ? shortWorkerLine(parallelRecommendation, selectedParallelWorkerLimit)
      : "Worker recommendation unavailable.";
  }, [parallelRecommendation, selectedParallelWorkerLimit]);

  useEffect(() => {
    if (!apiClient) {
      setParallelManualMaxState(String(DEFAULT_PARALLEL_WORKER_LIMIT));
      setParallelRecommendation(null);
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
      setParallelManualMaxState(String(recommendation.recommended_parallel_chapters));
    }
  }, [apiClient, persistedSettingsRef, systemCapabilities]);

  const setParallelManualMax = useCallback(
    (value: string) => {
      if (!value.trim()) {
        setParallelManualMaxState("");
        return;
      }
      const next = clampWorkerLimit(Number(value), manualParallelWorkerUpperBound);
      setParallelManualMaxState(String(next));
    },
    [manualParallelWorkerUpperBound],
  );

  return {
    parallelManualMax,
    setParallelManualMax,
    manualParallelWorkerUpperBound,
    selectedParallelWorkerLimit,
    usableParallelWorkerLimit,
    parallelRecommendation,
    parallelWorkerStatus,
    parallelWorkerError,
  };
}
