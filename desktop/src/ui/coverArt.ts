import { createContext, useContext, useEffect, useState } from "react";

import type { ApiClient } from "../api/client";

/** The client covers are fetched with; absent in tests and before the backend connects. */
export const CoverClientContext = createContext<ApiClient | null>(null);
export const CoverGenerationContext = createContext(0);

/** Announces that the library list finished loading, so covers whose request failed can retry. */
export interface CoverRefreshSignal {
  subscribe: (listener: () => void) => () => void;
}

export function createCoverRefreshSignal(): CoverRefreshSignal & { notify: () => void } {
  const listeners = new Set<() => void>();
  return {
    subscribe: (listener) => {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    notify: () => {
      for (const listener of [...listeners]) listener();
    },
  };
}

// A stable signal rather than a changing value: only covers waiting to retry react to it.
export const CoverRefreshContext = createContext<CoverRefreshSignal | null>(null);

// Shelves request many covers at once; a PDF thumbnail is rendered on demand.
const MAX_CONCURRENT_COVER_REQUESTS = 4;

interface CoverEntry {
  request: Promise<string | null>;
  users: number;
  url: string | null;
  failed: boolean;
  controller: AbortController;
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
    const created: CoverEntry = { users: 0, url: null, request: Promise.resolve(null), failed: false, controller: new AbortController() };
    entry = created;
    cache.set(key, created);
    created.request = withRequestSlot(async () => {
      await Promise.resolve();
      // Covers scrolled/unmounted before their request slot need no work.
      if (created.users === 0) return null;
      const blob = await apiClient.requestBlob(`/api/library/${bookId}/cover`, { timeoutMs: 20_000, signal: created.controller.signal });
      if (created.users > 0 && blob.size > 0 && blob.type.startsWith("image/")) {
        created.url = URL.createObjectURL(blob);
      }
      return created.url;
    }).catch(() => {
      // Only an actual empty response is a cacheable missing cover. A failed
      // request can be retried by the next mount or library refresh.
      created.failed = true;
      if (cache.get(key) === created) cache.delete(key);
      return null;
    });
  }
  entry.users += 1;
  const owned = entry;
  return {
    request: owned.request,
    failed: () => owned.failed,
    release: () => {
      owned.users -= 1;
      // A same-key effect refresh/StrictMode remount can reuse its lease.
      queueMicrotask(() => {
        if (owned.users !== 0) return;
        if (cache.get(key) === owned) cache.delete(key);
        // No cover uses this entry any more and it has left the cache, so no
        // later mount can join its request: stop the backend render too.
        owned.controller.abort();
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
  const refreshSignal = useContext(CoverRefreshContext);
  const key = bookId ? JSON.stringify([generation, bookId, sourceKey]) : "";
  const [resolved, setResolved] = useState<{ client: ApiClient | null; key: string; url: string | null }>({ client: null, key: "", url: null });
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    if (!apiClient || !bookId || typeof apiClient.requestBlob !== "function" || typeof URL.createObjectURL !== "function") return undefined;
    let current = true;
    let stopWaiting: (() => void) | undefined;
    const lease = acquireCover(apiClient, bookId, key);
    void lease.request.then((url) => {
      if (!current) return;
      setResolved({ client: apiClient, key, url });
      // A failed request is retried after the next library load.
      if (lease.failed() && refreshSignal) {
        stopWaiting = refreshSignal.subscribe(() => setRetry((value) => value + 1));
      }
    });
    return () => {
      current = false;
      stopWaiting?.();
      lease.release();
    };
  }, [apiClient, bookId, key, refreshSignal, retry]);

  return resolved.client === apiClient && resolved.key === key ? resolved.url : null;
}
