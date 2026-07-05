import { useCallback, useEffect, useMemo, useRef, useState, type MutableRefObject } from "react";

import type { ApiClient } from "../../api/client";
import type { ModelOptionsResponse, SystemCapabilitiesResponse, TermSelectOption } from "../../types/api";

import {
  buildQuantizationOptions,
  findEngine,
  mapEngineOption,
  mapVoiceOption,
  parseSpeed,
  type PersistedSettings,
} from "./conversionOptionsShared";
import { isAbortLikeError } from "./useLatestRequest";

const VOICE_FILTER_DEBOUNCE_MS = 180;

// The backend's local-runtime probe finishes a few seconds after startup and
// reports runtime_probe.state === "pending"/"stale" until it does. We re-fetch
// capabilities with modest backoff until the probe reports "fresh" so that
// local backends stop looking unavailable, giving up after a bounded window.
const CAPABILITIES_PROBE_POLL_BASE_MS = 1500;
const CAPABILITIES_PROBE_POLL_MAX_MS = 6000;
const CAPABILITIES_PROBE_MAX_WAIT_MS = 60000;

export interface UseTtsOptionsArgs {
  apiClient: ApiClient | null;
  persistedSettingsRef: MutableRefObject<PersistedSettings>;
  settingsHydratedRef: MutableRefObject<boolean>;
}

export interface UseTtsOptionsResult {
  loading: boolean;
  error: string;
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
}: UseTtsOptionsArgs): UseTtsOptionsResult {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const [modelOptions, setModelOptions] = useState<ModelOptionsResponse | null>(null);
  const [systemCapabilities, setSystemCapabilities] = useState<SystemCapabilitiesResponse | null>(null);
  const [allVoiceOptions, setAllVoiceOptions] = useState<TermSelectOption[]>([]);
  const probeStateRef = useRef<string | undefined>(undefined);

  const [selectedEngine, setSelectedEngineState] = useState("");
  const [selectedVoice, setSelectedVoiceState] = useState("");
  const [voiceFilter, setVoiceFilter] = useState("");
  const [debouncedVoiceFilter, setDebouncedVoiceFilter] = useState("");

  const [speedInput, setSpeedInput] = useState("1.0");
  const [selectedQuantization, setSelectedQuantizationState] = useState("");

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
      probeStateRef.current = undefined;
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
        probeStateRef.current = capabilities.runtime_probe?.state;

        const persisted = persistedSettingsRef.current;

        const nextEngine =
          [persisted.tts_engine, String(capabilities.default_tts_engine ?? "")]
            .map((value) => String(value ?? "").trim())
            .find((candidate) => candidate && models.tts_engines.some((engine) => engine.id === candidate)) ??
          models.tts_engines[0]?.id ??
          "";

        setSelectedEngineState(nextEngine);

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
        setError(loadError instanceof Error ? loadError.message : "failed to load conversion options");
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
  }, [apiClient, persistedSettingsRef, settingsHydratedRef]);

  useEffect(() => {
    if (!apiClient) {
      return;
    }

    let cancelled = false;
    let timer: number | null = null;
    const controller = new AbortController();
    const startedAt = Date.now();
    let attempt = 0;

    const scheduleNext = () => {
      if (cancelled || probeStateRef.current === "fresh") {
        return;
      }
      if (Date.now() - startedAt >= CAPABILITIES_PROBE_MAX_WAIT_MS) {
        return;
      }
      const delay = Math.min(
        CAPABILITIES_PROBE_POLL_BASE_MS * 2 ** attempt,
        CAPABILITIES_PROBE_POLL_MAX_MS,
      );
      attempt += 1;
      timer = window.setTimeout(() => void poll(), delay);
    };

    const poll = async () => {
      if (cancelled || probeStateRef.current === "fresh") {
        return;
      }
      try {
        const capabilities = await apiClient.getSystemCapabilities({ signal: controller.signal });
        if (cancelled) {
          return;
        }
        probeStateRef.current = capabilities.runtime_probe?.state;
        setSystemCapabilities(capabilities);
      } catch (pollError: unknown) {
        if (cancelled || isAbortLikeError(pollError)) {
          return;
        }
      }
      scheduleNext();
    };

    scheduleNext();

    return () => {
      cancelled = true;
      controller.abort();
      if (timer !== null) {
        window.clearTimeout(timer);
      }
    };
  }, [apiClient]);

  useEffect(() => {
    if (!apiClient || !selectedEngine.trim()) {
      setAllVoiceOptions([]);
      setSelectedVoiceState("");
      return;
    }

    const controller = new AbortController();

    const loadVoices = async () => {
      try {
        const response = await apiClient.getVoiceOptions(selectedEngine, {
          signal: controller.signal,
        });
        if (controller.signal.aborted) {
          return;
        }

        const mapped = response.voices.map((voice) => mapVoiceOption(voice));
        setAllVoiceOptions(mapped);
      } catch (loadError: unknown) {
        if (controller.signal.aborted || isAbortLikeError(loadError)) {
          return;
        }

        const fallbackVoices = findEngine(modelOptions, selectedEngine)?.voices ?? [];
        const mapped = fallbackVoices.map((voice) => mapVoiceOption(voice));
        setAllVoiceOptions(mapped);
      }
    };

    void loadVoices();

    return () => {
      controller.abort();
    };
  }, [apiClient, modelOptions, selectedEngine]);

  useEffect(() => {
    setSelectedVoiceState((current) => {
      const preferred = persistedSettingsRef.current.voice;
      return (
        [current, preferred]
          .map((value) => String(value ?? "").trim())
          .find((candidate) => candidate && allVoiceOptions.some((voice) => voice.value === candidate)) ??
        allVoiceOptions[0]?.value ??
        ""
      );
    });
  }, [allVoiceOptions, persistedSettingsRef]);

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
    loading,
    error,
    modelOptions,
    systemCapabilities,
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
  };
}
