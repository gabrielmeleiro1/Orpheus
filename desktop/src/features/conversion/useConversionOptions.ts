import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ApiClient } from "../../api/client";
import type {
  ModelOptionsResponse,
  ParallelWorkerRecommendation,
  SystemCapabilitiesResponse,
  TermSelectOption,
  TtsEngineOption,
  TtsVoiceOption,
} from "../../types/api";

const CLEANER_BACKEND_OPTIONS: TermSelectOption[] = [
  { value: "mlx", label: "local (mlx)" },
  { value: "api", label: "api" },
];
const SETTINGS_KEY = "orpheus.desktop.settings.v1";
const VOICE_FILTER_DEBOUNCE_MS = 180;
const DEFAULT_PARALLEL_WORKER_MODE = "manual";
const DEFAULT_PARALLEL_WORKER_LIMIT = 4;

interface PersistedSettings {
  tts_engine?: string;
  voice?: string;
  speed?: number;
  tts_quantization?: string;
  cleaner_backend?: string;
  cleaner_model?: string;
  cleaner_backend_configured?: boolean;
  parallel_worker_mode?: "auto" | "manual";
  parallel_manual_max?: number;
}

interface UseConversionOptionsArgs {
  apiClient: ApiClient | null;
  conversionMode: string;
}

export interface UseConversionOptionsResult {
  loading: boolean;
  error: string;
  selectedEngine: string;
  setSelectedEngine: (value: string) => void;
  selectedVoice: string;
  setSelectedVoice: (value: string) => void;
  engineOptions: TermSelectOption[];
  filteredVoiceOptions: TermSelectOption[];
  voiceFilter: string;
  setVoiceFilter: (value: string) => void;
  speedInput: string;
  speed: number;
  setSpeed: (value: string) => void;
  selectedQuantization: string;
  setSelectedQuantization: (value: string) => void;
  quantizationOptions: TermSelectOption[];
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

function readPersistedSettings(): PersistedSettings {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(SETTINGS_KEY) ?? "{}");
    if (!parsed || typeof parsed !== "object") {
      return {};
    }
    return parsed as PersistedSettings;
  } catch {
    return {};
  }
}

function savePersistedSettings(settings: PersistedSettings): void {
  try {
    window.localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
  } catch {
    // Ignore storage failures.
  }
}

function mapEngineOption(engine: TtsEngineOption): TermSelectOption {
  const id = String(engine.id ?? "").trim();
  const displayName = String(engine.display_name ?? engine.name ?? "").trim();
  return {
    value: id,
    label: displayName ? `${id} - ${displayName}` : id,
  };
}

function mapVoiceOption(voice: TtsVoiceOption): TermSelectOption {
  const voiceId = String(voice.id ?? "").trim();
  const locale = String(voice.locale ?? voice.language ?? "").trim();
  const name = String(voice.name ?? "").trim();
  const styleTags = Array.isArray(voice.style_tags) ? voice.style_tags.join(" ") : "";

  return {
    value: voiceId,
    label: voiceId + (name ? ` - ${name}` : "") + (locale ? ` (${locale})` : ""),
    search: [voiceId, name, locale, styleTags].join(" ").toLowerCase(),
  };
}

function parseSpeed(value: string): number {
  const parsed = Number.parseFloat(value);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return 1;
  }
  return parsed;
}

function findEngine(models: ModelOptionsResponse | null, engineId: string): TtsEngineOption | null {
  if (!models || !engineId.trim()) {
    return null;
  }
  return models.tts_engines.find((engine) => engine.id === engineId) ?? null;
}

