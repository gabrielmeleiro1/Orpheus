import type { ConversionQueueItem } from "./conversionPanelModel";

/**
 * The Create queue outlives a restart. What is saved is the user's list: the
 * rows, their order, titles, authors and outcomes. Nothing about a row's live
 * state survives as-is: a row that was converting when the app closed is not
 * known to have finished, and a row that never started is inspected again so
 * a moved or deleted source shows up before Start rather than after it.
 */
const QUEUE_STORAGE_KEY = "orpheus.conversion.queue.v1";
/** Bounds what one stored record can cost; the oldest finished rows go first. */
const MAX_STORED_ITEMS = 200;

export const INTERRUPTED_CONVERSION_MESSAGE =
  "Orpheus closed during this conversion. Check Library before converting it again.";

interface StoredQueue {
  version: 1;
  items: ConversionQueueItem[];
  selectedQueueId: string | null;
}

export interface RestoredQueue {
  items: ConversionQueueItem[];
  selectedQueueId: string | null;
}

interface QueueStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

const STATUSES = new Set(["ready", "preflighting", "warning", "converting", "done", "failed"]);
const MODES = new Set(["audiobook", "document_audio"]);

function defaultStorage(): QueueStorage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

function isStoredItem(value: unknown): value is ConversionQueueItem {
  if (!value || typeof value !== "object") return false;
  const item = value as Record<string, unknown>;
  return typeof item.id === "string" && item.id !== ""
    && typeof item.sourcePath === "string" && item.sourcePath.trim() !== ""
    && typeof item.label === "string"
    && typeof item.title === "string"
    && typeof item.author === "string"
    && typeof item.error === "string"
    && typeof item.status === "string" && STATUSES.has(item.status)
    && typeof item.conversionMode === "string" && MODES.has(item.conversionMode)
    && (item.jobId === null || typeof item.jobId === "string")
    && (item.bookId === undefined || item.bookId === null || Number.isInteger(item.bookId))
    && (item.preflight === null || (typeof item.preflight === "object" && !Array.isArray(item.preflight)));
}

/** How a saved row comes back after a restart. */
function reviveItem(item: ConversionQueueItem): ConversionQueueItem {
  if (item.status === "converting") {
    return { ...item, status: "warning", error: INTERRUPTED_CONVERSION_MESSAGE };
  }
  if (item.jobId === null && (item.status === "ready" || item.status === "warning" || item.status === "preflighting")) {
    // The queue controller re-inspects every "preflighting" row once a backend is connected.
    return { ...item, status: "preflighting", error: "", preflight: null };
  }
  return item;
}

export function readStoredQueue(storage: QueueStorage | null = defaultStorage()): RestoredQueue {
  const empty: RestoredQueue = { items: [], selectedQueueId: null };
  if (!storage) return empty;
  try {
    const raw = storage.getItem(QUEUE_STORAGE_KEY);
    if (!raw) return empty;
    const record = JSON.parse(raw) as Partial<StoredQueue>;
    if (record.version !== 1 || !Array.isArray(record.items)) return empty;
    const seen = new Set<string>();
    const items = record.items.filter((item): item is ConversionQueueItem => {
      if (!isStoredItem(item) || seen.has(item.id)) return false;
      seen.add(item.id);
      return true;
    }).map(reviveItem);
    const selectedQueueId = typeof record.selectedQueueId === "string" && seen.has(record.selectedQueueId)
      ? record.selectedQueueId
      : null;
    return { items, selectedQueueId };
  } catch {
    return empty;
  }
}

function boundedItems(items: ConversionQueueItem[]): ConversionQueueItem[] {
  if (items.length <= MAX_STORED_ITEMS) return items;
  let excess = items.length - MAX_STORED_ITEMS;
  // Drop the oldest finished rows first; rows still waiting are the point of saving.
  const kept = items.filter((item) => {
    if (excess > 0 && item.status === "done") {
      excess -= 1;
      return false;
    }
    return true;
  });
  return kept.slice(-MAX_STORED_ITEMS);
}

export function writeStoredQueue(
  items: ConversionQueueItem[],
  selectedQueueId: string | null,
  storage: QueueStorage | null = defaultStorage(),
): void {
  if (!storage) return;
  try {
    if (items.length === 0) {
      storage.removeItem(QUEUE_STORAGE_KEY);
      return;
    }
    const stored = boundedItems(items);
    const record: StoredQueue = {
      version: 1,
      items: stored,
      selectedQueueId: stored.some((item) => item.id === selectedQueueId) ? selectedQueueId : null,
    };
    storage.setItem(QUEUE_STORAGE_KEY, JSON.stringify(record));
  } catch {
    // Full or blocked storage only costs the queue across a restart.
  }
}
