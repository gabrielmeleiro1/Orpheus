import type {
  CleanerBackendOption,
  CleanerBackendOptionApi,
  ModelOptionsResponse,
  SystemCapabilitiesResponse,
  TermSelectOption,
} from "../../types/api";

function normalizeCleanerBackendOption(apiOption: CleanerBackendOptionApi): CleanerBackendOption | null {
  const id = String(apiOption.name ?? "").trim();
  if (!id) {
    return null;
  }

  const option: CleanerBackendOption = {
    id,
    display_name: String(apiOption.display_name ?? "").trim() || id,
    is_local: Boolean(apiOption.is_local),
    requires_model: apiOption.requires_model ?? (Array.isArray(apiOption.models) && apiOption.models.length > 0),
    requires_api_key: Boolean(apiOption.requires_api_key),
    models: Array.isArray(apiOption.models)
      ? apiOption.models.map((model) => String(model).trim()).filter((model) => model.length > 0)
      : [],
  };

  if (Array.isArray(apiOption.supported_quantizations)) {
    const quantizations = apiOption.supported_quantizations
      .map((value) => String(value).trim())
      .filter((value) => value.length > 0);
    if (quantizations.length > 0) {
      option.supported_quantizations = quantizations;
    }
  }

  const defaultDevice = String(apiOption.default_device ?? "").trim();
  if (defaultDevice) {
    option.default_device = defaultDevice;
  }

  return option;
}

export function resolveCleanerBackendOptions(
  modelOptions: ModelOptionsResponse | null,
): CleanerBackendOption[] {
  const rawOptions = modelOptions?.cleaner_backend_options;
  if (!Array.isArray(rawOptions)) {
    return [];
  }

  const options: CleanerBackendOption[] = [];
  for (const rawOption of rawOptions) {
    const normalized = normalizeCleanerBackendOption(rawOption);
    if (normalized) {
      options.push(normalized);
    }
  }
  return options;
}

export function selectedCleanerBackendOption(
  modelOptions: ModelOptionsResponse | null,
  backendId: string,
): CleanerBackendOption | null {
  const normalizedId = String(backendId ?? "").trim();
  if (!normalizedId) {
    return null;
  }

  return resolveCleanerBackendOptions(modelOptions).find((option) => option.id === normalizedId) ?? null;
}

export function cleanerBackendRequiresApiKey(
  modelOptions: ModelOptionsResponse | null,
  backendId: string,
): boolean {
  return selectedCleanerBackendOption(modelOptions, backendId)?.requires_api_key ?? false;
}

export function isCleanerBackendRuntimeAvailable(
  option: CleanerBackendOption,
  capabilities: SystemCapabilitiesResponse | null,
): boolean {
  if (!option.is_local || option.id === "basic") {
    return true;
  }

  if (!capabilities) {
    return true;
  }

  return capabilities.cleaner_runtime_availability[option.id] !== false;
}

export function filterAvailableCleanerBackendOptions(
  options: CleanerBackendOption[],
  capabilities: SystemCapabilitiesResponse | null,
): CleanerBackendOption[] {
  return options.filter((option) => isCleanerBackendRuntimeAvailable(option, capabilities));
}

export function mapCleanerBackendOptionToTermSelect(option: CleanerBackendOption): TermSelectOption {
  return {
    value: option.id,
    label: option.display_name,
  };
}

// A "local" cleaner backend runs on-device and needs no API key (mlx/cuda),
// so it can start converting without extra credentials. The server declares
// backends api-first, so we cannot rely on declaration order to find one.
function isLocalCleanerBackendOption(option: CleanerBackendOption): boolean {
  return option.is_local || !option.requires_api_key;
}

// Prefer an available local backend (keeping server order among locals as the
// tie-break), falling back to the first available option only when no local
// backend is available.
export function preferredCleanerBackendId(availableOptions: CleanerBackendOption[]): string {
  const localOption = availableOptions.find(isLocalCleanerBackendOption);
  return (localOption ?? availableOptions[0])?.id ?? "";
}

interface InitialCleanerBackendSelection {
  backendId: string;
  restoredPersistedBackend: boolean;
}

// Pure selection policy: a persisted, still-available choice always wins;
// otherwise default to the preferred (local-first) available backend.
export function resolveInitialCleanerBackendId(
  availableOptions: CleanerBackendOption[],
  persistedBackendId: string,
): InitialCleanerBackendSelection {
  const normalizedPersisted = String(persistedBackendId ?? "").trim();
  if (normalizedPersisted && availableOptions.some((option) => option.id === normalizedPersisted)) {
    return { backendId: normalizedPersisted, restoredPersistedBackend: true };
  }
  return { backendId: preferredCleanerBackendId(availableOptions), restoredPersistedBackend: false };
}

