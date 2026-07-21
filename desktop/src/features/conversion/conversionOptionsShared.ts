import type {
  ModelOptionsResponse,
  SystemCapabilitiesResponse,
  TermSelectOption,
  TtsEngineOption,
  TtsVoiceOption,
} from "../../types/api";

const SETTINGS_KEY = "orpheus.desktop.settings.v1";

/** Same-document notification when desktop settings persistence changes. */
export const DESKTOP_SETTINGS_CHANGED_EVENT = "orpheus:desktop-settings-changed";

export interface PersistedSettings {
  tts_engine?: string;
  voice?: string;
  speed?: number;
  tts_quantization?: string;
  cleaner_backend?: string;
  cleaner_model?: string;
  cleaner_backend_configured?: boolean;
  cleaner_cuda_device?: string;
  cleaner_cuda_quantization?: string;
  parallel_worker_mode?: "auto" | "manual";
  parallel_manual_max?: number;
}

export function readPersistedSettings(): PersistedSettings {
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

export function savePersistedSettings(settings: PersistedSettings): void {
  try {
    window.localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
    window.dispatchEvent(new CustomEvent(DESKTOP_SETTINGS_CHANGED_EVENT));
  } catch {
    // Ignore storage failures.
  }
}

// Labels show display names only — raw ids stay out of visible copy but
// remain matchable through the search haystack.
export function mapEngineOption(engine: TtsEngineOption): TermSelectOption {
  const id = String(engine.id ?? "").trim();
  const displayName = String(engine.display_name ?? engine.name ?? "").trim();
  return {
    value: id,
    label: displayName || id,
  };
}

export function mapVoiceOption(voice: TtsVoiceOption): TermSelectOption {
  const voiceId = String(voice.id ?? "").trim();
  const locale = String(voice.locale ?? voice.language ?? "").trim();
  const name = String(voice.name ?? "").trim();
  const styleTags = Array.isArray(voice.style_tags) ? voice.style_tags.join(" ") : "";

  return {
    value: voiceId,
    label: (name || voiceId) + (locale ? ` (${locale})` : ""),
    search: [voiceId, name, locale, styleTags].join(" ").toLowerCase(),
  };
}

export function parseSpeed(value: string): number {
  const parsed = Number.parseFloat(value);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return 1;
  }
  return parsed;
}

export function findEngine(models: ModelOptionsResponse | null, engineId: string): TtsEngineOption | null {
  if (!models || !engineId.trim()) {
    return null;
  }
  return models.tts_engines.find((engine) => engine.id === engineId) ?? null;
}

export function buildQuantizationOptions(
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
