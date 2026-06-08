import type { BackendConnectionState } from "../../types/api";

export const VOICE_PREVIEW_DEFAULT_TEXT = "The quick brown fox jumps over the lazy dog.";

export function nowStatus(connectionState: BackendConnectionState): string {
  if (connectionState === "checking") {
    return "checking";
  }
  if (connectionState === "connected") {
    return "connected";
  }
  return "error";
}

export function isTauriRuntime(): boolean {
  return Boolean((window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__);
}

export function splitPath(path: string): string {
  const segments = path.split(/[\\/]+/).filter(Boolean);
  return segments.length > 0 ? segments[segments.length - 1] : path;
}

export function compactSample(text: string, maxChars = 260): string {
  const normalized = text.replace(/\s+/g, " ").trim();
  if (normalized.length <= maxChars) {
    return normalized;
  }
  return `${normalized.slice(0, maxChars - 3)}...`;
}

// Alias map must stay in sync with modules/tts/quantization.py (QUANTIZATION_ALIASES).
const QUANTIZATION_ALIASES: Record<string, string> = {
  q8: "8bit",
  q4: "4bit",
  full: "bf16",
};

export function normalizeQuantizationValue(value: string): string | null {
  if (!value) {
    return null;
  }
  const normalized = value.trim().toLowerCase();
  return QUANTIZATION_ALIASES[normalized] ?? normalized;
}

export function extractDroppedPaths(payload: unknown): string[] {
  if (Array.isArray(payload)) {
    return payload.filter((item): item is string => typeof item === "string" && item.trim().length > 0);
  }

  if (payload && typeof payload === "object") {
    const paths = (payload as { paths?: unknown }).paths;
    if (Array.isArray(paths)) {
      return paths.filter((item): item is string => typeof item === "string" && item.trim().length > 0);
    }
  }

  if (typeof payload === "string" && payload.trim().length > 0) {
    return [payload.trim()];
  }

  return [];
}
