// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, type ApiClient } from "../../api/client";
import type { LibraryBookDetail } from "../../types/api";
import { useAudioPlayer } from "../player/useAudioPlayer";
import { usePlaybackPersistence } from "../player/playbackPersistence";
import { useLibrary } from "./useLibrary";
import { useLibraryPanelState } from "./useLibraryPanelState";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

const original: LibraryBookDetail = {
  id: 1, title: "Original book", author: "Original author", tags: "original",
  source_path: null, source_type: "epub", conversion_mode: "audiobook",
  favorite: false, source_available: true, total_chapters: 0,
  playback_path: "/original.m4b", playback_url: "/api/library/1/audio",
  playback_status: "ready", playback_position_sec: 0, chapters: [],
};
const restored: LibraryBookDetail = {
  ...original, title: "Restored book", author: "Restored author", tags: "restored",
  playback_path: "/restored.m4b", playback_position_sec: 7,
};

function createClient() {
  let current = original;
  let generation = "backend:0";
  const restore = deferred<{ restored_id: string; safety_copy_path: string }>();
  const apiClient = {
    getLibrary: vi.fn(async (query: string) => ({
      items: !query || current.title.includes(query) ? [current] : [], count: 1,
    })),
    getLibraryCount: vi.fn(async () => ({ count: 1 })),
    getLibraryBook: vi.fn(async () => current),
    getLibraryAudioUrl: vi.fn(async () => "http://desktop.test/audio"),
    updateLibraryBook: vi.fn(async (_id: number, payload: object) => ({ ...current, ...payload })),
    updateBookPlaybackPosition: vi.fn(async (_id: number, position: number) => ({ position_sec: position })),
    deleteBook: vi.fn(async () => ({ deleted: true })),
    repairConversion: vi.fn(async () => ({ job_id: "repair" })),
    cleanupStorage: vi.fn(async () => ({ deleted_files: 0, deleted_rows: 0 })),
    getMaintenanceDiagnostics: vi.fn(async (): Promise<{ library_generation: string | null }> => ({ library_generation: generation })),
    getDatabaseBackups: vi.fn(async () => ({ items: [] })),
    restoreDatabaseBackup: vi.fn((_id: string, _options?: { signal?: AbortSignal }) => restore.promise),
  };
  return {
    apiClient, restore,
    commitRestore: () => { current = restored; generation = "backend:1"; },
    finishRestore: () => {
      current = restored;
      generation = "backend:1";
      restore.resolve({ restored_id: "saved.db", safety_copy_path: "safety.db" });
    },
  };
}

let probe: {
  library: ReturnType<typeof useLibrary>;
  editor: ReturnType<typeof useLibraryPanelState>;
  player: ReturnType<typeof useAudioPlayer>;
  playback: ReturnType<typeof usePlaybackPersistence>;
};
function Probe({ apiClient, backendDisconnected = false }: { apiClient: ApiClient | null; backendDisconnected?: boolean }) {
  const library = useLibrary({ apiClient, backendDisconnected });
  const editor = useLibraryPanelState({ library });
  const player = useAudioPlayer({ apiClient, library });
  const playback = usePlaybackPersistence({
    apiClient, library, audioRef: player.audioRef, loadedBookId: null,
  });
  probe = { library, editor, player, playback };
  return <audio ref={player.audioRef} />;
}

