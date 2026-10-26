import { useCallback, useEffect, useMemo, useState, type MutableRefObject } from "react";

import type { ApiClient } from "../../api/client";
import { ApiError, createRequestTimeoutError } from "../../api/http";
import type { ModelOptionsResponse, SystemCapabilitiesResponse, TermSelectOption } from "../../types/api";

import {
  buildQuantizationOptions,
  findEngine,
  mapEngineOption,
  mapVoiceOption,
  parseSpeed,
  resolveOptionValue,
  type PersistedSettings,
} from "./conversionOptionsShared";
import {
  getRuntimeBootstrap,
  invalidateRuntimeBootstrap,
  updateRuntimeBootstrapCapabilities,
} from "../../app/runtimeBootstrapCache";
import {
  CAPABILITIES_PROBE_REQUEST_TIMEOUT_MS,
  shouldPollRuntimeCapabilities,
  startRuntimeCapabilityPolling,
} from "./runtimeCapabilityPolling";

const VOICE_FILTER_DEBOUNCE_MS = 180;
// Bootstrap reads the model catalog and cached capability snapshot, without
// waiting for the background runtime probes covered by the 750s poll budget.
const TTS_OPTIONS_BOOTSTRAP_TIMEOUT_MS = CAPABILITIES_PROBE_REQUEST_TIMEOUT_MS;

interface UseTtsOptionsArgs {
  apiClient: ApiClient | null;
  persistedSettingsRef: MutableRefObject<PersistedSettings>;
  settingsHydratedRef: MutableRefObject<boolean>;
  runtimeSelectionGeneration?: number;
}

export interface UseTtsOptionsResult {
  loading: boolean;
  error: string;
  retryRuntimeOptions: () => void;
  modelOptions: ModelOptionsResponse | null;
  systemCapabilities: SystemCapabilitiesResponse | null;
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
}

