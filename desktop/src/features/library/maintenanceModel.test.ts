import { describe, expect, it } from "vitest";

import type { CleanupSummary, MaintenanceDiagnosticsResponse } from "../../types/library";
import { backupDisplayName, checkLibrarySummary, cleanupConfirmation, cleanupResultMessage, messageParts, restoreResultMessage } from "./maintenanceModel";

const preview = (overrides: Partial<CleanupSummary> = {}): CleanupSummary => ({
  changed: null, grace_hours: 24,
  removable: { output_folders: 0, temp_uploads: 2, temp_stage_folders: 1, pending_deletions: 0, orphan_sources: 0 },
  kept: [{ category: "unowned_output_folder", reason: "contains_finished_audio", items: [{ name: "Old Book", path: "/out/Old Book" }] }],
  ...overrides,
});
const report = (extra: Partial<MaintenanceDiagnosticsResponse> = {}) => ({
  database_path: "db", output_path: "out", temp_path: "temp", app_data_path: "data", audit: {}, ...extra,
}) as MaintenanceDiagnosticsResponse;

describe("library maintenance copy", () => {
  it("never reports a check result before a check has run", () => {
    expect(checkLibrarySummary(null)).toBe("Not checked yet");
  });

  it("says what cleanup would remove and what it keeps and why", () => {
    const text = checkLibrarySummary(report({ cleanup_preview: preview(), checked_at: "2026-11-16T19:46:00Z" }));
    expect(text).toMatch(/^Checked /);
    expect(text).toContain("Clean up would remove 2 old uploads and 1 temporary work folder.");
    expect(text).toContain("1 folder not in the library kept because it contains finished audio: Old Book");
  });

  it("agrees in number with the folders it keeps", () => {
    const kept = (reason: string, count: number) => messageParts(checkLibrarySummary(report({ cleanup_preview: preview({ kept: [{
      category: "unowned_output_folder", reason, items: [{ name: "A", path: "/out/A" }, { name: "B", path: "/out/B" }].slice(0, count),
    }] as CleanupSummary["kept"] }) }))).items;
    expect(kept("contains_finished_audio", 2)).toEqual(["2 folders not in the library kept because they contain finished audio: A and B"]);
    expect(kept("interrupted_conversion", 1)).toEqual(["1 folder not in the library kept because it holds an interrupted conversion: A"]);
    expect(kept("interrupted_conversion", 2)).toEqual(["2 folders not in the library kept because they hold an interrupted conversion: A and B"]);
    expect(kept("recently_modified", 1)).toEqual(["1 folder not in the library kept because it changed recently: A"]);
  });

  it("names the books whose library entry a cleanup removes: in the check, the confirmation and the result", () => {
    const nothing = { output_folders: 0, temp_uploads: 0, temp_stage_folders: 0, pending_deletions: 0, orphan_sources: 0 };
    const harbor = { id: 2, title: "Harbor Lights" };
    const one = report({ cleanup_preview: preview({ removable: nothing, kept: [], removable_books: [harbor] }) });
    expect(checkLibrarySummary(one)).toBe("A cleanup would remove 1 book with no audio and no source file: Harbor Lights.");
    expect(checkLibrarySummary(report({ cleanup_preview: preview({ kept: [], removable_books: [harbor, { id: 3, title: "Salt Road" }] }) })))
      .toBe("A cleanup would remove 2 books with no audio and no source file: Harbor Lights and Salt Road. It would also remove 2 old uploads and 1 temporary work folder.");

    expect(cleanupConfirmation(one))
      .toBe("Clean stale storage records and orphan files? This removes the library entry of 1 book with no audio and no source file: Harbor Lights. A database backup is saved first.");
    // The confirmation names every book, however many there are.
    const many = ["One", "Two", "Three", "Four"].map((title, id) => ({ id, title }));
    expect(cleanupConfirmation(report({ cleanup_preview: preview({ removable_books: many }) })))
      .toContain("This removes the library entries of 4 books with no audio and no source file: One, Two, Three and Four.");
    expect(cleanupConfirmation(report({ cleanup_preview: preview() })))
      .toBe("Clean stale storage records and orphan files? A database backup is saved first if anything changes.");
    expect(cleanupConfirmation(null)).toBe(cleanupConfirmation(report()));

    // The book's row and its three chapter rows are one named removal, not four items.
    expect(cleanupResultMessage({ backup_path: "/b.db", summary: preview({ changed: true, kept: [], removable_books: [],
      removed_books: [harbor], removed: { book_rows: 1, chapter_rows: 3, temp_uploads: 0 } }) }))
      .toBe("Removed 1 book with no audio and no source file: Harbor Lights. A database backup was saved first.");
    expect(cleanupResultMessage({ backup_path: "/b.db", summary: preview({ changed: true, kept: [], removable_books: [],
      removed_books: [harbor], removed: { book_rows: 1, chapter_rows: 3, temp_uploads: 2 } }) }))
      .toBe("Removed 1 book with no audio and no source file: Harbor Lights. Cleaned up 2 items. A database backup was saved first.");
    // Removing the folders that held an entry in place says what the next cleanup does to it.
    expect(cleanupResultMessage({ backup_path: null, removed_unowned_folders: ["Old Book"], summary: preview({ changed: true, kept: [],
      removed_books: [], removable_books: [harbor], removed: { unowned_folders: 1 } }) }))
      .toBe("Removed 1 folder not in the library: Old Book. The next cleanup would remove 1 book with no audio and no source file: Harbor Lights.");
  });

  it("states the outcome of a cleanup, including when there was nothing to do", () => {
    expect(cleanupResultMessage({ summary: preview({ kept: [], removed: { temp_uploads: 2, temp_stage_folders: 1 } }), backup_path: "/b.db" }))
      .toBe("Cleaned up 3 items. A database backup was saved first.");
    expect(cleanupResultMessage({ summary: preview({ kept: [], removed: {} }), backup_path: null })).toBe("Nothing needed cleaning up.");
  });

  it("counts only what a cleanup deleted and says what else it did", () => {
    const reconciled = { book_rows: 0, chapter_rows: 0, output_folders: 0, temp_uploads: 0, moved_sources: 0, reconciled_books: 1 };
    expect(cleanupResultMessage({ summary: preview({ changed: true, kept: [], removed: reconciled }), backup_path: "/b.db" }))
      .toBe("Reconnected 1 book to audio found on disk. A database backup was saved first.");
    expect(cleanupResultMessage({ summary: preview({ changed: true, kept: [], removed: { ...reconciled, temp_uploads: 2, moved_sources: 1 } }), backup_path: "/b.db" }))
      .toBe("Cleaned up 2 items. Reconnected 1 book to audio found on disk. Moved 1 source file into the library's storage. A database backup was saved first.");
  });

  it("puts each kept item on its own line and never advises a repair the missing source rules out", () => {
    const harbor = { id: 1, title: "Harbor Lights" };
    const tide = { id: 5, title: "Long Tide" };
    const text = checkLibrarySummary(report({ cleanup_preview: preview({
      removable: { output_folders: 0, temp_uploads: 0, temp_stage_folders: 0, pending_deletions: 0, orphan_sources: 0 },
      kept: [
        { category: "missing_source", reason: "needs_user_action", items: [harbor, { id: 2, title: "Quiet Rivers" }] },
        { category: "missing_chapter_audio", reason: "book_kept", items: [{ ...harbor, chapters: 3 }, { ...tide, chapters: 19 }] },
        { category: "book_without_audio", reason: "unowned_audio_present", items: [{ id: 3, title: "Salt Road" }] },
        { category: "book_without_audio", reason: "other_audio_present", items: [harbor] },
        { category: "unowned_output_folder", reason: "contains_finished_audio", items: [{ name: "Salt Road (2)", path: "/out/Salt Road (2)" }] },
      ] as CleanupSummary["kept"],
    }) }));
    expect(messageParts(text)).toEqual({ text: "Nothing needs cleaning up. Kept:", items: [
      "Quiet Rivers: source file missing, so Repair audio is unavailable for it",
      "Long Tide: chapter audio missing, book kept (Repair audio rebuilds it)",
      "Harbor Lights: chapter audio and source file missing, so the audio cannot be rebuilt (delete the book, or create it again from the original file)",
      "Salt Road: no audio in the library, but a folder with its name holds audio",
      "Harbor Lights: no audio; kept in the library while folders not in the library hold audio",
      "1 folder not in the library kept because it contains finished audio: Salt Road (2)",
    ] });
  });

  it("names the folders a cleanup removed on request and counts each removal once", () => {
    expect(cleanupResultMessage({
      summary: preview({ changed: true, kept: [], removed: { temp_uploads: 1, unowned_folders: 2 } }), backup_path: "/b.db",
      removed_unowned_folders: ["Old Book", "Orchard"], unknown_unowned_folders: ["Gone"],
    })).toBe("Removed 2 folders not in the library: Old Book and Orchard. 1 folder was already gone: Gone. Cleaned up 1 item. A database backup was saved first.");
    expect(cleanupResultMessage({
      summary: preview({ changed: true, kept: [], removed: { unowned_folders: 1 } }), backup_path: null, removed_unowned_folders: ["Old Book"],
    })).toBe("Removed 1 folder not in the library: Old Book.");
  });

  it("describes what a restore brought back and what is missing", () => {
    expect(restoreResultMessage({ restored_id: "b", safety_copy_path: "/safe.db", summary: {
      books_restored: 12, missing_audio: [{ id: 1, title: "Lantern" }], missing_source: [],
      unowned_output_folders: [{ name: "Orchard", path: "/out/Orchard", has_audio: true, has_recovery_inventory: false }],
    } })).toBe("Backup restored with 12 books. 1 book has no audio: Lantern. 1 folder on disk is not in this library: Orchard. A safety copy of the previous library was saved.");
  });

  it("names backups by their label", () => {
    expect(backupDisplayName({ label: "Backup from 2026-11-16 19:46 · 2 books", filename: "library_1.db", created_at: "", book_count: 2 }))
      .toBe("Backup from 2026-11-16 19:46 · 2 books");
  });
});
