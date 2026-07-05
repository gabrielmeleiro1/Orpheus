import { describe, expect, it } from "vitest";

import type { CleanerBackendOption, SystemCapabilitiesResponse } from "../../types/api";
import {
  buildCleanerRuntimePayloadFields,
  cleanerBackendUsesCudaFields,
  defaultCleanerCudaDevice,
  defaultCleanerCudaQuantization,
  deriveCleanerRuntimeStatus,
  isCleanerBackendRuntimeAvailable,
  preferredCleanerBackendId,
  resolveCleanerBackendOptions,
  resolveInitialCleanerBackendId,
} from "./cleanerBackendHelpers";

function cleanerBackendOption(
  overrides: Partial<CleanerBackendOption> = {},
): CleanerBackendOption {
  return {
    id: "mlx",
    display_name: "Local MLX",
    is_local: true,
    requires_model: true,
    requires_api_key: false,
    models: ["mlx-community/Llama-3.2-3B-Instruct-4bit"],
    ...overrides,
  };
}

function capabilities(
  availability: Record<string, boolean> = {},
  cleanerError: string | null = null,
): SystemCapabilitiesResponse {
  return {
    default_tts_engine: "kokoro",
    default_tts_quantization: "bf16",
    cleaner_runtime_availability: availability,
    runtime_probe: {
      state: "fresh",
      refresh_in_progress: false,
      cleaner: {
        available: cleanerError === null,
        latency_ms: null,
        error: cleanerError,
      },
    },
  };
}

describe("cleaner backend runtime availability", () => {
  it("normalizes backend metadata used by desktop runtime decisions", () => {
    expect(
      resolveCleanerBackendOptions({
        tts_engines: [],
        cleaner_models: [],
        api_cleaner_models: [],
        cleaner_backend_options: [
          {
            name: " cuda ",
            display_name: " Local CUDA ",
            is_local: true,
            requires_api_key: false,
            models: [" model-a ", "", "model-b"],
            supported_quantizations: [" 4bit ", "", " fp16 "],
            default_device: " cuda ",
          },
          {
            name: "   ",
            display_name: "Ignored",
            is_local: true,
            requires_api_key: false,
            models: [],
          },
        ],
      }),
    ).toEqual([
      {
        id: "cuda",
        display_name: "Local CUDA",
        is_local: true,
        requires_model: true,
        requires_api_key: false,
        models: ["model-a", "model-b"],
        supported_quantizations: ["4bit", "fp16"],
        default_device: "cuda",
      },
    ]);
  });

  it("keeps remote backends available regardless of runtime capability probes", () => {
    const option = cleanerBackendOption({
      id: "api",
      display_name: "Remote API",
      is_local: false,
      requires_model: false,
      requires_api_key: true,
      models: [],
    });

    expect(isCleanerBackendRuntimeAvailable(option, capabilities({ api: false }))).toBe(true);
  });

  it("gates local backends only when capabilities explicitly mark them unavailable", () => {
    const option = cleanerBackendOption({ id: "cuda", display_name: "Local CUDA" });

    expect(isCleanerBackendRuntimeAvailable(option, null)).toBe(true);
    expect(isCleanerBackendRuntimeAvailable(option, capabilities({}))).toBe(true);
    expect(isCleanerBackendRuntimeAvailable(option, capabilities({ cuda: true }))).toBe(true);
    expect(isCleanerBackendRuntimeAvailable(option, capabilities({ cuda: false }))).toBe(false);
  });
});

describe("cleaner backend CUDA metadata", () => {
  it("uses CUDA payload fields only when backend metadata advertises CUDA controls", () => {
    expect(cleanerBackendUsesCudaFields(cleanerBackendOption({ id: "cuda" }))).toBe(false);
    expect(
      cleanerBackendUsesCudaFields(cleanerBackendOption({ id: "cuda", default_device: "cuda" })),
    ).toBe(true);
    expect(
      cleanerBackendUsesCudaFields(
        cleanerBackendOption({ id: "local-gpu", supported_quantizations: ["4bit"] }),
      ),
    ).toBe(true);
  });

  it("builds CUDA runtime payload fields from metadata-driven backend controls", () => {
    const selectedBackend = cleanerBackendOption({
      id: "local-gpu",
      display_name: "Local GPU",
      default_device: "cuda",
      supported_quantizations: ["4bit", "8bit"],
    });

    expect(
      buildCleanerRuntimePayloadFields({
        selectedBackend,
        cleanerBackend: " local-gpu ",
        cleanerModel: " local/model ",
        cleanerApiKey: "ignored",
        cleanerCudaDevice: " cuda:1 ",
        cleanerCudaQuantization: " 8bit ",
      }),
    ).toEqual({
      cleaner_backend: "local-gpu",
      cleaner_model: "local/model",
      cleaner_cuda_device: "cuda:1",
      cleaner_cuda_quantization: "8bit",
    });
  });

  it("omits CUDA payload fields when metadata does not advertise CUDA controls", () => {
    expect(
      buildCleanerRuntimePayloadFields({
        selectedBackend: cleanerBackendOption({ id: "cuda" }),
        cleanerBackend: "cuda",
        cleanerModel: "local/model",
        cleanerApiKey: "",
        cleanerCudaDevice: "cuda:0",
        cleanerCudaQuantization: "4bit",
      }),
    ).toEqual({
      cleaner_backend: "cuda",
      cleaner_model: "local/model",
    });
  });

  it("derives CUDA defaults from backend metadata", () => {
    const option = cleanerBackendOption({
      id: "cuda",
      default_device: "cuda",
      supported_quantizations: ["4bit", "8bit"],
    });

    expect(defaultCleanerCudaDevice(option)).toBe("cuda");
    expect(defaultCleanerCudaQuantization(option)).toBe("4bit");
    expect(defaultCleanerCudaDevice(cleanerBackendOption({ default_device: " cuda:0 " }))).toBe(
      "cuda:0",
    );
    expect(defaultCleanerCudaQuantization(cleanerBackendOption())).toBe("");
  });
});