export function useTtsOptions({
  apiClient,
  persistedSettingsRef,
  settingsHydratedRef,
  runtimeSelectionGeneration = 0,
}: UseTtsOptionsArgs): UseTtsOptionsResult {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const [reloadAttempt, setReloadAttempt] = useState(0);
  const [loadedRuntime, setLoadedRuntime] = useState<{ client: ApiClient; generation: number; attempt: number } | null>(null);
  const runtimeLoaded = loadedRuntime?.client === apiClient && loadedRuntime?.generation === runtimeSelectionGeneration
    && loadedRuntime?.attempt === reloadAttempt;

  const [modelOptions, setModelOptions] = useState<ModelOptionsResponse | null>(null);
  const [systemCapabilities, setSystemCapabilities] = useState<SystemCapabilitiesResponse | null>(null);
  const [allVoiceOptions, setAllVoiceOptions] = useState<TermSelectOption[]>([]);
  const [loadedVoices, setLoadedVoices] = useState<{
    client: ApiClient; generation: number; attempt: number; engine: string; catalog: ModelOptionsResponse;
  } | null>(null);
  const [voiceError, setVoiceError] = useState("");

  const [selectedEngine, setSelectedEngineState] = useState("");
  const [selectedVoice, setSelectedVoiceState] = useState("");
  const [voiceFilter, setVoiceFilter] = useState("");
  const [debouncedVoiceFilter, setDebouncedVoiceFilter] = useState("");

  const [speedInput, setSpeedInput] = useState("1.0");
  const [selectedQuantization, setSelectedQuantizationState] = useState("");
  const voicesLoaded = runtimeLoaded && loadedVoices?.client === apiClient
    && loadedVoices?.generation === runtimeSelectionGeneration && loadedVoices?.attempt === reloadAttempt
    && loadedVoices?.engine === selectedEngine && loadedVoices?.catalog === modelOptions;

  const retryRuntimeOptions = useCallback(() => {
    if (!apiClient) return;
    invalidateRuntimeBootstrap(apiClient);
    setReloadAttempt((attempt) => attempt + 1);
  }, [apiClient]);

  const engineOptions = useMemo(
    () => (modelOptions?.tts_engines ?? []).map((engine) => mapEngineOption(engine)).filter((option) => option.value),
    [modelOptions],
  );

  const quantizationOptions = useMemo(
    () => buildQuantizationOptions(findEngine(modelOptions, selectedEngine), systemCapabilities),
    [modelOptions, selectedEngine, systemCapabilities],
  );

  const filteredVoiceOptions = useMemo(() => {
    const query = debouncedVoiceFilter.trim().toLowerCase();
    if (!query) {
      return allVoiceOptions;
    }
    return allVoiceOptions.filter((option) => (option.search ?? option.label.toLowerCase()).includes(query));
  }, [allVoiceOptions, debouncedVoiceFilter]);

  const speed = useMemo(() => parseSpeed(speedInput), [speedInput]);

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
      setError(null);
      setModelOptions(null);
      setSystemCapabilities(null);
      setAllVoiceOptions([]);
      return;
    }

    let cancelled = false;
    let optionsLoaded = false;
    let latestCapabilities: SystemCapabilitiesResponse | null = null;

    const reconcileTimeout = (capabilities: SystemCapabilitiesResponse) => {
      const primaryRuntime = String(capabilities.primary_runtime ?? "").trim().toLowerCase();
      const usableTts = capabilities.mlx_runtime_available === true ||
        ((primaryRuntime === "cpu" || primaryRuntime === "cuda") && Boolean(capabilities.default_tts_engine.trim()));
      if (optionsLoaded && usableTts) {
        setError((current) => current instanceof ApiError && current.code === "timeout" ? null : current);
      }
    };

    settingsHydratedRef.current = false;
    setLoading(true);
    setError(null);

    // Bound this consumer's wait without cancelling the bootstrap shared with
    // other panels. A late success can still hydrate this runtime generation.
    const bootstrapTimer = window.setTimeout(() => {
      if (cancelled) return;
      setModelOptions(null);
      setAllVoiceOptions([]);
      setError((current) => current ?? createRequestTimeoutError());
      setLoading(false);
      setLoadedRuntime({ client: apiClient, generation: runtimeSelectionGeneration, attempt: reloadAttempt });
    }, TTS_OPTIONS_BOOTSTRAP_TIMEOUT_MS);

    // Shared bootstrap can resolve with a cold-start pending/stale snapshot
    // after a live poll has already observed a fresher probe. Never let that
    // late cache result clobber Local MLX availability in Settings.
    const poller = startRuntimeCapabilityPolling({
      getSystemCapabilities: (signal) => apiClient.getSystemCapabilities({ signal }),
      onError: (pollError) => {
        if (!cancelled) {
          setError((current) => current ?? pollError);
        }
      },
      onCapabilities: (capabilities) => {
        if (!cancelled) {
          latestCapabilities = capabilities;
          setSystemCapabilities(capabilities);
          updateRuntimeBootstrapCapabilities(apiClient, capabilities);
          reconcileTimeout(capabilities);
        }
      },
    });

    const load = async () => {
      try {
        const { models, capabilities } = await getRuntimeBootstrap(apiClient);

        if (cancelled) {
          return;
        }

        setModelOptions(models);
        optionsLoaded = models.tts_engines.length > 0;
        // A live poll may already have supplied newer capabilities. The
        // bootstrap still completes the catalog needed to recover readiness.
        reconcileTimeout(latestCapabilities ?? capabilities);
        if (!poller.pollApplied) {
          setSystemCapabilities(capabilities);
          poller.noteCapabilities(capabilities);
          // Dashboard may have pinned a stale mlx=false bootstrap during setup.
          // Refresh immediately when Settings/Convert mounts so the Cleaner
          // selector does not wait for the first poll backoff.
          if (shouldPollRuntimeCapabilities(capabilities.runtime_probe, capabilities)) {
            void apiClient
              .getSystemCapabilities({ timeoutMs: CAPABILITIES_PROBE_REQUEST_TIMEOUT_MS })
              .then((fresh) => {
                if (cancelled) {
                  return;
                }
                latestCapabilities = fresh;
                setSystemCapabilities(fresh);
                updateRuntimeBootstrapCapabilities(apiClient, fresh);
                poller.noteCapabilities(fresh);
                reconcileTimeout(fresh);
              })
              .catch(() => {
                // Bounded poller continues on the normal backoff schedule.
              });
          }
        }

        const persisted = persistedSettingsRef.current;

        const nextEngine =
          [persisted.tts_engine, String(capabilities.default_tts_engine ?? "")]
            .map((value) => String(value ?? "").trim())
            .find((candidate) => candidate && models.tts_engines.some((engine) => engine.id === candidate)) ??
          models.tts_engines[0]?.id ??
          "";

        setSelectedEngineState(nextEngine);
        setSelectedVoiceState(String(persisted.voice ?? "").trim());
        setSelectedQuantizationState(String(persisted.tts_quantization ?? "").trim());

        const savedSpeed = Number.isFinite(persisted.speed) && (persisted.speed ?? 0) > 0 ? String(persisted.speed) : "1.0";
        setSpeedInput(savedSpeed);
        settingsHydratedRef.current = true;
      } catch (loadError: unknown) {
        if (cancelled) {
          return;
        }

        setModelOptions(null);
        setSystemCapabilities(null);
        setAllVoiceOptions([]);
        setError(loadError instanceof Error ? loadError : new Error("failed to load conversion options"));
      } finally {
        window.clearTimeout(bootstrapTimer);
        if (!cancelled) {
          setLoading(false);
          setLoadedRuntime({ client: apiClient, generation: runtimeSelectionGeneration, attempt: reloadAttempt });
        }
      }
    };

    void load();

    return () => {
      cancelled = true;
      window.clearTimeout(bootstrapTimer);
      poller.stop();
    };
  }, [apiClient, persistedSettingsRef, settingsHydratedRef, runtimeSelectionGeneration, reloadAttempt]);

  useEffect(() => {
    setVoiceError("");
    if (!apiClient || !runtimeLoaded || !modelOptions || !selectedEngine.trim()) return;

    const controller = new AbortController();
    const applyVoices = (mapped: TermSelectOption[]) => {
      setAllVoiceOptions(mapped);
      setSelectedVoiceState((current) =>
        [current, persistedSettingsRef.current.voice]
          .map((value) => String(value ?? "").trim())
          .find((candidate) => candidate && mapped.some((voice) => voice.value === candidate))
        ?? mapped[0]?.value ?? "",
      );
      setLoadedVoices({ client: apiClient, generation: runtimeSelectionGeneration,
        attempt: reloadAttempt, engine: selectedEngine, catalog: modelOptions });
    };

    const loadVoices = async () => {
      try {
        const response = await apiClient.getVoiceOptions(selectedEngine, {
          signal: controller.signal,
          timeoutMs: TTS_OPTIONS_BOOTSTRAP_TIMEOUT_MS,
        });
        if (controller.signal.aborted) {
          return;
        }

        const mapped = response.voices.map((voice) => mapVoiceOption(voice));
        applyVoices(mapped);
      } catch (loadError: unknown) {
        if (controller.signal.aborted || (loadError instanceof ApiError && loadError.code === "aborted")) {
          return;
        }

        const fallbackVoices = findEngine(modelOptions, selectedEngine)?.voices ?? [];
        const mapped = fallbackVoices.map((voice) => mapVoiceOption(voice));
        applyVoices(mapped);
        if (mapped.length === 0) {
          setVoiceError(loadError instanceof Error ? loadError.message : "failed to load voices");
        }
      }
    };

    void loadVoices();

    return () => {
      controller.abort();
    };
  }, [apiClient, modelOptions, persistedSettingsRef, selectedEngine, runtimeSelectionGeneration, runtimeLoaded, reloadAttempt]);

  useEffect(() => {
    setSelectedQuantizationState((previous) => resolveOptionValue(
      quantizationOptions,
      previous,
      persistedSettingsRef.current.tts_quantization,
      systemCapabilities?.default_tts_quantization,
    ));
  }, [persistedSettingsRef, quantizationOptions, systemCapabilities]);

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

  return {
    loading: Boolean(apiClient) && (loading || !runtimeLoaded || Boolean(modelOptions && selectedEngine && !voicesLoaded)),
    error: error?.message ?? voiceError,
    retryRuntimeOptions,
    // Cleaner hydration must wait for the catalog belonging to the saved selection.
    modelOptions: runtimeLoaded ? modelOptions : null,
    systemCapabilities,
    selectedEngine,
    setSelectedEngine,
    selectedVoice: voicesLoaded ? selectedVoice : "",
    setSelectedVoice,
    engineOptions,
    filteredVoiceOptions: voicesLoaded ? filteredVoiceOptions : [],
    voiceFilter,
    setVoiceFilter,
    speedInput,
    speed,
    setSpeed,
    selectedQuantization,
    setSelectedQuantization,
    quantizationOptions,
  };
}
