import { describe, expect, it } from "vitest";

import type { CleanerBackendOption } from "../../types/api";
import type { UseConversionOptionsResult } from "./useConversionOptions";
import { buildRuntimeOptions } from "./useConversionSubmitPayload";

type OptionState = UseConversionOptionsResult;

function backend(overrides: Partial<CleanerBackendOption>): CleanerBackendOption {
  return {
    id: "mlx",
    display_name: "Local MLX",
    is_local: true,
    requires_model: true,
    requires_api_key: false,
    models: ["model"],
    ...overrides,
  };
}

function optionState(overrides: Partial<OptionState> = {}): OptionState {
  const base: OptionState = {
    loading: false,
    error: "",
    selectedEngine: "kokoro",
    setSelectedEngine: () => undefined,
    selectedVoice: "af_sarah",
    setSelectedVoice: () => undefined,
    engineOptions: [],
    filteredVoiceOptions: [],
    voiceFilter: "",
    setVoiceFilter: () => undefined,
    speedInput: "1",
    speed: 1,
    setSpeed: () => undefined,
    selectedQuantization: "q8",
    setSelectedQuantization: () => undefined,
    quantizationOptions: [],
    cleanerBackend: "mlx",
    setCleanerBackend: () => undefined,
    cleanerBackendOptions: [],
    cleanerBackendConfigured: true,
    selectedCleanerBackend: backend({ id: "mlx" }),
    cleanerModel: " mlx/model ",
    setCleanerModel: () => undefined,
    cleanerCudaDevice: "",
    setCleanerCudaDevice: () => undefined,
    cleanerCudaQuantization: "",
    setCleanerCudaQuantization: () => undefined,
    cleanerApiKey: "",
    setCleanerApiKey: () => undefined,
    fetchOpenAiModelIds: async () => undefined,
    cleanerModelOptions: [],
    openAiModelFetchStatus: "",
    openAiModelFetchLoading: false,
    openAiModelFetchError: false,
    cleanerRuntimeStatus: "",
    cleanerRuntimeError: false,
    parallelManualMax: "1",
    setParallelManualMax: () => undefined,
    manualParallelWorkerUpperBound: 4,
    selectedParallelWorkerLimit: 1,
    parallelRecommendation: null,
    parallelWorkerStatus: "",
    parallelWorkerError: false,
  };
  return { ...base, ...overrides } as OptionState;
}

describe("conversion runtime option payloads", () => {
  it("builds kokoro local cleaner runtime options", () => {
    expect(buildRuntimeOptions(optionState())).toEqual({
      voice: "af_sarah",
      speed: 1,
      tts_engine: "kokoro",
      tts_quantization: "8bit",
      cleaner_backend: "mlx",
      cleaner_model: "mlx/model",
      tts_options: { quantization: "8bit" },
      parallel_worker_mode: "manual",
      max_parallel_chapters: 1,
      enable_parallel: false,
    });
  });

  it("builds openai remote cleaner runtime options with API credentials", () => {
    expect(
      buildRuntimeOptions(
        optionState({
          selectedEngine: "openai",
          selectedVoice: "alloy",
          selectedQuantization: "",
          cleanerBackend: "openai",
          selectedCleanerBackend: backend({
            id: "openai",
            is_local: false,
            requires_model: false,
            requires_api_key: true,
            models: [],
          }),
          cleanerModel: " gpt-4.1-mini ",
          cleanerApiKey: " sk-test ",
          selectedParallelWorkerLimit: 3,
        }),
      ),
    ).toEqual({
      voice: "alloy",
      speed: 1,
      tts_engine: "openai",
      tts_quantization: null,
      cleaner_backend: "openai",
      cleaner_api_model: "gpt-4.1-mini",
      cleaner_api_key: "sk-test",
      tts_options: { quantization: null },
      parallel_worker_mode: "manual",
      max_parallel_chapters: 3,
      enable_parallel: true,
    });
  });

  it("keeps cleaner backend id when no selected backend metadata is available", () => {
    expect(
      buildRuntimeOptions(
        optionState({
          selectedEngine: "",
          cleanerBackend: "unknown",
          selectedCleanerBackend: null,
          selectedParallelWorkerLimit: 2,
        }),
      ),
    ).toMatchObject({
      tts_engine: null,
      cleaner_backend: "unknown",
      enable_parallel: true,
      max_parallel_chapters: 2,
    });
  });

  it("includes CUDA cleaner controls only for metadata that advertises them", () => {
    expect(
      buildRuntimeOptions(
        optionState({
          cleanerBackend: "cuda",
          selectedCleanerBackend: backend({
            id: "cuda",
            default_device: "cuda",
            supported_quantizations: ["4bit"],
          }),
          cleanerCudaDevice: " cuda:0 ",
          cleanerCudaQuantization: " 4bit ",
        }),
      ),
    ).toMatchObject({
      cleaner_backend: "cuda",
      cleaner_cuda_device: "cuda:0",
      cleaner_cuda_quantization: "4bit",
    });
  });
});
