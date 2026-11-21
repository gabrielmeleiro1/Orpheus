// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import { useLibrary, type UseLibraryResult } from "./useLibrary";

afterEach(() => vi.unstubAllGlobals());

it("keeps the Back up and Clean up results on screen after the diagnostics refresh", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const apiClient = {
    getLibrary: vi.fn(async () => ({ items: [], count: 0, library_id: "library", library_epoch: "epoch" })),
    getLibraryCount: vi.fn(async () => ({ count: 0 })),
    getMaintenanceDiagnostics: vi.fn(async () => ({ database_path: "db", output_path: "out", temp_path: "temp",
      app_data_path: "data", audit: {}, library_id: "library", library_epoch: "epoch" })),
    getDatabaseBackups: vi.fn(async () => ({ items: [] as unknown[] })),
    backupDatabase: vi.fn(async () => ({ backup_path: "c:\\users\\reader\\data\\backups\\library_1.db" })),
    cleanupStorage: vi.fn(async () => ({ backup_path: null, summary: { changed: false, grace_hours: 24,
      removable: { output_folders: 0, temp_uploads: 0, temp_stage_folders: 0, pending_deletions: 0, orphan_sources: 0 },
      kept: [], removed: {} } })),
  } as unknown as ApiClient;
  let library!: UseLibraryResult;
  function Probe() {
    library = useLibrary({ apiClient });
    return null;
  }
  const root = createRoot(document.createElement("div"));
  try {
    await act(async () => root.render(<Probe />));
    await act(async () => { await library.backupDatabase(); });
    // Until the list knows the backup, its file name stands in; never the raw path.
    expect(library.maintenanceMessage).toBe("Backup saved: library_1.db");
    vi.mocked(apiClient.getDatabaseBackups).mockResolvedValue({ items: [{ id: "Library_1.db", filename: "Library_1.db",
      created_at: "2026-11-17T04:57:08Z", size: 1, book_count: 6, label: "Backup from 2026-11-17 04:57 · 6 books" }] });
    await act(async () => { await library.backupDatabase(); });
    expect(library.maintenanceMessage).toBe("Backup saved: Backup from 2026-11-17 04:57 · 6 books");
    vi.mocked(apiClient.getDatabaseBackups).mockResolvedValue({ items: [] });
    await act(async () => { await library.cleanupStorage(); });
    expect(library.maintenanceMessage).toBe("Nothing needed cleaning up.");
    // A plain cleanup never names folders; removal is passed through only when asked for.
    expect(apiClient.cleanupStorage).toHaveBeenLastCalledWith(undefined);
    vi.mocked(apiClient.cleanupStorage).mockResolvedValueOnce({ backup_path: "/b.db", removed_unowned_folders: ["Old Book"], summary: {
      changed: true, grace_hours: 24, kept: [], removed: { unowned_folders: 1 },
      removable: { output_folders: 0, temp_uploads: 0, temp_stage_folders: 0, pending_deletions: 0, orphan_sources: 0 } } });
    await act(async () => { await library.cleanupStorage(["Old Book"]); });
    expect(apiClient.cleanupStorage).toHaveBeenLastCalledWith({ removeUnownedFolders: ["Old Book"] });
    expect(library.maintenanceMessage).toBe("Removed 1 folder not in the library: Old Book. A database backup was saved first.");
  } finally {
    await act(async () => root.unmount());
  }
});
