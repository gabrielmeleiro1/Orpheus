export function SetupDangerZone({
  deletePending,
  deleteArmed,
  deleteResult,
  onDeleteClick,
  disarmDelete,
}: {
  deletePending: boolean;
  deleteArmed: boolean;
  deleteResult: string | null;
  onDeleteClick: () => void;
  disarmDelete: () => void;
}) {
  return (
    <section className="setup-danger" aria-labelledby="setup-danger-heading">
      <p className="section-label" id="setup-danger-heading">Local data</p>
      <div className="setup-danger-box">
        <p>Delete downloaded models and reset runtime setup.</p>
        <div className="setup-danger-actions">
          <button
            className="btn btn-danger"
            type="button"
            disabled={deletePending}
            onClick={onDeleteClick}
            onBlur={disarmDelete}
            onKeyDown={(event) => { if (event.key === "Escape") disarmDelete(); }}
          >
            {deletePending ? "Deleting…" : deleteArmed ? "Confirm delete" : "Delete local runtime data"}
          </button>
          {deleteResult ? <span className="setup-danger-note">{deleteResult}</span> : null}
        </div>
      </div>
    </section>
  );
}
