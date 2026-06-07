import { useCallback } from "react";

import type {
  ConversionMode,
  ConversionRuntimeOptions,
  StartConversionRequest,
} from "../../types/api";
import { normalizeQuantizationValue } from "./conversionPanelHelpers";
import type { ConversionQueueItem } from "./conversionPanelModel";
import type { useConversionOptions } from "./useConversionOptions";

type ConversionOptionState = ReturnType<typeof useConversionOptions>;

export function buildRuntimeOptions(
  optionState: ConversionOptionState,
): ConversionRuntimeOptions {
  const quantization = normalizeQuantizationValue(optionState.selectedQuantization);
  return {
    voice: optionState.selectedVoice,
    speed: optionState.speed,
    tts_engine: optionState.selectedEngine || null,
    tts_quantization: quantization,
    cleaner_backend: optionState.cleanerBackend,
    cleaner_model:
      optionState.cleanerBackend === "mlx" ? optionState.cleanerModel || null : null,
    cleaner_api_model:
      optionState.cleanerBackend === "api" ? optionState.cleanerModel || null : null,
    cleaner_api_key:
      optionState.cleanerBackend === "api"
        ? optionState.cleanerApiKey.trim() || null
        : null,
    tts_options: { quantization },
    parallel_worker_mode: optionState.parallelWorkerMode,
    max_parallel_chapters: optionState.selectedParallelWorkerLimit,
    enable_parallel: optionState.selectedParallelWorkerLimit > 1,
  };
}

interface UseConversionSubmitPayloadArgs {
  optionState: ConversionOptionState;
  conversionMode: ConversionMode;
  sourcePath: string;
  titleOverride: string;
  authorOverride: string;
}

export function useConversionSubmitPayload({
  optionState,
  conversionMode,
  sourcePath,
  titleOverride,
  authorOverride,
}: UseConversionSubmitPayloadArgs) {
  const buildStartPayload = useCallback(
    (item?: ConversionQueueItem): StartConversionRequest => {
      return {
        conversion_mode: item?.conversionMode ?? conversionMode,
        source_path: (item?.sourcePath ?? sourcePath).trim(),
        title: (item?.title ?? titleOverride).trim() || null,
        author: (item?.author ?? authorOverride).trim() || null,
        runtime_options: buildRuntimeOptions(optionState),
      };
    },
    [
      authorOverride,
      conversionMode,
      optionState,
      sourcePath,
      titleOverride,
    ],
  );

  return { buildStartPayload };
}
