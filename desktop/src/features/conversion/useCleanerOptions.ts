import { useCallback, useEffect, useMemo, useRef, useState, type MutableRefObject } from "react";

import type { ApiClient } from "../../api/client";
import type { CleanerBackendOption, ModelOptionsResponse, SystemCapabilitiesResponse, TermSelectOption } from "../../types/api";

import {
  cleanerBackendRequiresApiKey,
  defaultCleanerCudaDevice,
  defaultCleanerCudaQuantization,
  deriveCleanerRuntimeStatus,
  filterAvailableCleanerBackendOptions,
  mapCleanerBackendOptionToTermSelect,
  preferredCleanerBackendId,
  resolveCleanerBackendOptions,
  resolveInitialCleanerBackendId,
  selectedCleanerBackendOption,
} from "./cleanerBackendHelpers";
import type { PersistedSettings } from "./conversionOptionsShared";
import { isAbortLikeError, useLatestRequest } from "./useLatestRequest";

export interface UseCleanerOptionsArgs {
  apiClient: ApiClient | null;
  modelOptions: ModelOptionsResponse | null;
  systemCapabilities: SystemCapabilitiesResponse | null;
  persistedSettingsRef: MutableRefObject<PersistedSettings>;
}

export interface UseCleanerOptionsResult {
  cleanerOptionsHydrated: boolean;
  cleanerBackend: string;
  setCleanerBackend: (value: string) => void;
  cleanerBackendOptions: TermSelectOption[];
  cleanerBackendConfigured: boolean;
  selectedCleanerBackend: CleanerBackendOption | null;
  cleanerModel: string;
  setCleanerModel: (value: string) => void;
  cleanerModelOptions: TermSelectOption[];
  cleanerCudaDevice: string;
  setCleanerCudaDevice: (value: string) => void;
  cleanerCudaQuantization: string;
  setCleanerCudaQuantization: (value: string) => void;
  cleanerApiKey: string;
  setCleanerApiKey: (value: string) => void;
  fetchOpenAiModelIds: () => Promise<void>;
  openAiModelFetchStatus: string;
  openAiModelFetchLoading: boolean;
  openAiModelFetchError: boolean;
  cleanerRuntimeStatus: string;
  cleanerRuntimeError: boolean;
}

// Provider failures arrive as our proxy's message wrapping the provider's raw
// JSON error body; surface just the human sentence inside it when present.
export function compactProviderError(message: string): string {
  const match = message.match(/"message":\s*"((?:[^"\\]|\\.)+)"/);
  if (!match) {
    return message;
  }
  try {
    return JSON.parse(`"${match[1]}"`);
  } catch {
    return match[1];
  }
}

function resolveInitialBackendSelection(
  modelOptions: ModelOptionsResponse | null,
  systemCapabilities: SystemCapabilitiesResponse | null,
  persisted: PersistedSettings,
) {
  const availableOptions = filterAvailableCleanerBackendOptions(
    resolveCleanerBackendOptions(modelOptions),
    systemCapabilities,
  );

  const hasConfiguredCleanerBackend = Boolean(persisted.cleaner_backend_configured);
  const persistedBackend = hasConfiguredCleanerBackend
    ? String(
        persisted.preferred_cleaner_backend ?? persisted.cleaner_backend ?? "",
      ).trim()
    : "";

  return resolveInitialCleanerBackendId(availableOptions, persistedBackend);
}

function resolveInitialCudaDevice(
  backendOption: CleanerBackendOption | null,
  persisted: PersistedSettings,
): string {
  const persistedDevice = String(persisted.cleaner_cuda_device ?? "").trim();
  if (persistedDevice) {
    return persistedDevice;
  }
  return defaultCleanerCudaDevice(backendOption);
}

