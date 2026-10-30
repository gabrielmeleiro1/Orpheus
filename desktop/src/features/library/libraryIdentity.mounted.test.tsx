// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "../../api/client";
import type { LibraryBookDetail } from "../../types/api";
import { playbackCheckpointBookIdentity, readPlaybackCheckpoint, writePlaybackCheckpoint } from "../player/playbackCheckpoint";
import { useAudioPlayer } from "../player/useAudioPlayer";
import { useLibrary } from "./useLibrary";

const book: LibraryBookDetail = {
  library_id: "library", library_epoch: "selected", id: 1, title: "Original",
  author: null, tags: "", favorite: false, source_path: "book.epub", source_type: "epub",
  conversion_mode: "audiobook", source_available: true, total_chapters: 0, chapters: [],
  playback_path: "/book.m4b", playback_url: "/api/library/1/audio", playback_status: "ready",
  playback_position_sec: 7,
};

describe("library identity across desktop requests", () => {
  let root: Root;
  let library: ReturnType<typeof useLibrary>;
  let player: ReturnType<typeof useAudioPlayer>;
  let replaced: boolean;
  let rejectMutation: boolean;
  let requests: Array<{ path: string; body: Record<string, unknown> }>;

  beforeEach(async () => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    window.localStorage.clear();
    replaced = false;
    rejectMutation = false;
    requests = [];
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
    vi.spyOn(HTMLMediaElement.prototype, "readyState", "get").mockReturnValue(1);
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = new URL(String(input));
      const current = replaced ? { ...book, title: "Restored", library_epoch: "replacement" } : book;
      const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
      if (!init?.method || init.method === "GET") {
        if (url.pathname === "/api/library") return json({ ...current, items: [current], count: 1 });
        if (url.pathname === "/api/library/1") return json(current);
      }
      const body = init?.body ? JSON.parse(String(init.body)) : Object.fromEntries(url.searchParams);
      requests.push({ path: url.pathname, body });
      if (rejectMutation) {
        replaced = true;
        return json({ detail: { code: "library_replaced", message: "Library was replaced" } }, 409);
      }
      if (url.pathname === "/api/desktop/query-token") return json({ token: "scoped", expires_at: 1 });
      if (url.pathname.endsWith("/playback-position")) return json({ ...current, position_sec: body.position_sec });
      if (url.pathname.startsWith("/api/conversions/repair")) return json({ job_id: "repair", status: "running" });
      if (init?.method === "DELETE") return json({ deleted: true, book_id: 1 });
      if (init?.method === "PATCH") return json({ ...current, ...body });
      throw new Error(`Unexpected request: ${url}`);
    });
    const apiClient = createApiClient({ baseUrl: "http://desktop.test", token: "secret" });
    function Probe() {
      library = useLibrary({ apiClient });
      player = useAudioPlayer({ apiClient, library });
      return <audio ref={player.audioRef} />;
    }
    root = createRoot(document.createElement("div"));
    await act(async () => root.render(<Probe />));
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    vi.restoreAllMocks();
    window.localStorage.clear();
  });

  it("retires loaded audio when a refresh discovers the same book in a new epoch", async () => {
    await act(async () => player.togglePlay());
    const generation = library.libraryGeneration!;
    replaced = true;
    await act(async () => library.refreshLibrary());
    expect(library.libraryGeneration).toBeGreaterThan(generation);
    expect(library.selectedBookDetail?.library_epoch).toBe("replacement");
    expect(player.audioRef.current!.getAttribute("src")).toBeNull();
  });

  it("resumes the server position after a restore discovered by a fresh desktop session", async () => {
    writePlaybackCheckpoint(book.id, 90, playbackCheckpointBookIdentity(book));
    window.localStorage.setItem("orpheus.playback.v2.1", JSON.stringify({
      version: 2, bookIdentity: JSON.stringify([book.id, "", book.source_path]), positionSec: 80,
    }));
    await act(async () => root.unmount());
    replaced = true;
    const apiClient = createApiClient({ baseUrl: "http://desktop.test", token: "secret" });
    function Reopened() {
      library = useLibrary({ apiClient });
      player = useAudioPlayer({ apiClient, library });
      return <audio ref={player.audioRef} />;
    }
    root = createRoot(document.createElement("div"));
    await act(async () => root.render(<Reopened />));
    await act(async () => player.togglePlay());
    expect(player.audioRef.current!.currentTime).toBe(7);
    await act(async () => player.audioProps.onPause());
    expect(requests.at(-1)?.body).toEqual({ position_sec: 7, expected_epoch: "replacement" });
    expect(readPlaybackCheckpoint(book.id, playbackCheckpointBookIdentity(book))).toBe(90);
    expect(readPlaybackCheckpoint(book.id, playbackCheckpointBookIdentity({ ...book, library_epoch: "replacement" }))).toBe(7);
  });

  const actions = ["metadata", "delete", "repair", "playback", "audio"] as const;
  async function perform(action: typeof actions[number]) {
    if (action === "metadata") await library.updateSelectedBookMetadata({ title: "Edited", author: "", tags: "" });
    if (action === "delete") await library.deleteSelectedBook(player.releaseBookAudio);
    if (action === "repair") await library.repairSelectedBook(player.releaseBookAudio);
    if (action === "audio") await player.togglePlay();
    if (action === "playback") {
      player.audioRef.current!.currentTime = 22;
      player.audioProps.onPause();
    }
  }

  it.each(actions)("sends the acted-on epoch for %s", async (action) => {
    if (action === "playback") await act(async () => player.togglePlay());
    await act(async () => perform(action));
    expect(requests.at(-1)?.body.expected_epoch).toBe("selected");
  });

  it.each(actions)("reloads the session after a stale %s without replaying it", async (action) => {
    if (action === "playback") await act(async () => player.togglePlay());
    const generation = library.libraryGeneration!;
    requests = [];
    rejectMutation = true;
    await act(async () => perform(action));
    expect(requests).toHaveLength(1);
    expect(library.libraryGeneration).toBeGreaterThan(generation);
    expect(library.selectedBookDetail?.library_epoch).toBe("replacement");
    expect(player.audioRef.current!.getAttribute("src")).toBeNull();
  });
});