export function defaultCleanerCudaDevice(option: CleanerBackendOption | null): string {
  return String(option?.default_device ?? "").trim() || (option?.id === "cuda" ? "cuda" : "");
}

export function defaultCleanerCudaQuantization(option: CleanerBackendOption | null): string {
  const quantizations = option?.supported_quantizations ?? [];
  return quantizations[0] ?? "";
}

interface CleanerRuntimePayloadInput {
  selectedBackend: CleanerBackendOption | null;
  cleanerBackend: string;
  cleanerModel: string;
  cleanerApiKey: string;
  cleanerCudaDevice: string;
  cleanerCudaQuantization: string;
}

interface CleanerRuntimePayloadFields {
  cleaner_backend?: string | null;
  cleaner_model?: string | null;
  cleaner_api_model?: string | null;
  cleaner_api_key?: string | null;
  cleaner_cuda_device?: string | null;
  cleaner_cuda_quantization?: string | null;
}

export function cleanerBackendUsesCudaFields(option: CleanerBackendOption | null): boolean {
  if (!option) {
    return false;
  }

  return Boolean(option.default_device || (option.supported_quantizations?.length ?? 0) > 0);
}

export function buildCleanerRuntimePayloadFields(
  input: CleanerRuntimePayloadInput,
): CleanerRuntimePayloadFields {
  const backend = input.selectedBackend;
  const backendId = String(input.cleanerBackend ?? "").trim();
  const normalizedModel = String(input.cleanerModel ?? "").trim();
  const normalizedApiKey = String(input.cleanerApiKey ?? "").trim();
  const fields: CleanerRuntimePayloadFields = {};

  if (backendId) {
    fields.cleaner_backend = backendId;
  }

  if (backend?.is_local) {
    fields.cleaner_model = normalizedModel || null;
  }

  if (backend?.requires_api_key) {
    fields.cleaner_api_model = normalizedModel || null;
    fields.cleaner_api_key = normalizedApiKey || null;
  }

  if (cleanerBackendUsesCudaFields(backend)) {
    const device = String(input.cleanerCudaDevice ?? "").trim();
    const quantization = String(input.cleanerCudaQuantization ?? "").trim();
    fields.cleaner_cuda_device = device || null;
    fields.cleaner_cuda_quantization = quantization || null;
  }

  return fields;
}

function localCleanerRuntimeDisplayName(displayName: string, localPrefix: "Local" | "local"): string {
  const normalizedDisplayName = displayName.trim();
  if (/^local\b/i.test(normalizedDisplayName)) {
    return normalizedDisplayName;
  }

  return `${localPrefix} ${normalizedDisplayName}`;
}

export function deriveCleanerRuntimeStatus(
  capabilities: SystemCapabilitiesResponse | null,
  selectedBackend: CleanerBackendOption | null,
  hasApiKey = false,
): {
  message: string;
  isError: boolean;
} {
  if (!selectedBackend) {
    return { message: "No cleaner backend options available.", isError: true };
  }

  const displayName = selectedBackend.display_name;
  if (selectedBackend.id === "basic") {
    return { message: `${displayName} ready.`, isError: false };
  }

  if (!selectedBackend.is_local) {
    if (selectedBackend.requires_api_key && !hasApiKey) {
      return {
        message: `${displayName} selected. Paste an API key to load models.`,
        isError: false,
      };
    }
    return { message: `${displayName} cleaner ready.`, isError: false };
  }

  if (!capabilities) {
    return { message: "Runtime capability probe unavailable.", isError: true };
  }

  const availability = capabilities.cleaner_runtime_availability[selectedBackend.id];
  if (availability === true) {
    return {
      message: `${localCleanerRuntimeDisplayName(displayName, "Local")} runtime available.`,
      isError: false,
    };
  }

  if (availability === false) {
    const cleanerError = capabilities.runtime_probe?.cleaner?.error;
    const detail =
      typeof cleanerError === "string" && cleanerError.trim() ? ` (${cleanerError.trim()})` : "";
    return {
      message: `${localCleanerRuntimeDisplayName(
        displayName,
        "Local",
      )} cleaner unavailable${detail}. Choose another backend if needed.`,
      isError: true,
    };
  }

  return {
    message: `Checking ${localCleanerRuntimeDisplayName(displayName, "local")} cleaner runtime…`,
    isError: false,
  };
}
