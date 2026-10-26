import { useRef, useState } from "react";

import type { ApiClient } from "../../api/client";
import { formatFreedBytes, safeError } from "./setupPageHelpers";
import { syncSetupPreferences } from "./setupPreferences";

export function SetupDangerZone({
  apiClient,
  onDeleted,
  onError,
}: {
  apiClient: ApiClient | null;
  onDeleted: () => Promise<unknown>;
  onError: (message: string | null) => void;
}) {
  const [deleteArmed, setDeleteArmed] = useState(false);
  const [deletePending, setDeletePending] = useState(false);
  const [deleteResult, setDeleteResult] = useState<string | null>(null);
  const deleteLocked = useRef(false);
  const deleteTrigger = useRef<HTMLButtonElement>(null);
  const cancelDelete = () => {
    setDeleteArmed(false);
    deleteTrigger.current?.focus();
  };

  const disarmDelete = () => setDeleteArmed(false);

  const onDeleteClick = async () => {
    if (!apiClient || deleteLocked.current) return;
    if (!deleteArmed) return;
    disarmDelete();
    deleteLocked.current = true;
    setDeletePending(true);
    setDeleteResult(null);
    onError(null);
    try {
      const result = await apiClient.deleteRuntimeSetupData({ timeoutMs: 20_000 });
      if ("setup_reset_id" in result && typeof result.setup_reset_id === "string") {
        syncSetupPreferences({ configured: false, setup_reset_id: result.setup_reset_id });
      }
      setDeleteResult(`${result.message ? `${result.message} ` : ""}Freed ${formatFreedBytes(result.freed_bytes)}.`);
      await onDeleted();
    } catch (reason: unknown) {
      onError(safeError(reason, "Local runtime data could not be deleted."));
    } finally {
      deleteLocked.current = false;
      setDeletePending(false);
    }
  };

  return (
    <details className="setup-danger disclosure" onToggle={(event) => { if (!event.currentTarget.open) disarmDelete(); }}>
      <summary>Reset local data</summary>
      <div className="setup-danger-box">
        <div className="setup-danger-actions">
          <span className="setup-danger-note">Removes every downloaded model and starts setup again. Books and audio stay.</span>
          <button
            ref={deleteTrigger}
            className="btn btn-ghost btn-sm btn-quiet-danger setup-delete"
            type="button"
            disabled={deletePending}
            onClick={() => setDeleteArmed(true)}
          >
            {deletePending ? "Deleting…" : "Delete local runtime data"}
          </button>
        </div>
        {deleteResult ? <p className="setup-model-feedback" role="status">{deleteResult}</p> : null}
        {deleteArmed ? <div className="inline-confirm setup-model-confirm" role="group" aria-label="Confirm deleting local runtime data" onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); cancelDelete(); } }}>
          <p>Delete downloaded models and reset runtime setup? This cannot be undone. Your books and audio will stay.</p>
          <div className="setup-model-buttons"><button className="btn btn-secondary btn-sm" type="button" autoFocus onClick={cancelDelete}>Cancel</button><button className="btn btn-danger btn-sm" type="button" onClick={() => void onDeleteClick()}>Delete</button></div>
        </div> : null}
      </div>
    </details>
  );
}