describe("library backup restore", () => {
  let root: Root;
  let container: HTMLDivElement;

  beforeEach(() => {
    vi.useFakeTimers();
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    window.localStorage.clear();
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
    vi.spyOn(HTMLMediaElement.prototype, "readyState", "get").mockReturnValue(1);
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    window.localStorage.clear();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("releases old audio and replaces drafts when restored books reuse ids", async () => {
    const client = createClient();
    await act(async () => root.render(<Probe apiClient={client.apiClient as unknown as ApiClient} />));
    await act(async () => probe.player.togglePlay());
    const audio = probe.player.audioRef.current!;
    audio.currentTime = 42;
    await act(async () => probe.editor.setEditTitle("Unsaved original title"));
    const oldSave = probe.editor.saveMetadata;
    const oldEvents = probe.player.audioProps;
    let pending!: Promise<void>;
    await act(async () => { pending = probe.library.restoreBackup!("saved.db"); });
    await act(async () => { client.finishRestore(); await pending; });

    expect(probe.player.audioProps.src).toBeNull();
    expect(audio.getAttribute("src")).toBeNull();
    expect(probe.editor.editTitle).toBe(restored.title);
    expect(probe.editor.editAuthor).toBe(restored.author);
    expect(probe.editor.editTags).toBe(restored.tags);
    client.apiClient.updateBookPlaybackPosition.mockClear();
    await act(async () => {
      oldEvents.onPause();
      oldEvents.onEnded();
      probe.player.audioProps.onPause();
      window.dispatchEvent(new Event("beforeunload"));
      await oldSave();
    });
    expect(client.apiClient.updateBookPlaybackPosition).not.toHaveBeenCalled();
    expect(client.apiClient.updateLibraryBook).not.toHaveBeenCalled();
    expect(probe.library.selectedBookDetail?.playback_position_sec).toBe(7);
    await act(async () => probe.editor.saveMetadata());
    expect(client.apiClient.updateLibraryBook).toHaveBeenCalledWith(1, {
      title: restored.title, author: restored.author, tags: restored.tags,
    }, undefined);
  });

  it("keeps restore busy through search changes and reloads the latest query", async () => {
    const client = createClient();
    await act(async () => root.render(<Probe apiClient={client.apiClient as unknown as ApiClient} />));
    let pending!: Promise<void>;
    await act(async () => { pending = probe.library.restoreBackup!("saved.db"); });
    await act(async () => probe.library.setSearchQuery("Restored"));
    await act(async () => vi.advanceTimersByTimeAsync(220));
    expect(probe.library.maintenanceLoading).toBe(true);
    expect(probe.library.items).toEqual([]);
    await act(async () => { client.finishRestore(); await pending; });
    expect(probe.library.items.map((item) => item.title)).toEqual([restored.title]);
    // The last request is the sidebar's unfiltered page, so look at the searched ones.
    expect(client.apiClient.getLibrary.mock.calls.filter(([search]) => search).at(-1))
      .toEqual(["Restored", 100, { conversionMode: "", offset: 0 }, expect.anything()]);
    expect(probe.library.maintenanceLoading).toBe(false);
    expect(probe.library.maintenanceMessage).toContain("Backup restored");
  });

  it("drains pending metadata and playback writes and blocks mutations during restore", async () => {
    const client = createClient();
    const metadata = deferred<LibraryBookDetail>();
    const playback = deferred<{ position_sec: number }>();
    client.apiClient.updateLibraryBook.mockImplementationOnce(() => metadata.promise);
    client.apiClient.updateBookPlaybackPosition.mockImplementationOnce(() => playback.promise);
    await act(async () => root.render(<Probe apiClient={client.apiClient as unknown as ApiClient} />));
    await act(async () => probe.player.togglePlay());
    probe.player.audioRef.current!.currentTime = 42;
    let save!: Promise<void>;
    await act(async () => {
      save = probe.editor.saveMetadata();
      probe.player.audioProps.onPause();
    });
    expect(client.apiClient.updateBookPlaybackPosition).toHaveBeenCalledTimes(1);
    window.localStorage.setItem("preferences", "keep");
    let pending!: Promise<void>;
    await act(async () => { pending = probe.library.restoreBackup!("saved.db"); });
    expect(client.apiClient.restoreDatabaseBackup).not.toHaveBeenCalled();
    await act(async () => {
      await probe.library.updateSelectedBookMetadata({ title: "Blocked", author: "", tags: "" });
      await probe.library.deleteSelectedBook();
      await probe.library.repairSelectedBook();
      probe.player.audioProps.onPause();
    });
    expect(client.apiClient.updateLibraryBook).toHaveBeenCalledTimes(1);
    expect(client.apiClient.updateBookPlaybackPosition).toHaveBeenCalledTimes(1);
    expect(client.apiClient.deleteBook).not.toHaveBeenCalled();
    expect(client.apiClient.repairConversion).not.toHaveBeenCalled();
    await act(async () => { metadata.resolve(original); await save; });
    expect(client.apiClient.restoreDatabaseBackup).not.toHaveBeenCalled();
    await act(async () => { playback.resolve({ position_sec: 42 }); });
    expect(client.apiClient.restoreDatabaseBackup).toHaveBeenCalledWith("saved.db", expect.objectContaining({ timeoutMs: 600_000 }));
    await act(async () => { client.finishRestore(); await pending; });
    expect(probe.library.selectedBookDetail?.playback_position_sec).toBe(7);
    await act(async () => probe.player.togglePlay());
    expect(probe.player.audioRef.current!.currentTime).toBe(7);
    expect(window.localStorage.getItem("preferences")).toBe("keep");
  });

  it("discards pending detail and audio responses from before a restore", async () => {
    const client = createClient();
    const audioUrl = deferred<string>();
    const oldDetail = deferred<LibraryBookDetail>();
    await act(async () => root.render(<Probe apiClient={client.apiClient as unknown as ApiClient} />));
    client.apiClient.getLibraryAudioUrl.mockImplementationOnce(() => audioUrl.promise);
    client.apiClient.getLibraryBook.mockImplementationOnce(() => oldDetail.promise);
    let loadingAudio!: Promise<void>;
    let loadingDetail!: Promise<LibraryBookDetail | null>;
    let pending!: Promise<void>;
    await act(async () => {
      loadingAudio = probe.player.togglePlay();
      loadingDetail = probe.library.selectBook(0);
      pending = probe.library.restoreBackup!("saved.db");
    });
    await act(async () => { client.finishRestore(); await pending; });
    await act(async () => {
      oldDetail.resolve(original);
      audioUrl.resolve("http://desktop.test/original");
      await loadingAudio;
      await loadingDetail;
    });
    expect(probe.library.selectedBookDetail?.title).toBe(restored.title);
    expect(probe.editor.editTitle).toBe(restored.title);
    expect(probe.player.audioProps.src).toBeNull();
    expect(probe.player.audioRef.current!.getAttribute("src")).toBeNull();
  });

  it("retains audio and drafts and resumes saves when restore fails", async () => {
    const client = createClient();
    await act(async () => root.render(<Probe apiClient={client.apiClient as unknown as ApiClient} />));
    await act(async () => probe.player.togglePlay());
    await act(async () => probe.editor.setEditTitle("Unsaved original title"));
    const src = probe.player.audioProps.src;
    const save = probe.editor.saveMetadata;
    let pending!: Promise<void>;
    await act(async () => { pending = probe.library.restoreBackup!("saved.db"); });
    await act(async () => { client.restore.reject(new Error("restore failed")); await pending; });
    expect(probe.player.audioProps.src).toBe(src);
    expect(probe.editor.editTitle).toBe("Unsaved original title");
    expect(probe.library.maintenanceLoading).toBe(false);
    expect(probe.library.maintenanceMessage).toBe("restore failed");
    await act(async () => {
      await save();
      probe.player.audioProps.onPause();
    });
    expect(client.apiClient.updateLibraryBook).toHaveBeenCalledTimes(1);
    expect(client.apiClient.updateBookPlaybackPosition).toHaveBeenCalledTimes(1);
  });

  it.each(["network", "timeout", "http"] as const)("invalidates old saves and playback after a committed restore loses its %s response", async (code) => {
    const client = createClient();
    await act(async () => root.render(<Probe apiClient={client.apiClient as unknown as ApiClient} />));
    await act(async () => probe.player.togglePlay());
    probe.player.audioRef.current!.currentTime = 42;
    await act(async () => probe.editor.setEditTitle("Unsaved original title"));
    const oldSave = probe.editor.saveMetadata;
    const oldEvents = probe.player.audioProps;
    let pending!: Promise<void>;
    await act(async () => { pending = probe.library.restoreBackup!("saved.db"); });
    client.commitRestore();
    await act(async () => {
      client.restore.reject(new ApiError({
        message: "Restore response lost", code, status: code === "http" ? 503 : null,
        detail: "", retriable: true,
      }));
      await pending;
    });
    expect(client.apiClient.getMaintenanceDiagnostics).toHaveBeenCalledTimes(2);
    expect(probe.editor.editTitle).toBe(restored.title);
    expect(probe.player.audioProps.src).toBeNull();
    await act(async () => {
      await oldSave();
      oldEvents.onPause();
      oldEvents.onEnded();
      window.dispatchEvent(new Event("beforeunload"));
    });
    expect(client.apiClient.updateLibraryBook).not.toHaveBeenCalled();
    expect(client.apiClient.updateBookPlaybackPosition).not.toHaveBeenCalled();
    await act(async () => probe.editor.saveMetadata());
    expect(client.apiClient.updateLibraryBook).toHaveBeenCalledWith(1, {
      title: restored.title, author: restored.author, tags: restored.tags,
    }, undefined);
  });

  it("retains drafts and resumes saves after a rejected restore without another diagnostics request", async () => {
    const client = createClient();
    await act(async () => root.render(<Probe apiClient={client.apiClient as unknown as ApiClient} />));
    await act(async () => probe.editor.setEditTitle("Unsaved original title"));
    const oldSave = probe.editor.saveMetadata;
    let pending!: Promise<void>;
    await act(async () => { pending = probe.library.restoreBackup!("saved.db"); });
    client.apiClient.getMaintenanceDiagnostics.mockRejectedValue(new Error("Backend unavailable"));
    await act(async () => {
      client.restore.reject(new ApiError({
        message: "Restore is blocked", code: "http", status: 409, detail: "", retriable: false,
      }));
      await pending;
    });
    expect(client.apiClient.getMaintenanceDiagnostics).toHaveBeenCalledTimes(1);
    expect(probe.editor.editTitle).toBe("Unsaved original title");
    await act(async () => { await oldSave(); });
    expect(client.apiClient.updateLibraryBook).toHaveBeenCalledWith(1, {
      title: "Unsaved original title", author: original.author, tags: original.tags,
    }, undefined);
  });

  it("pauses writes until diagnostics can verify an uncertain restore", async () => {
    const client = createClient();
    await act(async () => root.render(<Probe apiClient={client.apiClient as unknown as ApiClient} />));
    await act(async () => probe.player.togglePlay());
    const oldSave = probe.editor.saveMetadata;
    const verification = deferred<{ library_generation: string }>();
    client.apiClient.getMaintenanceDiagnostics.mockResolvedValueOnce({ library_generation: "backend:0" });
    client.apiClient.getMaintenanceDiagnostics.mockImplementationOnce(() => verification.promise);
    let pending!: Promise<void>;
    await act(async () => { pending = probe.library.restoreBackup!("saved.db"); });
    await act(async () => { client.restore.reject(new Error("Restore response lost")); });
    await act(async () => {
      await oldSave();
      probe.player.audioProps.onPause();
    });
    expect(client.apiClient.updateLibraryBook).not.toHaveBeenCalled();
    expect(client.apiClient.updateBookPlaybackPosition).not.toHaveBeenCalled();
    client.commitRestore();
    await act(async () => { verification.reject(new Error("Backend unavailable")); await pending; });
    expect(probe.library.maintenanceMessage).toContain("Library changes are paused");
    await act(async () => probe.library.updateSelectedBookMetadata({ title: "Blocked", author: "", tags: "" }));
    expect(client.apiClient.updateLibraryBook).not.toHaveBeenCalled();
    await act(async () => probe.library.loadMaintenanceDiagnostics());
    expect(probe.editor.editTitle).toBe(restored.title);
    await act(async () => { await oldSave(); });
    expect(client.apiClient.updateLibraryBook).not.toHaveBeenCalled();
    await act(async () => probe.editor.saveMetadata());
    expect(client.apiClient.updateLibraryBook).toHaveBeenCalledWith(1, {
      title: restored.title, author: restored.author, tags: restored.tags,
    }, undefined);
  });

  it("reports paused writes instead of running cleanup, metadata saves or repairs after an unverified restore", async () => {
    const client = createClient();
    await act(async () => root.render(<Probe apiClient={client.apiClient as unknown as ApiClient} />));
    client.apiClient.getMaintenanceDiagnostics.mockResolvedValueOnce({ library_generation: "backend:0" });
    client.apiClient.getMaintenanceDiagnostics.mockRejectedValueOnce(new Error("Backend unavailable"));
    let pending!: Promise<void>;
    await act(async () => { pending = probe.library.restoreBackup!("saved.db"); });
    await act(async () => { client.restore.reject(new Error("Restore response lost")); await pending; });
    expect(probe.library.maintenanceMessage).toContain("Library changes are paused");
    await act(async () => probe.library.cleanupStorage!(["/out/Old"]));
    expect(client.apiClient.cleanupStorage).not.toHaveBeenCalled();
    expect(probe.library.maintenanceMessage).toContain("Library changes are paused");
    await act(async () => probe.library.updateSelectedBookMetadata({ title: "Blocked", author: "", tags: "" }));
    expect(client.apiClient.updateLibraryBook).not.toHaveBeenCalled();
    expect(probe.library.statusMessage).toContain("paused");
    await act(async () => probe.library.repairSelectedBook());
    expect(client.apiClient.repairConversion).not.toHaveBeenCalled();
    expect(probe.library.statusMessage).toContain("paused");
  });

  it("keeps writes paused across a disconnect during restore until the reconnected library is verified", async () => {
    const client = createClient();
    const apiClient = client.apiClient as unknown as ApiClient;
    await act(async () => root.render(<Probe apiClient={apiClient} />));
    const oldSave = probe.editor.saveMetadata;
    let pending!: Promise<void>;
    await act(async () => { pending = probe.library.restoreBackup!("saved.db"); });
    await act(async () => root.render(<Probe apiClient={apiClient} backendDisconnected />));
    client.commitRestore();
    await act(async () => { client.restore.reject(new Error("Connection lost")); await pending; });
    await act(async () => { await oldSave(); });
    expect(client.apiClient.updateLibraryBook).not.toHaveBeenCalled();
    await act(async () => root.render(<Probe apiClient={apiClient} />));
    await act(async () => probe.library.updateSelectedBookMetadata({ title: "Blocked", author: "", tags: "" }));
    expect(client.apiClient.updateLibraryBook).not.toHaveBeenCalled();
    await act(async () => probe.library.loadMaintenanceDiagnostics());
    expect(probe.editor.editTitle).toBe(restored.title);
    await act(async () => { await oldSave(); });
    expect(client.apiClient.updateLibraryBook).not.toHaveBeenCalled();
    await act(async () => probe.editor.saveMetadata());
    expect(client.apiClient.updateLibraryBook).toHaveBeenCalledWith(1, {
      title: restored.title, author: restored.author, tags: restored.tags,
    }, undefined);
  });

  it.each(["pending", "uncertain"] as const)("keeps metadata and playback writes paused after client replacement while restore is %s", async (outcome) => {
    const clientA = createClient();
    const clientB = createClient();
    await act(async () => root.render(<Probe apiClient={clientA.apiClient as unknown as ApiClient} />));
    await act(async () => probe.player.togglePlay());
    const oldSave = probe.editor.saveMetadata;
    const oldEvents = probe.player.audioProps;
    const oldPlayback = probe.playback.persistBookPlaybackPosition;
    let pending!: Promise<void>;
    await act(async () => { pending = probe.library.restoreBackup!("saved.db"); });
    if (outcome === "uncertain") {
      clientA.apiClient.getMaintenanceDiagnostics.mockRejectedValue(new Error("Backend unavailable"));
      await act(async () => { clientA.restore.reject(new Error("Connection lost")); await pending; });
    }
    await act(async () => root.render(<Probe apiClient={null} backendDisconnected />));
    clientB.commitRestore();
    await act(async () => root.render(<Probe apiClient={clientB.apiClient as unknown as ApiClient} />));
    expect(probe.library.maintenanceMessage).toContain("Library changes are paused");
    expect(probe.library.selectedBookDetail?.title).toBe(restored.title);
    const write = async () => {
      await probe.library.updateSelectedBookMetadata({ title: "Blocked", author: "", tags: "" });
      await probe.playback.persistBookPlaybackPosition(1, 42, { force: true });
    };
    await act(write);
    expect(clientB.apiClient.updateLibraryBook).not.toHaveBeenCalled();
    expect(clientB.apiClient.updateBookPlaybackPosition).not.toHaveBeenCalled();
    if (outcome === "pending") {
      await act(async () => { clientA.restore.reject(new Error("Connection lost")); await pending; });
    }
    clientB.apiClient.getMaintenanceDiagnostics.mockResolvedValueOnce({ library_generation: null });
    await act(async () => probe.library.loadMaintenanceDiagnostics());
    expect(probe.library.maintenanceMessage).toBe("Library generation unavailable. Writes are paused.");
    await act(write);
    expect(clientB.apiClient.updateLibraryBook).not.toHaveBeenCalled();
    expect(clientB.apiClient.updateBookPlaybackPosition).not.toHaveBeenCalled();
    const verification = deferred<{ library_generation: string }>();
    clientB.apiClient.getMaintenanceDiagnostics.mockImplementationOnce(() => verification.promise);
    let diagnostics!: Promise<void>;
    await act(async () => { diagnostics = probe.library.loadMaintenanceDiagnostics(); });
    await act(write);
    expect(clientB.apiClient.updateLibraryBook).not.toHaveBeenCalled();
    expect(clientB.apiClient.updateBookPlaybackPosition).not.toHaveBeenCalled();
    await act(async () => { verification.resolve({ library_generation: "backend:1" }); await diagnostics; });
    await act(async () => {
      await oldSave();
      await oldPlayback(1, 42, { force: true });
      oldEvents.onPause();
      oldEvents.onEnded();
    });
    expect(clientA.apiClient.updateLibraryBook).not.toHaveBeenCalled();
    expect(clientA.apiClient.updateBookPlaybackPosition).not.toHaveBeenCalled();
    await act(async () => {
      await probe.editor.saveMetadata();
      await probe.playback.persistBookPlaybackPosition(1, 11, { force: true });
    });
    expect(clientB.apiClient.updateLibraryBook).toHaveBeenCalledWith(1, {
      title: restored.title, author: restored.author, tags: restored.tags,
    }, undefined);
    expect(clientB.apiClient.updateBookPlaybackPosition).toHaveBeenCalledWith(1, 11, { expectedEpoch: undefined, timeoutMs: 15_000 });
  });

  it("invalidates old metadata and playback callbacks when the client is replaced", async () => {
    const clientA = createClient();
    const clientB = createClient();
    await act(async () => root.render(<Probe apiClient={clientA.apiClient as unknown as ApiClient} />));
    const oldSave = probe.editor.saveMetadata;
    const oldPlayback = probe.playback.persistBookPlaybackPosition;
    await act(async () => root.render(<Probe apiClient={null} backendDisconnected />));
    await act(async () => root.render(<Probe apiClient={clientB.apiClient as unknown as ApiClient} />));
    await act(async () => {
      await oldSave();
      await oldPlayback(1, 42, { force: true });
    });
    expect(clientA.apiClient.updateLibraryBook).not.toHaveBeenCalled();
    expect(clientA.apiClient.updateBookPlaybackPosition).not.toHaveBeenCalled();
    await act(async () => probe.editor.saveMetadata());
    expect(clientB.apiClient.updateLibraryBook).toHaveBeenCalledTimes(1);
  });

  it("keeps ownership when maintenance actions are requested during restore", async () => {
    const client = createClient();
    await act(async () => root.render(<Probe apiClient={client.apiClient as unknown as ApiClient} />));
    let pending!: Promise<void>;
    await act(async () => { pending = probe.library.restoreBackup!("saved.db"); });
    await act(async () => {
      await probe.library.loadMaintenanceDiagnostics();
      await probe.library.backupDatabase();
      await probe.library.cleanupStorage();
      await probe.library.restoreBackup!("another.db");
    });
    expect(client.apiClient.restoreDatabaseBackup).toHaveBeenCalledTimes(1);
    expect(client.apiClient.getMaintenanceDiagnostics).toHaveBeenCalledTimes(1);
    expect(probe.library.maintenanceLoading).toBe(true);
    await act(async () => { client.finishRestore(); await pending; });
    expect(probe.library.maintenanceMessage).toContain("Backup restored");
    expect(probe.library.selectedBookDetail?.title).toBe(restored.title);
  });


  it("waits for pending storage cleanup before replacing the database", async () => {
    const client = createClient();
    const cleanup = deferred<{ deleted_files: number; deleted_rows: number }>();
    client.apiClient.cleanupStorage.mockImplementationOnce(() => cleanup.promise);
    await act(async () => root.render(<Probe apiClient={client.apiClient as unknown as ApiClient} />));
    let cleaning!: Promise<void>;
    let pending!: Promise<void>;
    await act(async () => {
      cleaning = probe.library.cleanupStorage();
      pending = probe.library.restoreBackup!("saved.db");
    });
    expect(client.apiClient.restoreDatabaseBackup).not.toHaveBeenCalled();
    await act(async () => { cleanup.resolve({ deleted_files: 0, deleted_rows: 0 }); await cleaning; });
    expect(client.apiClient.restoreDatabaseBackup).toHaveBeenCalledWith("saved.db", expect.objectContaining({ timeoutMs: 600_000 }));
    expect(probe.library.maintenanceLoading).toBe(true);
    await act(async () => { client.finishRestore(); await pending; });
    expect(probe.library.selectedBookDetail?.title).toBe(restored.title);
  });

  it("aborts a stalled restore on disconnect so Check can verify and resume writes after reconnect", async () => {
    const client = createClient();
    let restoreSignal: AbortSignal | undefined;
    client.apiClient.restoreDatabaseBackup.mockImplementationOnce((_id: string, options?: { signal?: AbortSignal }) => {
      restoreSignal = options?.signal;
      return new Promise<{ restored_id: string; safety_copy_path: string }>((_resolve, reject) => {
        restoreSignal?.addEventListener("abort", () => reject(new ApiError({
          message: "request aborted", code: "aborted", status: null, detail: "", retriable: false,
        })));
      });
    });
    const apiClient = client.apiClient as unknown as ApiClient;
    await act(async () => root.render(<Probe apiClient={apiClient} />));
    let pending!: Promise<void>;
    await act(async () => { pending = probe.library.restoreBackup!("saved.db"); });
    expect(restoreSignal?.aborted).toBe(false);
    await act(async () => root.render(<Probe apiClient={apiClient} backendDisconnected />));
    expect(restoreSignal?.aborted).toBe(true);
    await act(async () => { await pending; });
    await act(async () => root.render(<Probe apiClient={apiClient} />));
    expect(probe.library.maintenanceMessage).toContain("Library changes are paused");
    await act(async () => probe.library.updateSelectedBookMetadata({ title: "Blocked", author: "", tags: "" }));
    expect(client.apiClient.updateLibraryBook).not.toHaveBeenCalled();
    client.commitRestore();
    await act(async () => probe.library.loadMaintenanceDiagnostics());
    expect(probe.library.maintenanceMessage).toBe("Diagnostics loaded");
    expect(probe.editor.editTitle).toBe(restored.title);
    await act(async () => probe.editor.saveMetadata());
    expect(client.apiClient.updateLibraryBook).toHaveBeenCalledTimes(1);
  });

  it("resumes writes without verification when the backend disconnects before the restore is sent", async () => {
    const client = createClient();
    const playback = deferred<{ position_sec: number }>();
    client.apiClient.updateBookPlaybackPosition.mockImplementationOnce(() => playback.promise);
    const apiClient = client.apiClient as unknown as ApiClient;
    await act(async () => root.render(<Probe apiClient={apiClient} />));
    await act(async () => probe.player.togglePlay());
    probe.player.audioRef.current!.currentTime = 42;
    await act(async () => probe.player.audioProps.onPause());
    let pending!: Promise<void>;
    await act(async () => { pending = probe.library.restoreBackup!("saved.db"); });
    await act(async () => root.render(<Probe apiClient={apiClient} backendDisconnected />));
    await act(async () => { playback.resolve({ position_sec: 42 }); await pending; });
    await act(async () => root.render(<Probe apiClient={apiClient} />));
    expect(client.apiClient.restoreDatabaseBackup).not.toHaveBeenCalled();
    expect(probe.library.maintenanceMessage).not.toContain("Library changes are paused");
    await act(async () => probe.editor.saveMetadata());
    expect(client.apiClient.updateLibraryBook).toHaveBeenCalledTimes(1);
  });

  it("keeps writes paused when a restore times out before the library generation changes", async () => {
    const client = createClient();
    await act(async () => root.render(<Probe apiClient={client.apiClient as unknown as ApiClient} />));
    let pending!: Promise<void>;
    await act(async () => { pending = probe.library.restoreBackup!("saved.db"); });
    await act(async () => {
      client.restore.reject(new ApiError({
        message: "request timed out", code: "timeout", status: null, detail: "", retriable: true,
      }));
      await pending;
    });
    expect(client.apiClient.getMaintenanceDiagnostics).toHaveBeenCalledTimes(2);
    expect(probe.library.maintenanceMessage).toContain("Library changes are paused");
    expect(probe.library.maintenanceLoading).toBe(false);
    await act(async () => probe.library.updateSelectedBookMetadata({ title: "Blocked", author: "", tags: "" }));
    expect(client.apiClient.updateLibraryBook).not.toHaveBeenCalled();
    client.commitRestore();
    await act(async () => probe.library.loadMaintenanceDiagnostics());
    expect(probe.editor.editTitle).toBe(restored.title);
    await act(async () => probe.editor.saveMetadata());
    expect(client.apiClient.updateLibraryBook).toHaveBeenCalledTimes(1);
  });

});
