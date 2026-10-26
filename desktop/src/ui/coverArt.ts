import { createContext, useContext, useEffect, useState } from "react";

import type { ApiClient } from "../api/client";

/** The client covers are fetched with; absent in tests and before the backend connects. */
export const CoverClientContext = createContext<ApiClient | null>(null);
export const CoverGenerationContext = createContext(0);
export const CoverRefreshContext = createContext(false);

// Shelves request many covers at once; a PDF thumbnail is rendered on demand.
const MAX_CONCURRENT_COVER_REQUESTS = 4;

interface CoverEntry {
  request: Promise<string | null>;
  users: number;
  url: string | null;
}
const coverCache = new WeakMap<ApiClient, Map<string, CoverEntry>>();
let activeRequests = 0;
const waiting: Array<() => void> = [];

export async function withRequestSlot<T>(run: () => Promise<T>): Promise<T> {
  if (activeRequests >= MAX_CONCURRENT_COVER_REQUESTS) {
    // A finishing request hands its slot straight to the next waiter, so a
    // caller arriving before the waiter resumes cannot take it as well.
    await new Promise<void>((resolve) => waiting.push(resolve));
  } else {
    activeRequests += 1;
  }
  try {
    return await run();
  } finally {
    const next = waiting.shift();
    if (next) next();
    else activeRequests -= 1;
  }
}

function acquireCover(apiClient: ApiClient, bookId: number, key: string) {
  let cache = coverCache.get(apiClient);
  if (!cache) {
    cache = new Map();
    coverCache.set(apiClient, cache);
  }
  let entry = cache.get(key);
  if (!entry) {
    const created: CoverEntry = { users: 0, url: null, request: Promise.resolve(null) };
    entry = created;
    cache.set(key, created);
    created.request = withRequestSlot(async () => {
      await Promise.resolve();
      // Covers scrolled/unmounted before their request slot need no work.
      if (created.users === 0) return null;
      const blob = await apiClient.requestBlob(`/api/library/${bookId}/cover`, { timeoutMs: 20_000 });
      if (created.users > 0 && blob.size > 0 && blob.type.startsWith("image/")) {
        created.url = URL.createObjectURL(blob);
      }
      return created.url;
    }).catch(() => {
      // Only an actual empty response is a cacheable missing cover. A failed
      // request can be retried by the next mount or library refresh.
      if (cache.get(key) === created) cache.delete(key);
      return null;
    });
  }
  entry.users += 1;
  const owned = entry;
  return {
    request: owned.request,
    release: () => {
      owned.users -= 1;
      // A same-key effect refresh/StrictMode remount can reuse its lease.
      queueMicrotask(() => {
        if (owned.users !== 0) return;
        if (cache.get(key) === owned) cache.delete(key);
        if (owned.url) URL.revokeObjectURL(owned.url);
        owned.url = null;
      });
    },
  };
}

/** Object URL of the book's real cover (EPUB cover or PDF first page), or null while loading or when it has none. */
export function useCoverArt(bookId: number | null | undefined, sourceKey = ""): string | null {
  const apiClient = useContext(CoverClientContext);
  const generation = useContext(CoverGenerationContext);
  const refreshing = useContext(CoverRefreshContext);
  const key = bookId ? JSON.stringify([generation, bookId, sourceKey]) : "";
  const [resolved, setResolved] = useState<{ client: ApiClient | null; key: string; url: string | null }>({ client: null, key: "", url: null });

  useEffect(() => {
    if (!apiClient || !bookId || typeof apiClient.requestBlob !== "function" || typeof URL.createObjectURL !== "function") return undefined;
    let current = true;
    const lease = acquireCover(apiClient, bookId, key);
    void lease.request.then((url) => {
      if (current) setResolved({ client: apiClient, key, url });
    });
    return () => {
      current = false;
      lease.release();
    };
  }, [apiClient, bookId, key, refreshing]);

  return resolved.client === apiClient && resolved.key === key ? resolved.url : null;
}
