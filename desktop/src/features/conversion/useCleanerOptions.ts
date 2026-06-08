import { useCallback, useEffect, useMemo, useState, type MutableRefObject } from "react";

import type { ApiClient } from "../../api/client";
import type { ModelOptionsResponse, SystemCapabilitiesResponse, TermSelectOption } from "../../types/api";

import type { PersistedSettings } from "./conversionOptionsShared";

const CLEANER_BACKEND_OPTIONS: TermSelectOption[] = [
  { value: "mlx", label: "local (mlx)" },
  { value: "api", label: "api" },
];

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
}

function deriveCleanerRuntimeStatus(capabilities: SystemCapabilitiesResponse | null): {
  message: string;
  isError: boolean;
} {
  if (!capabilities) {
    return { message: "Runtime capability probe unavailable.", isError: true };
  }

  const runtimeProbe = capabilities.runtime_probe;
  const state = String(runtimeProbe?.state ?? "").toLowerCase();
  const refreshInProgress = Boolean(runtimeProbe?.refresh_in_progress);

  if (!runtimeProbe || state !== "fresh" || refreshInProgress) {
    return { message: "Checking local MLX cleaner runtime...", isError: false };
  }

  if (capabilities.mlx_cleaner_runtime_available) {
    return { message: "Local MLX cleaner ready.", isError: false };
  }

  const cleanerError = runtimeProbe?.cleaner?.error;
  const detail = typeof cleanerError === "string" && cleanerError.trim() ? ` (${cleanerError.trim()})` : "";
  return {
    message: `Local MLX cleaner unavailable${detail}. Choose API in config if needed.`,
    isError: true,
  };
}

export function useCleanerOptions({
  apiClient,
  modelOptions,
  systemCapabilities,
  persistedSettingsRef,
}: UseCleanerOptionsArgs): UseCleanerOptionsResult {
  const [cleanerBackend, setCleanerBackendState] = useState("mlx");
  const [cleanerBackendConfigured, setCleanerBackendConfigured] = useState(false);
  const [cleanerModel, setCleanerModelState] = useState("");
  const [cleanerApiKey, setCleanerApiKeyState] = useState("");
  const [fetchedOpenAiModelOptions, setFetchedOpenAiModelOptions] = useState<TermSelectOption[]>([]);
  const [openAiModelFetchStatus, setOpenAiModelFetchStatus] = useState("Paste an OpenAI API key to fetch available models.");
  const [openAiModelFetchLoading, setOpenAiModelFetchLoading] = useState(false);
  const [openAiModelFetchError, setOpenAiModelFetchError] = useState(false);

  const [cleanerRuntimeStatus, setCleanerRuntimeStatus] = useState("Checking local MLX cleaner runtime...");
  const [cleanerRuntimeError, setCleanerRuntimeError] = useState(false);

  const cleanerModelOptions = useMemo(() => {
    if (cleanerBackend === "api" && fetchedOpenAiModelOptions.length > 0) {
      return fetchedOpenAiModelOptions;
    }
    const models = cleanerBackend === "api" ? modelOptions?.api_cleaner_models : modelOptions?.cleaner_models;
    return (models ?? []).map((model) => ({ value: model, label: model }));
  }, [cleanerBackend, fetchedOpenAiModelOptions, modelOptions]);

  useEffect(() => {
    if (!apiClient) {
      setCleanerBackendState("mlx");
      setCleanerBackendConfigured(false);
      setCleanerModelState("");
      setCleanerApiKeyState("");
      setFetchedOpenAiModelOptions([]);
      setOpenAiModelFetchStatus("Paste an OpenAI API key to fetch available models.");
      setOpenAiModelFetchLoading(false);
      setOpenAiModelFetchError(false);
      setCleanerRuntimeStatus("Checking local MLX cleaner runtime...");
      setCleanerRuntimeError(false);
      return;
    }

    if (!modelOptions) {
      return;
    }

    const persisted = persistedSettingsRef.current;
    const hasConfiguredCleanerBackend = Boolean(persisted.cleaner_backend_configured);
    setCleanerBackendConfigured(hasConfiguredCleanerBackend);
    const fallbackBackend = "mlx";
    const nextBackend =
      [hasConfiguredCleanerBackend ? persisted.cleaner_backend : "", fallbackBackend]
        .map((value) => String(value ?? "").trim())
        .find((candidate) => CLEANER_BACKEND_OPTIONS.some((option) => option.value === candidate)) ?? "mlx";

    setCleanerBackendState(nextBackend);
  }, [apiClient, modelOptions, persistedSettingsRef]);

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
    if (!systemCapabilities) {
      if (!apiClient) {
        return;
      }
      setCleanerRuntimeStatus("Runtime capability probe unavailable.");
      setCleanerRuntimeError(true);
      return;
    }

    const runtimeStatus = deriveCleanerRuntimeStatus(systemCapabilities);
    setCleanerRuntimeStatus(runtimeStatus.message);
    setCleanerRuntimeError(runtimeStatus.isError);
  }, [apiClient, systemCapabilities]);

  const setCleanerBackend = useCallback((value: string) => {
    setCleanerBackendConfigured(true);
    setCleanerBackendState(value);
  }, []);

  const setCleanerModel = useCallback((value: string) => {
    setCleanerBackendConfigured(true);
    setCleanerModelState(value);
  }, []);

  const setCleanerApiKey = useCallback((value: string) => {
    setCleanerApiKeyState(value);
    setOpenAiModelFetchError(false);
    if (!value.trim()) {
      setOpenAiModelFetchStatus("Paste an OpenAI API key to fetch available models.");
    }
  }, []);

  const fetchOpenAiModelIds = useCallback(async () => {
    if (!apiClient) {
      setOpenAiModelFetchError(true);
      setOpenAiModelFetchStatus("Backend is not connected.");
      return;
    }

    const normalizedKey = cleanerApiKey.trim();
    if (!normalizedKey) {
      setOpenAiModelFetchError(true);
      setOpenAiModelFetchStatus("Paste an OpenAI API key before fetching models.");
      return;
    }

    setCleanerBackendConfigured(true);
    setOpenAiModelFetchLoading(true);
    setOpenAiModelFetchError(false);
    setOpenAiModelFetchStatus("Fetching OpenAI models...");

    try {
      const modelIds = await apiClient.fetchOpenAiModelIds(normalizedKey);
      const options = modelIds.map((model) => ({ value: model, label: model }));
      setFetchedOpenAiModelOptions(options);
      setCleanerBackendState("api");
      setOpenAiModelFetchStatus(
        options.length > 0 ? `Loaded ${options.length} OpenAI models.` : "No OpenAI models returned for this key.",
      );
      setOpenAiModelFetchError(options.length === 0);
    } catch (fetchError: unknown) {
      setOpenAiModelFetchError(true);
      setOpenAiModelFetchStatus(fetchError instanceof Error ? fetchError.message : "OpenAI model fetch failed.");
    } finally {
      setOpenAiModelFetchLoading(false);
    }
  }, [apiClient, cleanerApiKey]);

  return {
    cleanerBackend,
    setCleanerBackend,
    cleanerBackendOptions: CLEANER_BACKEND_OPTIONS,
    cleanerBackendConfigured,
    cleanerModel,
    setCleanerModel,
    cleanerModelOptions,
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
