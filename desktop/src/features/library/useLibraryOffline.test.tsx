// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import type { LibraryBookSummary } from "../../types/api";
import { useLibrary, type UseLibraryResult } from "./useLibrary";

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

const summary = (id: number): LibraryBookSummary => ({
  id, title: `Book ${id}`, author: null, source_path: null, conversion_mode: "audiobook", tags: "", favorite: false,
  source_available: true, total_chapters: 1, completed_chapters: 1,
});

function client(): ApiClient {
  return {
    getLibrary: vi.fn(async () => ({ items: [summary(1), summary(2)], count: 2, library_id: "library", library_epoch: "epoch" })),
    getLibraryCount: vi.fn(async () => ({ count: 2 })),
    getLibraryBook: vi.fn(async (id: number) => ({ ...summary(id), source_type: "epub", playback_path: null,
      playback_status: "ready", playback_position_sec: 0, chapters: [], library_id: "library", library_epoch: "epoch" })),
  } as unknown as ApiClient;
}

it.each([
  ["the health check fails", (api: ApiClient) => ({ apiClient: api, backendDisconnected: true })],
  ["the desktop shell drops the client", () => ({ apiClient: null, backendDisconnected: true })],
])("keeps the last known shelf, marked offline, when %s", async (_case, disconnect) => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const api = client();
  let library!: UseLibraryResult;
  function Probe({ apiClient, backendDisconnected }: { apiClient: ApiClient | null; backendDisconnected: boolean }) {
    library = useLibrary({ apiClient, backendDisconnected });
    return null;
  }
  const root = createRoot(document.createElement("div"));
  try {
    await act(async () => root.render(<Probe apiClient={api} backendDisconnected={false} />));
    expect(library.items).toHaveLength(2);
    expect(library.offline).toBe(false);

    await act(async () => root.render(<Probe {...disconnect(api)} />));
    expect(library.items.map((item) => item.id)).toEqual([1, 2]);
    expect(library.indexedCount).toBe(2);
    expect(library.offline).toBe(true);
    expect(library.errorMessage).toMatch(/disconnected/i);

    // Reconnecting reloads the real list and drops the offline mark.
    await act(async () => root.render(<Probe apiClient={client()} backendDisconnected={false} />));
    expect(library.items).toHaveLength(2);
    expect(library.offline).toBe(false);
    expect(library.errorMessage).toBe("");
  } finally {
    await act(async () => root.unmount());
  }
});

it("keeps the offline shelf until the reconnected list has loaded, then shows the live list", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const api = client();
  let resolveList!: (page: unknown) => void;
  const reconnected = client();
  vi.mocked(reconnected.getLibrary).mockImplementation(() => new Promise((resolve) => { resolveList = resolve as (page: unknown) => void; }));
  let library!: UseLibraryResult;
  const rendered: number[][] = [];
  function Probe({ apiClient, backendDisconnected }: { apiClient: ApiClient | null; backendDisconnected: boolean }) {
    library = useLibrary({ apiClient, backendDisconnected });
    rendered.push(library.items.map((item) => item.id));
    return null;
  }
  const root = createRoot(document.createElement("div"));
  try {
    await act(async () => root.render(<Probe apiClient={api} backendDisconnected={false} />));
    await act(async () => root.render(<Probe apiClient={null} backendDisconnected />));
    rendered.length = 0;
    await act(async () => root.render(<Probe apiClient={reconnected} backendDisconnected={false} />));
    expect(reconnected.getLibrary).toHaveBeenCalled();
    expect(rendered.length).toBeGreaterThan(0);
    expect(rendered.every((ids) => ids.join() === "1,2")).toBe(true);
    expect(library.offline).toBe(true);

    await act(async () => {
      resolveList({ items: [summary(3)], count: 1, library_id: "library", library_epoch: "epoch" });
    });
    expect(library.items.map((item) => item.id)).toEqual([3]);
    expect(library.offline).toBe(false);
  } finally {
    await act(async () => root.unmount());
  }
});
