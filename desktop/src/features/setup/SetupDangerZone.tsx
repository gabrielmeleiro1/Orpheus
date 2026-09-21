import { useCallback, useEffect, useRef, useState } from "react";

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
  const deleteTimer = useRef<ReturnType<typeof setTimeout>>();
  const deleteLocked = useRef(false);

  const disarmDelete = useCallback(() => {
    clearTimeout(deleteTimer.current);
    setDeleteArmed(false);
  }, []);

  useEffect(() => () => clearTimeout(deleteTimer.current), []);

  const onDeleteClick = async () => {
    if (!apiClient || deleteLocked.current) return;
    if (!deleteArmed) {
      setDeleteArmed(true);
      deleteTimer.current = setTimeout(disarmDelete, 5_000);
      return;
    }
    disarmDelete();
    deleteLocked.current = true;
    setDeletePending(true);
    setDeleteResult(null);
    onError(null);
    try {
      const result = await apiClient.deleteRuntimeSetupData({ timeoutMs: 20_000 });
      setDeleteResult(`Freed ${formatFreedBytes(result.freed_bytes)}.`);
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
            onClick={() => void onDeleteClick()}
            onBlur={disarmDelete}
            onKeyDown={(event) => { if (event.key === "Escape") disarmDelete(); }}
          >
            {deletePending ? "Deleting…" : deleteArmed ? "Confirm delete" : "Delete local runtime data"}
          </button>
          {deleteResult ? <span className="setup-danger-note">{deleteResult}</span> : null}
        </div>
      </div>
    </details>
  );
}
