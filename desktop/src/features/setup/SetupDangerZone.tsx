import { useRef, useState } from "react";

import type { ApiClient } from "../../api/client";
import { formatFreedBytes, safeError } from "./setupPageHelpers";

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
    <details className="setup-danger disclosure-card disclosure" onToggle={(event) => { if (!event.currentTarget.open) disarmDelete(); }}>
      <summary>
        <span>Local data</span>
        <span className="disclosure-card-meta">Manage downloaded models and runtime setup</span>
      </summary>
      <div className="setup-danger-box">
        <p>Delete downloaded models and reset runtime setup.</p>
        <div className="setup-danger-actions">
          <button
            className="btn btn-danger"
            type="button"
            disabled={deletePending}
            onClick={() => setDeleteArmed(true)}
          >
            {deletePending ? "Deleting…" : "Delete local runtime data"}
          </button>
          {deleteResult ? <span className="setup-danger-note">{deleteResult}</span> : null}
        </div>
        {deleteArmed ? <div className="setup-model-confirm" role="group" aria-label="Confirm deleting local runtime data" onKeyDown={(event) => { if (event.key === "Escape") disarmDelete(); }}>
          <p>Delete downloaded models and reset runtime setup? This cannot be undone. Your books and audio will stay.</p>
          <div className="setup-model-buttons"><button className="btn" type="button" autoFocus onClick={disarmDelete}>Keep</button><button className="btn btn-danger" type="button" onClick={() => void onDeleteClick()}>Delete</button></div>
        </div> : null}
      </div>
    </details>
  );
}
