export const VOICE_PREVIEW_DEFAULT_TEXT = "The quick brown fox jumps over the lazy dog.";

export function splitPath(path: string): string {
  const segments = path.split(/[\\/]+/).filter(Boolean);
  return segments.length > 0 ? segments[segments.length - 1] : path;
}

/** Middle-truncate a long string, keeping both ends readable. */
export function middleTruncate(value: string, maxChars: number): string {
  if (value.length <= maxChars) {
    return value;
  }
  const keep = Math.max(1, maxChars - 1);
  const front = Math.ceil(keep / 2);
  const back = Math.floor(keep / 2);
  return `${value.slice(0, front)}…${value.slice(value.length - back)}`;
}

export interface SourceDisplay {
  /** Friendly basename shown to the user (e.g. "Pasted Document.md"). */
  name: string;
  /** Containing directory for real file paths, middle-truncated. Empty for upload handles. */
  dir: string;
}

/**
 * Describe a chosen source for display: a friendly basename plus (for real
 * filesystem paths) its middle-truncated directory. Upload handles such as
 * "upload:1699_ab_Pasted Document.md" collapse to just their friendly label.
 * Callers keep the full path/handle for the tooltip.
 */
export function describeSourceDisplay(sourcePath: string, label?: string): SourceDisplay {
  const trimmed = sourcePath.trim();
  if (!trimmed) {
    return { name: "", dir: "" };
  }

  const friendlyLabel = label?.trim();
  // A URL-ish scheme ("upload:…") has no filesystem directory to surface, but a
  // Windows drive letter ("C:\…") is a real path — exclude it from the scheme test.
  const isHandle = /^[a-z][a-z0-9+.-]*:/i.test(trimmed) && !/^[a-z]:[\\/]/i.test(trimmed);
  const name = friendlyLabel || splitPath(trimmed);
  if (isHandle) {
    // Never surface the raw handle: "upload:<stamp>_<hash>_<file>" collapses
    // to its original file name when no queue-item label is around.
    return { name: friendlyLabel || name.replace(/^upload:\d+_[0-9a-f]+_/i, ""), dir: "" };
  }

  const separatorIndex = Math.max(trimmed.lastIndexOf("/"), trimmed.lastIndexOf("\\"));
  const dir = separatorIndex > 0 ? trimmed.slice(0, separatorIndex) : "";
  return { name, dir: dir ? middleTruncate(dir, 44) : "" };
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
