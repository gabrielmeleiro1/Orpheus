import { formatListeningLength } from "../../format/time";

export const VOICE_PREVIEW_DEFAULT_TEXT = "The quick brown fox jumps over the lazy dog.";

/** Windows Explorer's Copy as path wraps a path in double quotes. */
export function unwrapQuotedSourcePath(value: string): string {
  const trimmed = value.trim();
  return trimmed.length >= 2 && trimmed.startsWith('"') && trimmed.endsWith('"')
    ? trimmed.slice(1, -1)
    : value;
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
  return `${normalized.slice(0, maxChars - 1)}…`;
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

export function pathsFromUriList(uriList: string): string[] {
  const paths: string[] = [];
  for (const rawLine of uriList.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) {
      continue;
    }

    let url: URL;
    try {
      url = new URL(line);
    } catch {
      continue;
    }
    if (url.protocol !== "file:") {
      continue;
    }

    let pathname = url.pathname;
    try {
      pathname = decodeURIComponent(pathname);
    } catch {
      // Keep the encoded path when malformed escapes prevent decoding.
    }
    if (/^\/[A-Za-z]:[\\/]/.test(pathname)) {
      pathname = pathname.slice(1);
    }
    paths.push(url.host ? `//${url.host}${pathname}` : pathname);
  }
  return paths;
}

// Mirrors ESTIMATED_CHARS_PER_AUDIO_SECOND in modules/ingestion/preflight.py,
// so this estimate and the queued row's (the backend's) agree for the same text.
const ESTIMATED_CHARACTERS_PER_AUDIO_SECOND = 18.5;

/** Size and rough narration length of pasted text, shown before Start. */
export function pastedTextEstimate(text: string): string {
  const characters = text.trim().length;
  if (characters === 0) return "";
  return `${characters.toLocaleString("en-US")} characters · About ${formatListeningLength(Math.round(characters / ESTIMATED_CHARACTERS_PER_AUDIO_SECOND))} of audio`;
}
