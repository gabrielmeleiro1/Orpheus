import { useEffect, useRef, useState } from "react";

import { PathText } from "../setup/PathText";
import type { UseLibraryResult } from "./useLibrary";
import type { UseLibraryMaintenanceResult } from "./useLibraryMaintenance";
import { backupDisplayName, checkLibrarySummary, cleanupConfirmation, messageParts, unownedFolders } from "./maintenanceModel";
import "./maintenance.css";

/** Hook messages that only restate what the rows already show. */
const QUIET_MESSAGES = new Set(["", "Maintenance idle", "Diagnostics loaded"]);

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

/** A result's sentences, then everything it kept as a list, so no part is cut off. */
function ResultText({ message }: { message: string }) {
  const { text, items } = messageParts(message);
  return (
    <>
      {text}
      {items.length > 0 ? (
        <ul className="maintenance-kept" aria-label="Kept">
          {items.map((item) => <li key={item}>{item}</li>)}
        </ul>
      ) : null}
    </>
  );
}

export function LibraryMaintenanceSection({ library, selectedDetailId }: {
  library: UseLibraryResult & Partial<UseLibraryMaintenanceResult>;
  selectedDetailId: number | null;
}) {
  const [cleanupConfirmationArmed, setCleanupConfirmationArmed] = useState(false);
  const cleanupConfirmationRef = useRef(false);
  const cleanupTriggerRef = useRef<HTMLButtonElement>(null);
  const cleanupCancelRef = useRef<HTMLButtonElement>(null);
  const restoreCleanupFocusRef = useRef(false);
  const [restoreConfirmationId, setRestoreConfirmationId] = useState<string | null>(null);
  const restoreConfirmationRef = useRef<string | null>(null);
  const restoreTriggerRef = useRef<HTMLButtonElement | null>(null);
  const restoreCancelRef = useRef<HTMLButtonElement>(null);
  const restoreBackupFocusRef = useRef(false);
  const [foldersConfirmationArmed, setFoldersConfirmationArmed] = useState(false);
  const foldersTriggerRef = useRef<HTMLButtonElement>(null);
  const foldersCancelRef = useRef<HTMLButtonElement>(null);
  const restoreFoldersFocusRef = useRef(false);
  const sectionRef = useRef<HTMLDivElement>(null);
  const focusAfterActionRef = useRef<HTMLButtonElement | null>(null);
  const loadDiagnostics = useRef(library.loadMaintenanceDiagnostics);
  loadDiagnostics.current = library.loadMaintenanceDiagnostics;

  // The section renders only while Settings is open: show the current findings and backups right away.
  useEffect(() => {
    void loadDiagnostics.current();
  }, []);

  useEffect(() => {
    if (restoreConfirmationId !== null) {
      restoreCancelRef.current?.focus();
    } else if (restoreBackupFocusRef.current) {
      restoreBackupFocusRef.current = false;
      restoreTriggerRef.current?.focus();
    }
  }, [restoreConfirmationId]);

  const cancelRestore = () => {
    restoreConfirmationRef.current = null;
    restoreBackupFocusRef.current = true;
    setRestoreConfirmationId(null);
  };

  useEffect(() => {
    if (cleanupConfirmationArmed) {
      cleanupCancelRef.current?.focus();
    } else if (restoreCleanupFocusRef.current) {
      restoreCleanupFocusRef.current = false;
      cleanupTriggerRef.current?.focus();
    }
  }, [cleanupConfirmationArmed]);

  useEffect(() => {
    if (foldersConfirmationArmed) {
      foldersCancelRef.current?.focus();
    } else if (restoreFoldersFocusRef.current) {
      restoreFoldersFocusRef.current = false;
      foldersTriggerRef.current?.focus();
    }
  }, [foldersConfirmationArmed]);

  // A confirmed action disables its trigger while it runs, so focus cannot go
  // back at once: return it when the action is over and the trigger is usable
  // again, unless the user has taken focus out of this section meanwhile.
  useEffect(() => {
    const trigger = focusAfterActionRef.current;
    if (!trigger || library.maintenanceLoading) return;
    focusAfterActionRef.current = null;
    const active = document.activeElement;
    if (trigger.isConnected && !trigger.disabled && (!active || active === document.body || sectionRef.current?.contains(active))) {
      trigger.focus();
    }
  }, [library.maintenanceLoading, cleanupConfirmationArmed, foldersConfirmationArmed, restoreConfirmationId]);

  const cancelFolders = () => {
    restoreFoldersFocusRef.current = true;
    setFoldersConfirmationArmed(false);
  };

  const cancelCleanup = () => {
    cleanupConfirmationRef.current = false;
    restoreCleanupFocusRef.current = true;
    setCleanupConfirmationArmed(false);
  };

  useEffect(() => {
    cleanupConfirmationRef.current = false;
    setCleanupConfirmationArmed(false);
    restoreConfirmationRef.current = null;
    setRestoreConfirmationId(null);
    setFoldersConfirmationArmed(false);
  }, [library.selectedBookId, selectedDetailId]);

  const report = library.maintenanceReport;
  const folders = unownedFolders(report);
  // A confirmation never outlives the folders it listed.
  const confirmingFolders = foldersConfirmationArmed && folders.length > 0;
  const backups = library.maintenanceBackups ?? [];
  const message = library.maintenanceMessage ?? "";

  return (
    <div className="maintenance-toggle maintenance-body" ref={sectionRef}>
      {report?.quarantine ? (
        <p className="maintenance-quarantine" role="alert">
          The library database was damaged and moved to {report.quarantine.path} on {new Date(report.quarantine.created_at).toLocaleString()}.
          {" "}Restore a backup below to recover your library.
        </p>
      ) : null}

      <div className="settings-rows maintenance-actions">
        {report ? (
          <div className="settings-row">
            <span className="field-label">Database</span>
            <PathText className="settings-path" path={report.database_path} />
          </div>
        ) : null}
        <div className="settings-row">
          <span className="field-label">Check library</span>
          <div className="settings-row-hint" id="library-check-summary"><ResultText message={checkLibrarySummary(report)} /></div>
          <button
            className="btn btn-secondary btn-sm"
            type="button"
            disabled={library.maintenanceLoading}
            onClick={() => void library.loadMaintenanceDiagnostics()}
          >
            Check
          </button>
        </div>
        <div className="settings-row">
          <span className="field-label">Back up</span>
          <span className="settings-row-hint">Saves a dated copy of the library database.</span>
          <button
            className="btn btn-secondary btn-sm"
            type="button"
            disabled={library.maintenanceLoading}
            onClick={() => void library.backupDatabase()}
          >
            Back up
          </button>
        </div>
        <div className="settings-row">
          <span className="field-label">Clean up</span>
          <span className="settings-row-hint">Removes leftover temporary files. Finished audio is kept.</span>
          <button
            id="library-cleanup-btn"
            ref={cleanupTriggerRef}
            className="btn btn-secondary btn-sm"
            type="button"
            disabled={library.maintenanceLoading}
            onClick={() => {
              cancelRestore();
              setFoldersConfirmationArmed(false);
              cleanupConfirmationRef.current = true;
              setCleanupConfirmationArmed(true);
            }}
          >
            Clean up
          </button>
        </div>
        {folders.length > 0 ? (
          <div className="settings-row">
            <span className="field-label">Other folders</span>
            <span className="settings-row-hint">
              {plural(folders.length, "folder in the output folder is", "folders in the output folder are")} not in the library. Kept unless you remove {folders.length === 1 ? "it" : "them"}.
            </span>
            <button
              id="library-unowned-btn"
              ref={foldersTriggerRef}
              className="btn btn-secondary btn-sm"
              type="button"
              disabled={library.maintenanceLoading}
              onClick={() => {
                cleanupConfirmationRef.current = false;
                setCleanupConfirmationArmed(false);
                restoreConfirmationRef.current = null;
                setRestoreConfirmationId(null);
                setFoldersConfirmationArmed(true);
              }}
            >
              Remove…
            </button>
          </div>
        ) : null}
      </div>

      {confirmingFolders ? (
        <div
          className="inline-confirm maintenance-confirm"
          role="group"
          aria-label="Confirm removing folders not in the library"
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              event.stopPropagation();
              cancelFolders();
            }
          }}
        >
          <div className="maintenance-confirm-copy" role="status">
            <p>
              Permanently delete {plural(folders.length, "folder", "folders")} not in the library, with any audio inside? Leftover temporary files are cleaned up too. This cannot be undone.
            </p>
            <ul className="maintenance-folder-list" aria-label="Folders to delete">
              {folders.map((folder) => <li key={folder.name}><PathText className="settings-path" path={folder.path} /></li>)}
            </ul>
          </div>
          <button id="library-unowned-cancel-btn" ref={foldersCancelRef} className="btn btn-secondary btn-sm" type="button" onClick={cancelFolders}>
            Cancel
          </button>
          <button
            id="library-unowned-confirm-btn"
            className="btn btn-danger btn-sm"
            type="button"
            disabled={library.maintenanceLoading}
            onClick={() => {
              setFoldersConfirmationArmed(false);
              focusAfterActionRef.current = foldersTriggerRef.current;
              void library.cleanupStorage(folders.map((folder) => folder.name));
            }}
          >
            {folders.length === 1 ? "Delete folder" : `Delete ${folders.length} folders`}
          </button>
        </div>
      ) : null}

      {cleanupConfirmationArmed ? (
        <div
          className="inline-confirm maintenance-confirm"
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              event.stopPropagation();
              cancelCleanup();
            }
          }}
        >
          <span className="library-cleanup-confirmation" role="status">
            {cleanupConfirmation(report)}
          </span>
          <button
            id="library-cleanup-cancel-btn"
            ref={cleanupCancelRef}
            className="btn btn-secondary btn-sm"
            type="button"
            onClick={cancelCleanup}
          >
            Cancel
          </button>
          <button
            id="library-cleanup-confirm-btn"
            className="btn btn-danger btn-sm"
            type="button"
            disabled={library.maintenanceLoading}
            onClick={() => {
              if (!cleanupConfirmationRef.current) {
                return;
              }
              cleanupConfirmationRef.current = false;
              setCleanupConfirmationArmed(false);
              focusAfterActionRef.current = cleanupTriggerRef.current;
              void library.cleanupStorage();
            }}
          >
            Clean up storage
          </button>
        </div>
      ) : null}

      {QUIET_MESSAGES.has(message) ? null : (
        <div className="maintenance-message status-sentence" role="status" aria-live="polite"><ResultText message={message} /></div>
      )}

      {backups.length > 0 ? (
        <section className="maintenance-backups-group" aria-labelledby="maintenance-backups-heading">
          <h3 className="maintenance-group-label" id="maintenance-backups-heading">Backups</h3>
          <ul className="settings-rows maintenance-backups" aria-label="Database backups">
            {backups.map((backup) => (
              <li className="settings-row" key={backup.id}>
                <span className="field-label tabular-nums">{new Date(backup.created_at).toLocaleDateString()}</span>
                <span className="maintenance-backup-copy">
                  <strong>{backupDisplayName(backup)}</strong>
                  <span className="settings-row-hint tabular-nums">{new Date(backup.created_at).toLocaleTimeString()} · {(backup.size / 1024).toFixed(1)} KB{backup.book_count !== null ? ` · ${plural(backup.book_count, "book", "books")}` : ""}</span>
                </span>
                <button
                  className="btn btn-ghost btn-sm btn-flush-end maintenance-restore-btn"
                  type="button"
                  disabled={library.maintenanceLoading || !library.restoreBackup}
                  onClick={(event) => {
                    cleanupConfirmationRef.current = false;
                    setCleanupConfirmationArmed(false);
                    setFoldersConfirmationArmed(false);
                    restoreTriggerRef.current = event.currentTarget;
                    restoreConfirmationRef.current = backup.id;
                    setRestoreConfirmationId(backup.id);
                  }}
                >
                  Restore
                </button>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {restoreConfirmationId !== null ? (
        <div
          className="inline-confirm maintenance-confirm"
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              event.stopPropagation();
              cancelRestore();
            }
          }}
        >
          <span role="status">
            Restore {backupDisplayName(backups.find((backup) => backup.id === restoreConfirmationId)
              ?? { label: null, filename: restoreConfirmationId, created_at: "", book_count: null })}? This replaces your current library database. A safety copy will be kept first.
          </span>
          <button id="library-restore-cancel-btn" ref={restoreCancelRef} className="btn btn-secondary btn-sm" type="button" onClick={cancelRestore}>
            Cancel
          </button>
          <button
            id="library-restore-confirm-btn"
            className="btn btn-danger btn-sm"
            type="button"
            disabled={library.maintenanceLoading}
            onClick={() => {
              const id = restoreConfirmationRef.current;
              if (id === null) {
                return;
              }
              restoreConfirmationRef.current = null;
              setRestoreConfirmationId(null);
              focusAfterActionRef.current = restoreTriggerRef.current;
              void library.restoreBackup?.(id);
            }}
          >
            Restore backup
          </button>
        </div>
      ) : null}

      {selectedDetailId !== null ? (
        <details className="disclosure raw-metadata-toggle">
          <summary>Raw metadata</summary>
          <pre id="book-detail">{library.bookDetailText}</pre>
        </details>
      ) : null}
    </div>
  );
}