describe("cleaner runtime status", () => {
  it("reports missing and remote backend states without probing local runtime", () => {
    expect(deriveCleanerRuntimeStatus(null, null)).toEqual({
      message: "No cleaner backend options available.",
      isError: true,
    });
    expect(
      deriveCleanerRuntimeStatus(
        capabilities({ api: false }),
        cleanerBackendOption({
          id: "api",
          display_name: "Remote API",
          is_local: false,
          requires_model: false,
          requires_api_key: true,
          models: [],
        }),
      ),
    ).toEqual({ message: "Remote API cleaner ready.", isError: false });
  });

  it("reports local runtime ready, unavailable with detail, and pending states", () => {
    const option = cleanerBackendOption({ id: "cuda", display_name: "Local CUDA" });

    expect(deriveCleanerRuntimeStatus(capabilities({ cuda: true }), option)).toEqual({
      message: "Local CUDA cleaner ready.",
      isError: false,
    });
    expect(deriveCleanerRuntimeStatus(capabilities({ cuda: false }, "missing torch"), option)).toEqual({
      message: "Local CUDA cleaner unavailable (missing torch). Choose another backend if needed.",
      isError: true,
    });
    expect(deriveCleanerRuntimeStatus(capabilities({}), option)).toEqual({
      message: "Checking Local CUDA cleaner runtime...",
      isError: false,
    });
  });

  it("keeps the local runtime status prefix for backend labels without it", () => {
    const option = cleanerBackendOption({ id: "cuda", display_name: "CUDA" });

    expect(deriveCleanerRuntimeStatus(capabilities({ cuda: true }), option)).toEqual({
      message: "Local CUDA cleaner ready.",
      isError: false,
    });
    expect(deriveCleanerRuntimeStatus(capabilities({ cuda: false }), option)).toEqual({
      message: "Local CUDA cleaner unavailable. Choose another backend if needed.",
      isError: true,
    });
    expect(deriveCleanerRuntimeStatus(capabilities({}), option)).toEqual({
      message: "Checking local CUDA cleaner runtime...",
      isError: false,
    });
  });
});

describe("initial cleaner backend selection policy", () => {
  const apiOption = cleanerBackendOption({
    id: "api",
    display_name: "Cloud API",
    is_local: false,
    requires_api_key: true,
    models: [],
  });
  const cudaOption = cleanerBackendOption({ id: "cuda", display_name: "Local CUDA" });
  const mlxOption = cleanerBackendOption({ id: "mlx", display_name: "Local MLX" });

  it("prefers an available local backend over the api backend in server order", () => {
    // Server declares api first, but a local backend must win when available.
    expect(preferredCleanerBackendId([apiOption, cudaOption, mlxOption])).toBe("cuda");
    expect(resolveInitialCleanerBackendId([apiOption, cudaOption, mlxOption], "")).toEqual({
      backendId: "cuda",
      restoredPersistedBackend: false,
    });
  });

  it("falls back to the api backend only when no local backend is available", () => {
    expect(preferredCleanerBackendId([apiOption])).toBe("api");
    expect(resolveInitialCleanerBackendId([apiOption], "")).toEqual({
      backendId: "api",
      restoredPersistedBackend: false,
    });
    expect(preferredCleanerBackendId([])).toBe("");
  });

  it("honors a persisted, still-available backend choice over the default", () => {
    expect(resolveInitialCleanerBackendId([apiOption, cudaOption, mlxOption], "api")).toEqual({
      backendId: "api",
      restoredPersistedBackend: true,
    });
  });

  it("ignores a persisted backend that is no longer available", () => {
    expect(resolveInitialCleanerBackendId([apiOption, mlxOption], "cuda")).toEqual({
      backendId: "mlx",
      restoredPersistedBackend: false,
    });
  });
});