function resolveInitialCudaQuantization(
  backendOption: CleanerBackendOption | null,
  persisted: PersistedSettings,
): string {
  const persistedQuantization = String(persisted.cleaner_cuda_quantization ?? "").trim();
  const supportedQuantizations = backendOption?.supported_quantizations ?? [];
  if (persistedQuantization && supportedQuantizations.includes(persistedQuantization)) {
    return persistedQuantization;
  }
  return defaultCleanerCudaQuantization(backendOption);
}

export function useCleanerOptions({
  apiClient,
  modelOptions,
  systemCapabilities,
  persistedSettingsRef,
}: UseCleanerOptionsArgs): UseCleanerOptionsResult {
  const cleanerOptionsHydratedRef = useRef(false);
  const [cleanerOptionsHydrated, setCleanerOptionsHydrated] = useState(false);
  const preferredCleanerBackendRef = useRef("");
  const [cleanerBackend, setCleanerBackendState] = useState("");
  const [cleanerBackendConfigured, setCleanerBackendConfigured] = useState(false);
  const [cleanerModel, setCleanerModelState] = useState("");
  const [cleanerCudaDevice, setCleanerCudaDeviceState] = useState("");
  const [cleanerCudaQuantization, setCleanerCudaQuantizationState] = useState("");
  const [cleanerApiKey, setCleanerApiKeyState] = useState("");
  const [fetchedOpenAiModelOptions, setFetchedOpenAiModelOptions] = useState<TermSelectOption[]>([]);
  const [openAiModelFetchStatus, setOpenAiModelFetchStatus] = useState("Paste an API key to fetch available models.");
  const [openAiModelFetchLoading, setOpenAiModelFetchLoading] = useState(false);
  const [openAiModelFetchError, setOpenAiModelFetchError] = useState(false);

  const [cleanerRuntimeStatus, setCleanerRuntimeStatus] = useState("Checking cleaner runtime...");
  const [cleanerRuntimeError, setCleanerRuntimeError] = useState(false);
  const beginOpenAiModelFetch = useLatestRequest();
  const lastAutoFetchedKeyRef = useRef("");
  const openAiModelFetchLoadingRef = useRef(false);

  const availableCleanerBackendOptions = useMemo(
    () => filterAvailableCleanerBackendOptions(resolveCleanerBackendOptions(modelOptions), systemCapabilities),
    [modelOptions, systemCapabilities],
  );

  const cleanerBackendOptions = useMemo(
    () => availableCleanerBackendOptions.map(mapCleanerBackendOptionToTermSelect),
    [availableCleanerBackendOptions],
  );

  const selectedCleanerBackend = useMemo(
    () => selectedCleanerBackendOption(modelOptions, cleanerBackend),
    [cleanerBackend, modelOptions],
  );

  const cleanerModelOptions = useMemo(() => {
    // API-key backends list models live from the provider once a key is
    // pasted; a canned fallback list would advertise models the key may not
    // actually have access to.
    if (cleanerBackendRequiresApiKey(modelOptions, cleanerBackend)) {
      return fetchedOpenAiModelOptions;
    }

    const models = selectedCleanerBackend?.models ?? [];
    return models.map((model) => ({ value: model, label: model }));
  }, [cleanerBackend, fetchedOpenAiModelOptions, modelOptions, selectedCleanerBackend]);

  const selectCleanerBackend = useCallback(
    (
      value: string,
      cudaDefaults?: {
        device?: string;
        quantization?: string;
      },
    ) => {
      const backendOption = selectedCleanerBackendOption(modelOptions, value);

      setCleanerBackendState(value);
      setCleanerCudaDeviceState(cudaDefaults?.device ?? defaultCleanerCudaDevice(backendOption));
      setCleanerCudaQuantizationState(cudaDefaults?.quantization ?? defaultCleanerCudaQuantization(backendOption));
    },
    [modelOptions],
  );

  useEffect(() => {
    if (!apiClient) {
      cleanerOptionsHydratedRef.current = false;
      setCleanerOptionsHydrated(false);
      setCleanerBackendState("");
      setCleanerBackendConfigured(false);
      setCleanerModelState("");
      setCleanerCudaDeviceState("");
      setCleanerCudaQuantizationState("");
      setCleanerApiKeyState("");
      setFetchedOpenAiModelOptions([]);
      setOpenAiModelFetchStatus("Paste an API key to fetch available models.");
      setOpenAiModelFetchLoading(false);
      setOpenAiModelFetchError(false);
      setCleanerRuntimeStatus("Checking cleaner runtime...");
      setCleanerRuntimeError(false);
      lastAutoFetchedKeyRef.current = "";
      openAiModelFetchLoadingRef.current = false;
      beginOpenAiModelFetch();
      return;
    }

    if (!modelOptions || cleanerOptionsHydratedRef.current) {
      return;
    }

    const backendOptions = resolveCleanerBackendOptions(modelOptions);
    if (backendOptions.length === 0) {
      return;
    }

    const persisted = persistedSettingsRef.current;
    const hasConfiguredCleanerBackend = Boolean(persisted.cleaner_backend_configured);
    const legacyPersistedBackend = String(persisted.cleaner_backend ?? "").trim();
    const legacyPersistedModel = String(persisted.cleaner_model ?? "").trim();
    const persistedPreference = hasConfiguredCleanerBackend
      ? String(persisted.preferred_cleaner_backend ?? legacyPersistedBackend).trim()
      : "";
    const persistedModelPreference = hasConfiguredCleanerBackend
      ? String(persisted.preferred_cleaner_model ?? legacyPersistedModel).trim()
      : "";
    if (
      hasConfiguredCleanerBackend &&
      persistedPreference &&
      !String(persisted.preferred_cleaner_backend ?? "").trim()
    ) {
      // Migrate v1 settings in memory before the parent persistence effect
      // serializes the effective fallback backend over the legacy choice.
      persistedSettingsRef.current = {
        ...persisted,
        preferred_cleaner_backend: persistedPreference,
      };
    }
    if (
      hasConfiguredCleanerBackend &&
      persistedModelPreference &&
      !String(persisted.preferred_cleaner_model ?? "").trim()
    ) {
      persistedSettingsRef.current = {
        ...persistedSettingsRef.current,
        preferred_cleaner_model: persistedModelPreference,
      };
    }
    preferredCleanerBackendRef.current = persistedPreference;
    setCleanerBackendConfigured(hasConfiguredCleanerBackend);

    const initialSelection = resolveInitialBackendSelection(modelOptions, systemCapabilities, persisted);
    const backendOption = selectedCleanerBackendOption(modelOptions, initialSelection.backendId);
    const persistedCudaDefaults = initialSelection.restoredPersistedBackend
      ? {
          device: resolveInitialCudaDevice(backendOption, persisted),
          quantization: resolveInitialCudaQuantization(backendOption, persisted),
        }
      : undefined;
    selectCleanerBackend(initialSelection.backendId, persistedCudaDefaults);
    cleanerOptionsHydratedRef.current = true;
    setCleanerOptionsHydrated(true);
  }, [apiClient, beginOpenAiModelFetch, modelOptions, persistedSettingsRef, selectCleanerBackend, systemCapabilities]);

  useEffect(() => {
    if (!apiClient || !modelOptions || !cleanerBackend) {
      return;
    }

    const isAvailable = availableCleanerBackendOptions.some((option) => option.id === cleanerBackend);
    const preferredBackend = preferredCleanerBackendRef.current;
    if (preferredBackend && availableCleanerBackendOptions.some((option) => option.id === preferredBackend)) {
      if (preferredBackend !== cleanerBackend) {
        const preferredOption = selectedCleanerBackendOption(modelOptions, preferredBackend);
        selectCleanerBackend(preferredBackend, {
          device: resolveInitialCudaDevice(preferredOption, persistedSettingsRef.current),
          quantization: resolveInitialCudaQuantization(preferredOption, persistedSettingsRef.current),
        });
      }
      return;
    }
    // When the user has explicitly configured a backend we only intervene if it
    // drops out of availability. When the selection is still an auto-default we
    // keep tracking the preferred (local-first) option as the runtime probe
    // finishes and local backends flip from unavailable to available.
    if (isAvailable && cleanerBackendConfigured) {
      return;
    }

    const nextBackend = cleanerBackendConfigured
      ? availableCleanerBackendOptions[0]?.id ?? ""
      : preferredCleanerBackendId(availableCleanerBackendOptions);
    if (nextBackend !== cleanerBackend) {
      selectCleanerBackend(nextBackend);
    }
  }, [
    apiClient,
    availableCleanerBackendOptions,
    cleanerBackend,
    cleanerBackendConfigured,
    modelOptions,
    selectCleanerBackend,
  ]);

  useEffect(() => {
    setCleanerModelState((previous) => {
      if (cleanerModelOptions.some((option) => option.value === previous)) {
        return previous;
      }

      const persisted = String(
        persistedSettingsRef.current.preferred_cleaner_model ??
          persistedSettingsRef.current.cleaner_model ??
          "",
      ).trim();
      if (persisted && cleanerModelOptions.some((option) => option.value === persisted)) {
        return persisted;
      }

      return cleanerModelOptions[0]?.value ?? "";
    });
  }, [cleanerModelOptions, persistedSettingsRef]);

  useEffect(() => {
    if (!selectedCleanerBackend) {
      setCleanerRuntimeStatus("No cleaner backend options available.");
      setCleanerRuntimeError(true);
      return;
    }

    const runtimeStatus = deriveCleanerRuntimeStatus(
      systemCapabilities,
      selectedCleanerBackend,
      Boolean(cleanerApiKey.trim()),
    );
    setCleanerRuntimeStatus(runtimeStatus.message);
    setCleanerRuntimeError(runtimeStatus.isError);
  }, [cleanerApiKey, selectedCleanerBackend, systemCapabilities]);

  const setCleanerBackend = useCallback(
    (value: string) => {
      const normalizedValue = String(value ?? "").trim();
      preferredCleanerBackendRef.current = normalizedValue;
      persistedSettingsRef.current = {
        ...persistedSettingsRef.current,
        preferred_cleaner_backend: normalizedValue || undefined,
      };
      setCleanerBackendConfigured(true);
      selectCleanerBackend(normalizedValue);
    },
    [persistedSettingsRef, selectCleanerBackend],
  );

  const setCleanerModel = useCallback((value: string) => {
    const normalizedValue = String(value ?? "").trim();
    persistedSettingsRef.current = {
      ...persistedSettingsRef.current,
      preferred_cleaner_model: normalizedValue || undefined,
    };
    setCleanerBackendConfigured(true);
    setCleanerModelState(value);
  }, [persistedSettingsRef]);

  const setCleanerCudaDevice = useCallback((value: string) => {
    setCleanerBackendConfigured(true);
    setCleanerCudaDeviceState(value);
  }, []);

  const setCleanerCudaQuantization = useCallback((value: string) => {
    setCleanerBackendConfigured(true);
    setCleanerCudaQuantizationState(value);
  }, []);

  const setCleanerApiKey = useCallback((value: string) => {
    const normalizedValue = value.trim();
    if (normalizedValue !== cleanerApiKey.trim()) {
      // A key identifies the provider model list. Invalidate any request and
      // selection tied to the previous key before accepting the new value.
      openAiModelFetchLoadingRef.current = false;
      beginOpenAiModelFetch();
      setFetchedOpenAiModelOptions([]);
      setCleanerModelState("");
      setOpenAiModelFetchLoading(false);
      lastAutoFetchedKeyRef.current = "";
      setOpenAiModelFetchStatus("Paste an API key to fetch available models.");
    }
    setCleanerApiKeyState(value);
    setOpenAiModelFetchError(false);
    if (!normalizedValue) {
      setOpenAiModelFetchStatus("Paste an API key to fetch available models.");
    }
  }, [beginOpenAiModelFetch, cleanerApiKey]);

  const fetchOpenAiModelIds = useCallback(async () => {
    if (!apiClient) {
      setOpenAiModelFetchError(true);
      setOpenAiModelFetchStatus("Backend is not connected.");
      return;
    }

    if (!selectedCleanerBackend?.requires_api_key) {
      setOpenAiModelFetchError(true);
      setOpenAiModelFetchStatus("Select a cleaner backend that uses an API key before fetching models.");
      return;
    }

    const normalizedKey = cleanerApiKey.trim();
    if (!normalizedKey) {
      setOpenAiModelFetchError(true);
      setOpenAiModelFetchStatus("Paste an API key before fetching models.");
      return;
    }

    if (openAiModelFetchLoadingRef.current) {
      return;
    }

    setCleanerBackendConfigured(true);
    lastAutoFetchedKeyRef.current = normalizedKey;
    openAiModelFetchLoadingRef.current = true;
    setOpenAiModelFetchLoading(true);
    setOpenAiModelFetchError(false);
    setOpenAiModelFetchStatus(`Fetching ${selectedCleanerBackend.display_name} models...`);
    const isLatest = beginOpenAiModelFetch();

    try {
      const modelIds = await apiClient.fetchOpenAiModelIds(normalizedKey);
      if (!isLatest()) {
        return;
      }
      const options = modelIds.map((model) => ({ value: model, label: model }));
      setFetchedOpenAiModelOptions(options);
      setOpenAiModelFetchStatus(
        options.length > 0
          ? `Loaded ${options.length} ${selectedCleanerBackend.display_name} models.`
          : `No ${selectedCleanerBackend.display_name} models returned for this key.`,
      );
      setOpenAiModelFetchError(options.length === 0);
    } catch (fetchError: unknown) {
      if (
        isLatest() &&
        !isAbortLikeError(fetchError)
      ) {
        setOpenAiModelFetchError(true);
        setOpenAiModelFetchStatus(
          fetchError instanceof Error
            ? compactProviderError(fetchError.message)
            : "Cleaner model fetch failed.",
        );
      }
    } finally {
      if (isLatest()) {
        openAiModelFetchLoadingRef.current = false;
        setOpenAiModelFetchLoading(false);
      }
    }
  }, [apiClient, beginOpenAiModelFetch, cleanerApiKey, selectedCleanerBackend]);

  // Fetch provider models automatically once a pasted key settles, so the
  // model list fills without a manual "Fetch models" click. The length gate
  // skips half-typed keys and the ref stops re-fetching an unchanged (e.g.
  // already-rejected) key; the manual button stays as the retry path.
  useEffect(() => {
    const normalizedKey = cleanerApiKey.trim();
    if (!apiClient || !selectedCleanerBackend?.requires_api_key) {
      return;
    }
    if (normalizedKey.length < 20 || lastAutoFetchedKeyRef.current === normalizedKey) {
      return;
    }
    const timer = setTimeout(() => {
      if (openAiModelFetchLoadingRef.current) {
        return;
      }
      lastAutoFetchedKeyRef.current = normalizedKey;
      void fetchOpenAiModelIds();
    }, 600);
    return () => clearTimeout(timer);
  }, [apiClient, cleanerApiKey, fetchOpenAiModelIds, selectedCleanerBackend]);

  return {
    cleanerOptionsHydrated,
    cleanerBackend,
    setCleanerBackend,
    cleanerBackendOptions,
    cleanerBackendConfigured,
    selectedCleanerBackend,
    cleanerModel,
    setCleanerModel,
    cleanerModelOptions,
    cleanerCudaDevice,
    setCleanerCudaDevice,
    cleanerCudaQuantization,
    setCleanerCudaQuantization,
    cleanerApiKey,
    setCleanerApiKey,
    fetchOpenAiModelIds,
    openAiModelFetchStatus,
    openAiModelFetchLoading,
    openAiModelFetchError,
    cleanerRuntimeStatus,
    cleanerRuntimeError,
  };
}
