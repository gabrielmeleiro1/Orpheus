// @vitest-environment happy-dom

import { act, useState } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LibraryMaintenanceSection } from "./LibraryMaintenanceSection";
import type { UseLibraryResult } from "./useLibrary";

describe("LibraryMaintenanceSection", () => {
  let container: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;
  const restoreBackup = vi.fn(async () => undefined);
  const loadMaintenanceDiagnostics = vi.fn(async () => undefined);
  const cleanupStorage = vi.fn(async () => undefined);
  const model = () => ({
    selectedBookId: null,
    maintenanceLoading: false,
    maintenanceMessage: "",
    maintenanceBackups: [{ id: "library_saved.db", filename: "library_saved.db", created_at: "2026-09-01T12:00:00Z", size: 4096, book_count: 2 }],
    maintenanceReport: {
      database_path: "library.db", output_path: "output", temp_path: "temp", app_data_path: "data", audit: {},
      quarantine: { path: "library.db.corrupt", created_at: "2026-09-30T12:00:00Z" },
      cleanup_preview: {
        changed: null, grace_hours: 24,
        removable: { output_folders: 0, temp_uploads: 0, temp_stage_folders: 0, pending_deletions: 0, orphan_sources: 0 },
        kept: [],
      },
    },
    loadMaintenanceDiagnostics,
    restoreBackup,
    cleanupStorage,
  }) as unknown as UseLibraryResult;
  const withUnownedFolders = () => ({ ...model(), maintenanceReport: { ...model().maintenanceReport!, quarantine: null, cleanup_preview: {
    changed: null, grace_hours: 24,
    removable: { output_folders: 0, temp_uploads: 0, temp_stage_folders: 0, pending_deletions: 0, orphan_sources: 0 },
    kept: [
      { category: "unowned_output_folder", reason: "contains_finished_audio", items: [{ name: "Old Book", path: "/out/Old Book" }] },
      { category: "unowned_output_folder", reason: "recently_modified", items: [{ name: "Orchard", path: "/out/Orchard" }] },
    ],
  } } }) as unknown as UseLibraryResult;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    vi.clearAllMocks();
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  it("loads findings on open and shows the quarantined database, the actions and available backups", async () => {
    await act(async () => root.render(<LibraryMaintenanceSection library={model()} selectedDetailId={null} />));
    expect(loadMaintenanceDiagnostics).toHaveBeenCalledOnce();
    expect([...container.querySelectorAll(".maintenance-actions .settings-row > .field-label")].map((label) => label.textContent))
      .toEqual(["Database", "Check library", "Back up", "Clean up"]);
    expect(container.querySelector(".maintenance-actions .settings-path")?.getAttribute("data-tip")).toBe("library.db");
    expect(container.querySelector(".maintenance-actions")?.textContent).toContain("Nothing needs cleaning up.");
    expect(container.querySelector(".maintenance-quarantine")?.textContent).toContain("library.db.corrupt");
    // People see when a backup was made, not its file name.
    expect(container.textContent).toContain("Backup from");
    expect(container.textContent).not.toContain("library_saved.db");
    expect(container.textContent).toContain("2 books");
    await act(async () => [...container.querySelectorAll("button")].find((button) => button.textContent === "Check")!.click());
    expect(loadMaintenanceDiagnostics).toHaveBeenCalledTimes(2);
  });

  it("summarises only the problems a check found and hides idle status messages", async () => {
    const library = { ...model(), maintenanceMessage: "Diagnostics loaded", maintenanceReport: {
      ...model().maintenanceReport!, quarantine: null, cleanup_preview: {
        ...model().maintenanceReport!.cleanup_preview,
        removable: { output_folders: 2, temp_uploads: 1, temp_stage_folders: 0, pending_deletions: 0, orphan_sources: 0 },
      },
    } } as unknown as UseLibraryResult;
    await act(async () => root.render(<LibraryMaintenanceSection library={library} selectedDetailId={null} />));
    expect(container.querySelector(".maintenance-actions")?.textContent).toContain("Clean up would remove 2 leftover output folders and 1 old upload.");
    expect(container.textContent).not.toContain("Diagnostics loaded");
    expect(container.querySelector(".maintenance-message")).toBeNull();
  });

  it("shows a check or cleanup result in full, with each kept item as its own list entry", async () => {
    const library = { ...withUnownedFolders(), maintenanceMessage: "Nothing needed cleaning up. Kept:\nFirst kept thing\nSecond kept thing" } as unknown as UseLibraryResult;
    await act(async () => root.render(<LibraryMaintenanceSection library={library} selectedDetailId={null} />));
    const check = container.querySelector("#library-check-summary")!;
    expect(check.firstChild?.textContent).toBe("Nothing needs cleaning up. Kept:");
    expect([...check.querySelectorAll("li")].map((item) => item.textContent)).toEqual([
      "1 folder not in the library kept because it contains finished audio: Old Book",
      "1 folder not in the library kept because it changed recently: Orchard",
    ]);
    const message = container.querySelector(".maintenance-message")!;
    expect(message.getAttribute("role")).toBe("status");
    expect([...message.querySelectorAll("li")].map((item) => item.textContent)).toEqual(["First kept thing", "Second kept thing"]);
  });

  it("promises a backup only where one is made", async () => {
    await act(async () => root.render(<LibraryMaintenanceSection library={withUnownedFolders()} selectedDetailId={null} />));
    await act(async () => (container.querySelector("#library-unowned-btn") as HTMLButtonElement).click());
    // Removing folders changes no library row, so no backup is kept for it.
    expect(container.querySelector(".inline-confirm")?.textContent).not.toMatch(/backup/i);
    expect(container.querySelector(".inline-confirm")?.textContent).toContain("This cannot be undone.");
    await act(async () => (container.querySelector("#library-cleanup-btn") as HTMLButtonElement).click());
    expect(container.querySelector(".library-cleanup-confirmation")?.textContent)
      .toBe("Clean stale storage records and orphan files? A database backup is saved first if anything changes.");
  });

  it("returns focus to Clean up once the confirmed cleanup has finished", async () => {
    let finish!: () => void;
    function Harness() {
      const [loading, setLoading] = useState(false);
      const library = { ...model(), maintenanceLoading: loading, bookDetailText: "{}", cleanupStorage: () => {
        // Like the real hook: busy from the same update that closes the confirmation.
        setLoading(true);
        return new Promise<void>((resolve) => { finish = () => { setLoading(false); resolve(); }; });
      } } as unknown as UseLibraryResult;
      return <LibraryMaintenanceSection library={library} selectedDetailId={1} />;
    }
    await act(async () => root.render(<Harness />));
    const trigger = container.querySelector("#library-cleanup-btn") as HTMLButtonElement;
    await act(async () => trigger.click());
    await act(async () => (container.querySelector("#library-cleanup-confirm-btn") as HTMLButtonElement).click());
    expect(trigger.disabled).toBe(true);
    // While every action is disabled, the nearest usable control takes the focus the removed button dropped.
    const rawMetadata = container.querySelector(".raw-metadata-toggle summary") as HTMLElement;
    rawMetadata.tabIndex = 0;
    rawMetadata.focus();
    expect(document.activeElement).toBe(rawMetadata);

    await act(async () => finish());

    expect(trigger.disabled).toBe(false);
    expect(document.activeElement).toBe(trigger);
  });

  it("leaves focus alone after a cleanup when the user has moved it out of the section", async () => {
    let finish!: () => void;
    function Harness() {
      const [loading, setLoading] = useState(false);
      const library = { ...model(), maintenanceLoading: loading, cleanupStorage: () => {
        setLoading(true);
        return new Promise<void>((resolve) => { finish = () => { setLoading(false); resolve(); }; });
      } } as unknown as UseLibraryResult;
      return <LibraryMaintenanceSection library={library} selectedDetailId={null} />;
    }
    const elsewhere = document.createElement("button");
    document.body.append(elsewhere);
    try {
      await act(async () => root.render(<Harness />));
      await act(async () => (container.querySelector("#library-cleanup-btn") as HTMLButtonElement).click());
      await act(async () => (container.querySelector("#library-cleanup-confirm-btn") as HTMLButtonElement).click());
      elsewhere.focus();
      await act(async () => finish());
      expect(document.activeElement).toBe(elsewhere);
    } finally {
      elsewhere.remove();
    }
  });

  it("requires inline confirmation before restoring a backup", async () => {
    await act(async () => root.render(<LibraryMaintenanceSection library={model()} selectedDetailId={null} />));
    const trigger = container.querySelector(".maintenance-restore-btn") as HTMLButtonElement;
    expect(trigger).not.toBeNull();
    await act(async () => trigger.click());
    expect(restoreBackup).not.toHaveBeenCalled();
    expect(container.querySelector(".inline-confirm")?.textContent).toContain("safety copy");
    const cancel = container.querySelector("#library-restore-cancel-btn") as HTMLButtonElement;
    expect(document.activeElement).toBe(cancel);
    await act(async () => cancel.click());
    expect(restoreBackup).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(trigger);
    await act(async () => trigger.click());
    await act(async () => (container.querySelector("#library-restore-confirm-btn") as HTMLButtonElement).click());
    expect(restoreBackup).toHaveBeenCalledExactlyOnceWith("library_saved.db");
    expect(container.querySelector("#library-restore-confirm-btn")).toBeNull();
  });

  it("offers to remove folders not in the library only when a check found some", async () => {
    await act(async () => root.render(<LibraryMaintenanceSection library={model()} selectedDetailId={null} />));
    expect(container.querySelector("#library-unowned-btn")).toBeNull();
    await act(async () => root.render(<LibraryMaintenanceSection library={withUnownedFolders()} selectedDetailId={null} />));
    const row = container.querySelector("#library-unowned-btn")!.closest(".settings-row")!;
    expect(row.textContent).toContain("2 folders in the output folder are not in the library. Kept unless you remove them.");
    expect(container.querySelector(".inline-confirm")).toBeNull();
  });

  it("deletes the folders not in the library only after an inline confirmation that lists them", async () => {
    await act(async () => root.render(<LibraryMaintenanceSection library={withUnownedFolders()} selectedDetailId={null} />));
    const trigger = container.querySelector("#library-unowned-btn") as HTMLButtonElement;
    await act(async () => trigger.click());
    const confirm = container.querySelector(".inline-confirm")!;
    expect(confirm.textContent).toContain("Permanently delete 2 folders not in the library");
    expect([...confirm.querySelectorAll(".maintenance-folder-list .path-text")].map((path) => path.getAttribute("data-tip")))
      .toEqual(["/out/Old Book", "/out/Orchard"]);
    const cancel = container.querySelector("#library-unowned-cancel-btn") as HTMLButtonElement;
    expect(document.activeElement).toBe(cancel);
    await act(async () => { confirm.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    expect(container.querySelector(".inline-confirm")).toBeNull();
    expect(document.activeElement).toBe(trigger);
    expect(cleanupStorage).not.toHaveBeenCalled();

    await act(async () => trigger.click());
    const destructive = container.querySelector("#library-unowned-confirm-btn") as HTMLButtonElement;
    expect(destructive.classList.contains("btn-danger")).toBe(true);
    expect(destructive.textContent).toBe("Delete 2 folders");
    await act(async () => destructive.click());
    expect(cleanupStorage).toHaveBeenCalledExactlyOnceWith(["Old Book", "Orchard"]);
    expect(container.querySelector(".inline-confirm")).toBeNull();
  });

  it("keeps every folder when only Clean up is confirmed", async () => {
    await act(async () => root.render(<LibraryMaintenanceSection library={withUnownedFolders()} selectedDetailId={null} />));
    await act(async () => (container.querySelector("#library-cleanup-btn") as HTMLButtonElement).click());
    await act(async () => (container.querySelector("#library-cleanup-confirm-btn") as HTMLButtonElement).click());
    expect(cleanupStorage).toHaveBeenCalledExactlyOnceWith();
  });

  it("cancels restore with Escape and disables restore while maintenance runs", async () => {
    const library = model();
    await act(async () => root.render(<LibraryMaintenanceSection library={library} selectedDetailId={null} />));
    const trigger = container.querySelector(".maintenance-restore-btn") as HTMLButtonElement;
    expect(trigger).not.toBeNull();
    await act(async () => trigger.click());
    await act(async () => {
      container.querySelector(".inline-confirm")?.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });
    expect(container.querySelector("#library-restore-confirm-btn")).toBeNull();
    expect(restoreBackup).not.toHaveBeenCalled();
    await act(async () => root.render(<LibraryMaintenanceSection library={{ ...library, maintenanceLoading: true }} selectedDetailId={null} />));
    expect((container.querySelector(".maintenance-restore-btn") as HTMLButtonElement).disabled).toBe(true);
  });
});