function buildQuantizationOptions(
  engine: TtsEngineOption | null,
  capabilities: SystemCapabilitiesResponse | null,
): TermSelectOption[] {
  const seen = new Set<string>();
  const values: string[] = [""];

  const addValue = (value: string) => {
    const normalized = value.trim();
    if (!normalized || seen.has(normalized)) {
      return;
    }
    seen.add(normalized);
    values.push(normalized);
  };

  const defaultQuantization = String(capabilities?.default_tts_quantization ?? "").trim();
  if (defaultQuantization) {
    addValue(defaultQuantization);
  }

  for (const value of engine?.supported_quantizations ?? []) {
    if (typeof value === "string") {
      addValue(value);
    }
  }

  if (values.length === 1) {
    values.push("bf16");
  }

  return values.map((value) => ({
    value,
    label: value || "default",
  }));
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

export function useConversionOptions({ apiClient, conversionMode }: UseConversionOptionsArgs): UseConversionOptionsResult {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const [modelOptions, setModelOptions] = useState<ModelOptionsResponse | null>(null);
  const [systemCapabilities, setSystemCapabilities] = useState<SystemCapabilitiesResponse | null>(null);
  const [allVoiceOptions, setAllVoiceOptions] = useState<TermSelectOption[]>([]);

  const [selectedEngine, setSelectedEngineState] = useState("");
  const [selectedVoice, setSelectedVoiceState] = useState("");
  const [voiceFilter, setVoiceFilter] = useState("");
  const [debouncedVoiceFilter, setDebouncedVoiceFilter] = useState("");

  const [speedInput, setSpeedInput] = useState("1.0");
  const [selectedQuantization, setSelectedQuantizationState] = useState("");

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
  const [parallelWorkerMode, setParallelWorkerModeState] = useState<"auto" | "manual">(DEFAULT_PARALLEL_WORKER_MODE);
  const [parallelManualMax, setParallelManualMaxState] = useState(String(DEFAULT_PARALLEL_WORKER_LIMIT));
  const [parallelRecommendation, setParallelRecommendation] = useState<ParallelWorkerRecommendation | null>(null);
  const [parallelWorkerStatus, setParallelWorkerStatus] = useState("Worker recommendation unavailable.");
  const [parallelWorkerError, setParallelWorkerError] = useState(false);
  const [parallelWarmupLoading, setParallelWarmupLoading] = useState(false);

  const persistedSettingsRef = useRef<PersistedSettings>(readPersistedSettings());
  const settingsHydratedRef = useRef(false);

  const engineOptions = useMemo(
    () => (modelOptions?.tts_engines ?? []).map((engine) => mapEngineOption(engine)).filter((option) => option.value),
    [modelOptions],
  );

  const quantizationOptions = useMemo(
    () => buildQuantizationOptions(findEngine(modelOptions, selectedEngine), systemCapabilities),
    [modelOptions, selectedEngine, systemCapabilities],
  );

  const cleanerModelOptions = useMemo(() => {
    if (cleanerBackend === "api" && fetchedOpenAiModelOptions.length > 0) {
      return fetchedOpenAiModelOptions;
    }
    const models = cleanerBackend === "api" ? modelOptions?.api_cleaner_models : modelOptions?.cleaner_models;
    return (models ?? []).map((model) => ({ value: model, label: model }));
  }, [cleanerBackend, fetchedOpenAiModelOptions, modelOptions]);

  const filteredVoiceOptions = useMemo(() => {
    const query = debouncedVoiceFilter.trim().toLowerCase();
    if (!query) {
      return allVoiceOptions;
    }
    return allVoiceOptions.filter((option) => (option.search ?? option.label.toLowerCase()).includes(query));
  }, [allVoiceOptions, debouncedVoiceFilter]);

  const speed = useMemo(() => parseSpeed(speedInput), [speedInput]);
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
    const timer = window.setTimeout(() => {
      setDebouncedVoiceFilter(voiceFilter);
    }, VOICE_FILTER_DEBOUNCE_MS);

    return () => {
      window.clearTimeout(timer);
    };
  }, [voiceFilter]);

  useEffect(() => {
    if (!apiClient) {
      settingsHydratedRef.current = false;
      setLoading(false);
      setError("");
      setModelOptions(null);
      setSystemCapabilities(null);
      setAllVoiceOptions([]);
      return;
    }

    let cancelled = false;

    settingsHydratedRef.current = false;
    setLoading(true);
    setError("");

    const load = async () => {
      try {
        const [models, capabilities] = await Promise.all([
          apiClient.getModelOptions(),
          apiClient.getSystemCapabilities(),
        ]);

        if (cancelled) {
          return;
        }

        setModelOptions(models);
        setSystemCapabilities(capabilities);
        setParallelRecommendation(capabilities.parallel_worker_recommendation ?? null);
        if (capabilities.parallel_worker_recommendation) {
          const recommendation = capabilities.parallel_worker_recommendation;
          setParallelManualMaxState(String(recommendation.requested_parallel_chapters));
          setParallelWorkerStatus(
            `Recommended ${recommendation.recommended_parallel_chapters}; max ${recommendation.max_safe_parallel_chapters}. ${recommendation.calculation}`,
          );
          setParallelWorkerError(!recommendation.memory_measurement_available);
        }

        const runtimeStatus = deriveCleanerRuntimeStatus(capabilities);
        setCleanerRuntimeStatus(runtimeStatus.message);
        setCleanerRuntimeError(runtimeStatus.isError);

        const persisted = persistedSettingsRef.current;

        const nextEngine =
          [persisted.tts_engine, String(capabilities.default_tts_engine ?? "")]
            .map((value) => String(value ?? "").trim())
            .find((candidate) => candidate && models.tts_engines.some((engine) => engine.id === candidate)) ??
          models.tts_engines[0]?.id ??
          "";

        setSelectedEngineState(nextEngine);

        const hasConfiguredCleanerBackend = Boolean(persisted.cleaner_backend_configured);
        setCleanerBackendConfigured(hasConfiguredCleanerBackend);
        const fallbackBackend = "mlx";
        const nextBackend =
          [hasConfiguredCleanerBackend ? persisted.cleaner_backend : "", fallbackBackend]
            .map((value) => String(value ?? "").trim())
            .find((candidate) => CLEANER_BACKEND_OPTIONS.some((option) => option.value === candidate)) ??
          "mlx";

        setCleanerBackendState(nextBackend);

        const savedSpeed = Number.isFinite(persisted.speed) && (persisted.speed ?? 0) > 0 ? String(persisted.speed) : "1.0";
        setSpeedInput(savedSpeed);
        if (persisted.parallel_worker_mode === "auto") {
          setParallelWorkerModeState("auto");
        } else {
          setParallelWorkerModeState(DEFAULT_PARALLEL_WORKER_MODE);
        }
        if (Number.isFinite(persisted.parallel_manual_max) && (persisted.parallel_manual_max ?? 0) > 0) {
          setParallelManualMaxState(String(persisted.parallel_manual_max));
        }
        settingsHydratedRef.current = true;
      } catch (loadError: unknown) {
        if (cancelled) {
          return;
        }

        setModelOptions(null);
        setSystemCapabilities(null);
        setAllVoiceOptions([]);
        setError(loadError instanceof Error ? loadError.message : "failed to load conversion options");
        setCleanerRuntimeStatus("Runtime capability probe unavailable.");
        setCleanerRuntimeError(true);
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    };

    void load();

    return () => {
      cancelled = true;
    };
  }, [apiClient]);

  useEffect(() => {
    if (!apiClient || !selectedEngine.trim()) {
      setAllVoiceOptions([]);
      setSelectedVoiceState("");
      return;
    }

    let cancelled = false;

    const loadVoices = async () => {
      try {
        const response = await apiClient.getVoiceOptions(selectedEngine);
        if (cancelled) {
          return;
        }

        const mapped = response.voices.map((voice) => mapVoiceOption(voice));
        setAllVoiceOptions(mapped);

        const preferred = persistedSettingsRef.current.voice;
        const nextVoice =
          [selectedVoice, preferred]
            .map((value) => String(value ?? "").trim())
            .find((candidate) => candidate && mapped.some((voice) => voice.value === candidate)) ??
          mapped[0]?.value ??
          "";

        setSelectedVoiceState(nextVoice);
      } catch {
        if (cancelled) {
          return;
        }

        const fallbackVoices = findEngine(modelOptions, selectedEngine)?.voices ?? [];
        const mapped = fallbackVoices.map((voice) => mapVoiceOption(voice));
        setAllVoiceOptions(mapped);

        const preferred = persistedSettingsRef.current.voice;
        const nextVoice =
          [selectedVoice, preferred]
            .map((value) => String(value ?? "").trim())
            .find((candidate) => candidate && mapped.some((voice) => voice.value === candidate)) ??
          mapped[0]?.value ??
          "";
        setSelectedVoiceState(nextVoice);
      }
    };

    void loadVoices();

    return () => {
      cancelled = true;
    };
  }, [apiClient, modelOptions, selectedEngine]);

  useEffect(() => {
    setSelectedQuantizationState((previous) => {
      if (quantizationOptions.some((option) => option.value === previous)) {
        return previous;
      }

      const persisted = String(persistedSettingsRef.current.tts_quantization ?? "").trim();
      if (persisted && quantizationOptions.some((option) => option.value === persisted)) {
        return persisted;
      }

      const capabilityDefault = String(systemCapabilities?.default_tts_quantization ?? "").trim();
      if (capabilityDefault && quantizationOptions.some((option) => option.value === capabilityDefault)) {
        return capabilityDefault;
      }

      return quantizationOptions[0]?.value ?? "";
    });
  }, [quantizationOptions, systemCapabilities]);

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
  }, [cleanerModelOptions]);

  useEffect(() => {
    if (!settingsHydratedRef.current) {
      return;
    }

    const nextSettings: PersistedSettings = {
      tts_engine: selectedEngine || undefined,
      voice: selectedVoice || undefined,
      speed,
      tts_quantization: selectedQuantization || undefined,
      cleaner_backend: cleanerBackend || undefined,
      cleaner_model: cleanerModel || undefined,
      cleaner_backend_configured: cleanerBackendConfigured,
      parallel_worker_mode: parallelWorkerMode,
      parallel_manual_max: selectedParallelWorkerLimit,
    };

    persistedSettingsRef.current = nextSettings;
    savePersistedSettings(nextSettings);
  }, [
    cleanerBackend,
    cleanerBackendConfigured,
    cleanerModel,
    parallelWorkerMode,
    selectedEngine,
    selectedParallelWorkerLimit,
    selectedQuantization,
    selectedVoice,
    speed,
  ]);

  useEffect(() => {
    if (!systemCapabilities) {
      return;
    }

    const runtimeStatus = deriveCleanerRuntimeStatus(systemCapabilities);
    setCleanerRuntimeStatus(runtimeStatus.message);
    setCleanerRuntimeError(runtimeStatus.isError);
  }, [systemCapabilities]);

  useEffect(() => {
    if (conversionMode !== "audiobook" && conversionMode !== "document_audio") {
      setError("invalid conversion mode");
    }
  }, [conversionMode]);

  const setSelectedEngine = useCallback((value: string) => {
    setSelectedEngineState(value);
    setVoiceFilter("");
    setDebouncedVoiceFilter("");
  }, []);

  const setSelectedVoice = useCallback((value: string) => {
    setSelectedVoiceState(value);
  }, []);

  const setSpeed = useCallback((value: string) => {
    setSpeedInput(value);
  }, []);

  const setSelectedQuantization = useCallback((value: string) => {
    setSelectedQuantizationState(value);
  }, []);

  const setCleanerBackend = useCallback((value: string) => {
    setCleanerBackendConfigured(true);
    setCleanerBackendState(value);
  }, []);

  const setCleanerModel = useCallback((value: string) => {
    setCleanerBackendConfigured(true);
    setCleanerModelState(value);
  }, []);

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

  const runParallelWarmup = useCallback(async () => {
    if (!apiClient) {
      setParallelWorkerError(true);
      setParallelWorkerStatus("Backend is not connected.");
      return;
    }

    setParallelWarmupLoading(true);
    setParallelWorkerError(false);
    setParallelWorkerStatus("Running model warm-up memory calibration...");
    try {
      const quantization = selectedQuantization.trim() || null;
      const requestedWarmupMax =
        parallelWorkerMode === "manual"
          ? selectedParallelWorkerLimit
          : parallelRecommendation?.requested_parallel_chapters ??
            parallelRecommendation?.max_safe_parallel_chapters ??
            Math.max(DEFAULT_PARALLEL_WORKER_LIMIT, Number.parseInt(parallelManualMax, 10) || 1);
      const recommendation = await apiClient.warmupParallelWorkers({
        voice: selectedVoice || "am_adam",
        speed,
        tts_engine: selectedEngine || null,
        tts_quantization: quantization,
        cleaner_backend: cleanerBackend,
        cleaner_model: cleanerBackend === "mlx" ? cleanerModel || null : null,
        cleaner_api_model: cleanerBackend === "api" ? cleanerModel || null : null,
        cleaner_api_key: cleanerBackend === "api" ? cleanerApiKey.trim() || null : null,
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
    cleanerApiKey,
    cleanerBackend,
    cleanerModel,
    parallelManualMax,
    parallelRecommendation,
    parallelWorkerMode,
    selectedEngine,
    selectedParallelWorkerLimit,
    selectedQuantization,
    selectedVoice,
    speed,
  ]);

  return {
    loading,
    error,
    selectedEngine,
    setSelectedEngine,
    selectedVoice,
    setSelectedVoice,
    engineOptions,
    filteredVoiceOptions,
    voiceFilter,
    setVoiceFilter,
    speedInput,
    speed,
    setSpeed,
    selectedQuantization,
    setSelectedQuantization,
    quantizationOptions,
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
