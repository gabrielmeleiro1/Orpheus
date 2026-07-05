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
  resolveCleanerBackendOptions,
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

interface InitialBackendSelection {
  backendId: string;
  restoredPersistedBackend: boolean;
}

function resolveInitialBackendSelection(
  modelOptions: ModelOptionsResponse | null,
  systemCapabilities: SystemCapabilitiesResponse | null,
  persisted: PersistedSettings,
): InitialBackendSelection {
  const availableOptions = filterAvailableCleanerBackendOptions(
    resolveCleanerBackendOptions(modelOptions),
    systemCapabilities,
  );
  const fallbackBackend = availableOptions[0]?.id ?? "";

  const hasConfiguredCleanerBackend = Boolean(persisted.cleaner_backend_configured);
  const persistedBackend = hasConfiguredCleanerBackend ? String(persisted.cleaner_backend ?? "").trim() : "";

  if (persistedBackend && availableOptions.some((option) => option.id === persistedBackend)) {
    return { backendId: persistedBackend, restoredPersistedBackend: true };
  }

  return { backendId: fallbackBackend, restoredPersistedBackend: false };
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
    if (cleanerBackendRequiresApiKey(modelOptions, cleanerBackend) && fetchedOpenAiModelOptions.length > 0) {
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
  }, [apiClient, modelOptions, persistedSettingsRef, selectCleanerBackend, systemCapabilities]);

  useEffect(() => {
    if (!apiClient || !modelOptions || !cleanerBackend) {
      return;
    }

    const isAvailable = availableCleanerBackendOptions.some((option) => option.id === cleanerBackend);
    if (isAvailable) {
      return;
    }

    const nextBackend = availableCleanerBackendOptions[0]?.id ?? "";
    if (nextBackend !== cleanerBackend) {
      selectCleanerBackend(nextBackend);
    }
  }, [apiClient, availableCleanerBackendOptions, cleanerBackend, modelOptions, selectCleanerBackend]);

  useEffect(() => {
    setCleanerModelState((previous) => {
      if (cleanerModelOptions.some((option) => option.value === previous)) {
        return previous;
      }

      const persisted = String(persistedSettingsRef.current.cleaner_model ?? "").trim();
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

    const runtimeStatus = deriveCleanerRuntimeStatus(systemCapabilities, selectedCleanerBackend);
    setCleanerRuntimeStatus(runtimeStatus.message);
    setCleanerRuntimeError(runtimeStatus.isError);
  }, [selectedCleanerBackend, systemCapabilities]);

  const setCleanerBackend = useCallback(
    (value: string) => {
      setCleanerBackendConfigured(true);
      selectCleanerBackend(value);
    },
    [selectCleanerBackend],
  );

  const setCleanerModel = useCallback((value: string) => {
    setCleanerBackendConfigured(true);
    setCleanerModelState(value);
  }, []);

  const setCleanerCudaDevice = useCallback((value: string) => {
    setCleanerBackendConfigured(true);
    setCleanerCudaDeviceState(value);
  }, []);

  const setCleanerCudaQuantization = useCallback((value: string) => {
    setCleanerBackendConfigured(true);
    setCleanerCudaQuantizationState(value);
  }, []);

  const setCleanerApiKey = useCallback((value: string) => {
    setCleanerApiKeyState(value);
    setOpenAiModelFetchError(false);
    if (!value.trim()) {
      setOpenAiModelFetchStatus("Paste an API key to fetch available models.");
    }
  }, []);

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

    setCleanerBackendConfigured(true);
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
      if (isLatest() && !isAbortLikeError(fetchError)) {
        setOpenAiModelFetchError(true);
        setOpenAiModelFetchStatus(fetchError instanceof Error ? fetchError.message : "Cleaner model fetch failed.");
      }
    } finally {
      if (isLatest()) {
        setOpenAiModelFetchLoading(false);
      }
    }
  }, [apiClient, beginOpenAiModelFetch, cleanerApiKey, selectedCleanerBackend]);

  return {
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
