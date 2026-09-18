import { useEffect, useRef, useState } from "react";

import type { UseLibraryResult } from "./useLibrary";

export function LibraryMaintenanceSection({ library, selectedDetailId }: {
  library: UseLibraryResult;
  selectedDetailId: number | null;
}) {
  const [cleanupConfirmationArmed, setCleanupConfirmationArmed] = useState(false);
  const cleanupConfirmationRef = useRef(false);

  useEffect(() => {
    cleanupConfirmationRef.current = false;
    setCleanupConfirmationArmed(false);
  }, [library.selectedBookId, selectedDetailId]);

  return (
    <details className="library-disclosure maintenance-toggle">
      <summary>Maintenance</summary>
      <div className="maintenance-body">
        <div className="maintenance-actions">
          <button
            className="btn btn-compact"
            type="button"
            disabled={library.maintenanceLoading}
            data-tip="Audit storage for orphaned files and missing chapters"
            onClick={() => void library.loadMaintenanceDiagnostics()}
          >
            Diagnostics
          </button>
          <button
            className="btn btn-compact"
            type="button"
            disabled={library.maintenanceLoading}
            data-tip="Copy the library database to a timestamped backup"
            onClick={() => void library.backupDatabase()}
          >
            Back up database
          </button>
          {cleanupConfirmationArmed ? (
            <>
              <span className="field-hint library-cleanup-confirmation" role="status">
                Clean stale storage records and orphan files? A database backup will be created first.
              </span>
              <button
                id="library-cleanup-cancel-btn"
                className="btn btn-ghost btn-compact"
                type="button"
                onClick={() => {
                  cleanupConfirmationRef.current = false;
                  setCleanupConfirmationArmed(false);
                }}
              >
                Cancel
              </button>
              <button
                id="library-cleanup-confirm-btn"
                className="btn btn-danger btn-compact"
                type="button"
                disabled={library.maintenanceLoading}
                onClick={() => {
                  if (!cleanupConfirmationRef.current) {
                    return;
                  }
                  cleanupConfirmationRef.current = false;
                  setCleanupConfirmationArmed(false);
                  void library.cleanupStorage();
                }}
              >
                Clean up storage
              </button>
            </>
          ) : (
            <button
              id="library-cleanup-btn"
              className="btn btn-compact"
              type="button"
              disabled={library.maintenanceLoading}
              data-tip="Remove orphaned temp files. Finished audio is never touched."
              onClick={() => {
                cleanupConfirmationRef.current = true;
                setCleanupConfirmationArmed(true);
              }}
            >
              Clean up storage
            </button>
          )}
          <button
            className="btn btn-compact"
            type="button"
            disabled={!library.canOpenAppData}
            data-tip="Open the application data folder"
            onClick={() => void library.openAppDataFolder()}
          >
            App data
          </button>
        </div>
        {library.maintenanceMessage ? (
          <p className="field-hint status-sentence" role="status" aria-live="polite">{library.maintenanceMessage}</p>
        ) : null}
        {library.maintenanceReport ? (
          <div className="maintenance-grid">
            <div><span>Database</span><strong>{library.maintenanceReport.database_path}</strong></div>
            <div><span>Output</span><strong>{library.maintenanceReport.output_path}</strong></div>
            <div><span>Temp uploads</span><strong className="tabular-nums">{library.maintenanceReport.audit.stale_temp_uploads?.length ?? 0}</strong></div>
            <div><span>Orphan folders</span><strong className="tabular-nums">{library.maintenanceReport.audit.orphan_output_dirs?.length ?? 0}</strong></div>
            <div><span>Stale sources</span><strong className="tabular-nums">{library.maintenanceReport.audit.stale_book_sources?.length ?? 0}</strong></div>
            <div><span>Missing chapters</span><strong className="tabular-nums">{library.maintenanceReport.audit.stale_chapter_rows?.length ?? 0}</strong></div>
          </div>
        ) : null}
        {selectedDetailId !== null ? (
          <details className="library-disclosure raw-metadata-toggle">
            <summary>Raw metadata</summary>
            <pre id="book-detail">{library.bookDetailText}</pre>
          </details>
        ) : null}
      </div>
    </details>
  );
}
