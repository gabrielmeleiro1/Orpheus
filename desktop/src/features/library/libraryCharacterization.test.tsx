// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient, type ApiClient } from "../../api/client";
import type { LibraryBookDetail } from "../../types/api";
import { useAudioPlayer } from "../player/useAudioPlayer";
import { useLibrary } from "./useLibrary";
import { useLibraryPanelState } from "./useLibraryPanelState";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

function book(id: number): LibraryBookDetail {
  return {
    library_id: "library", library_epoch: "original", id, title: `Book ${id}`,
    author: "Author", tags: "fiction", favorite: false, source_path: `book-${id}.epub`,
    source_type: "epub", conversion_mode: "audiobook", source_available: true,
    total_chapters: 0, chapters: [], playback_path: `/book-${id}.m4b`,
    playback_url: `/api/library/${id}/audio`, playback_status: "ready", playback_position_sec: 7,
  };
}

const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });

describe("library state across saves and replacement", () => {
  let root: Root;
  let container: HTMLDivElement;
  let library: ReturnType<typeof useLibrary>;
  let editor: ReturnType<typeof useLibraryPanelState>;
  let player: ReturnType<typeof useAudioPlayer>;
  let books: LibraryBookDetail[];
  let writes: Array<{ path: string; body: Record<string, unknown> }>;
  let patch: (body: Record<string, unknown>) => Promise<Response>;
  let restore: () => Promise<Response>;
  let diagnostics: () => Promise<Response>;

  function Probe({ client }: { client: ApiClient | null }) {
    library = useLibrary({ apiClient: client, backendDisconnected: client === null });
    editor = useLibraryPanelState({ library });
    player = useAudioPlayer({ apiClient: client, library });
    return <audio ref={player.audioRef} />;
  }

  const client = () => createApiClient({ baseUrl: "http://library.test", token: "session" });
  const mount = (apiClient: ApiClient | null) => act(async () => root.render(<Probe client={apiClient} />));

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    vi.useFakeTimers();
    window.localStorage.clear();
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    books = [book(1)];
    writes = [];
    patch = async (body) => {
      books[0] = { ...books[0], ...body };
      return json(books[0]);
    };
    restore = async () => json({ restored_id: "saved.db", safety_copy_path: "safety.db" });
    diagnostics = async () => json({ library_generation: books[0].library_epoch });
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
    vi.spyOn(HTMLMediaElement.prototype, "readyState", "get").mockReturnValue(1);
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = new URL(String(input));
      const path = url.pathname;
      if (!init?.method || init.method === "GET") {
        if (path === "/api/library/count") return json({ count: books.length });
        if (path === "/api/library") {
          const offset = Number(url.searchParams.get("offset") ?? 0);
          return json({ library_id: "library", library_epoch: books[0].library_epoch,
            items: books.slice(offset, offset + 100), count: books.length });
        }
        if (/^\/api\/library\/\d+$/.test(path)) return json(books.find((item) => item.id === Number(path.split("/").at(-1))));
        if (path === "/api/maintenance/diagnostics") return diagnostics();
        if (path === "/api/maintenance/backups") return json({ items: [] });
      }
      const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : {};
      writes.push({ path, body });
      if (path === "/api/desktop/query-token") return json({ token: "audio", expires_at: 9999999999 });
      if (init?.method === "PATCH") return patch(body);
      if (path.endsWith("/playback-position")) return json({ position_sec: body.position_sec });
      if (path === "/api/maintenance/backups/saved.db/restore") return restore();
      throw new Error(`Unexpected request: ${init?.method} ${path}`);
    });
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.restoreAllMocks();
    vi.useRealTimers();
    window.localStorage.clear();
  });

  it("keeps edits made during a save and submits them on the next save", async () => {
    const response = deferred<Response>();
    patch = () => response.promise;
    await mount(client());
    await act(async () => {
      editor.setEditTitle("Submitted title");
      editor.setEditAuthor("Submitted author");
      editor.setEditTags("submitted");
    });
    let saving!: Promise<void>;
    await act(async () => { saving = editor.saveMetadata(); });
    expect(library.savingMetadata).toBe(true);
    await act(async () => {
      editor.setEditTitle("Next title");
      editor.setEditAuthor("Next author");
      editor.setEditTags("next");
    });
    await act(async () => {
      response.resolve(json({ ...books[0], title: "Submitted title", author: "Submitted author", tags: "submitted" }));
      await saving;
    });
    expect(library.savingMetadata).toBe(false);
    expect(library.selectedBookDetail?.title).toBe("Submitted title");
    expect([editor.editTitle, editor.editAuthor, editor.editTags]).toEqual(["Next title", "Next author", "next"]);
    patch = async (body) => json({ ...books[0], ...body });
    await act(async () => editor.saveMetadata());
    expect(writes.map(({ body }) => body)).toEqual([
      { title: "Submitted title", author: "Submitted author", tags: "submitted", expected_epoch: "original" },
      { title: "Next title", author: "Next author", tags: "next", expected_epoch: "original" },
    ]);
    expect(library.selectedBookDetail?.title).toBe("Next title");
  });

  it("keeps an off-page selection and its loaded audio across a first-page refresh", async () => {
    books = Array.from({ length: 101 }, (_, index) => book(index + 1));
    await mount(client());
    await act(async () => library.loadMore());
    await act(async () => library.selectBook(100));
    await act(async () => player.togglePlay());
    const audio = container.querySelector("audio")!;
    const source = audio.getAttribute("src");
    expect(source).toContain("/101/audio");
    audio.currentTime = 42;
    await act(async () => library.refreshLibrary());
    expect(library.items).toHaveLength(100);
    expect(library.items.some((item) => item.id === 101)).toBe(false);
    expect(library.selectedBookId).toBe(101);
    expect(library.selectedBookDetail?.id).toBe(101);
    expect(library.selectedBookPlayable).toBe(true);
    expect(audio.getAttribute("src")).toBe(source);
    expect(audio.currentTime).toBe(42);
    await act(async () => player.audioProps.onPause());
    expect(writes.at(-1)).toEqual({ path: "/api/library/101/playback-position",
      body: { position_sec: 42, expected_epoch: "original" } });
  });

  it("reloads replaced books after a metadata conflict without replaying the draft", async () => {
    await mount(client());
    await act(async () => player.togglePlay());
    await act(async () => editor.setEditTitle("Old library draft"));
    patch = async () => {
      books = [{ ...book(1), library_epoch: "restored", title: "Restored title" }];
      return json({ detail: { code: "library_replaced", message: "Library was replaced" } }, 409);
    };
    await act(async () => editor.saveMetadata());
    expect(writes.filter(({ path }) => path === "/api/library/1")).toEqual([
      { path: "/api/library/1", body: { title: "Old library draft", author: "Author", tags: "fiction", expected_epoch: "original" } },
    ]);
    expect(library.selectedBookDetail?.library_epoch).toBe("restored");
    expect(editor.editTitle).toBe("Restored title");
    expect(container.querySelector("audio")!.getAttribute("src")).toBeNull();
    expect(library.savingMetadata).toBe(false);
  });

  it("pauses writes after a lost restore response until the replacement client verifies the library", async () => {
    const response = deferred<Response>();
    restore = () => response.promise;
    await mount(client());
    await act(async () => player.togglePlay());
    const oldSave = editor.saveMetadata;
    let restoring!: Promise<void>;
    await act(async () => { restoring = library.restoreBackup!("saved.db"); });
    expect(library.maintenanceLoading).toBe(true);
    expect(writes.at(-1)?.path).toBe("/api/maintenance/backups/saved.db/restore");
    diagnostics = async () => { throw new TypeError("Backend unavailable"); };
    await act(async () => { response.reject(new TypeError("Restore response lost")); await restoring; });
    expect(library.maintenanceMessage).toContain("Library changes are paused");
    expect(container.querySelector("audio")!.getAttribute("src")).toBeNull();
    await mount(null);
    books = [{ ...book(1), library_epoch: "restored", title: "Restored title" }];
    await mount(client());
    await act(async () => editor.saveMetadata());
    expect(writes.filter(({ path }) => path === "/api/library/1")).toHaveLength(0);
    expect(library.maintenanceMessage).toContain("Library changes are paused");
    diagnostics = async () => json({ library_generation: "restored" });
    await act(async () => library.loadMaintenanceDiagnostics());
    expect(editor.editTitle).toBe("Restored title");
    expect(library.maintenanceMessage).toBe("Diagnostics loaded");
    await act(async () => oldSave());
    expect(writes.filter(({ path }) => path === "/api/library/1")).toHaveLength(0);
    await act(async () => editor.saveMetadata());
    expect(writes.at(-1)?.body).toEqual({ title: "Restored title", author: "Author", tags: "fiction", expected_epoch: "restored" });
  });
});
