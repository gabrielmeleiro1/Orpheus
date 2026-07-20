import type {
  ConversionActionsViewModel,
  ConversionCapabilities,
  ConversionPanelActions,
} from "../conversionPanelStateTypes";

interface ConversionActionsViewProps {
  conversionActions: ConversionActionsViewModel;
  capabilities: ConversionCapabilities;
  actions: ConversionPanelActions;
}

export function ConversionActionsView({
  conversionActions,
  capabilities,
  actions,
}: ConversionActionsViewProps) {
  return (
    <>
      {conversionActions.blockingReason ? (
        <div id="conversion-blocking-reason" className="action-row">
          <span className="hint" role="status" aria-live="polite">
            {conversionActions.blockingReason}
          </span>
          {conversionActions.showSettingsShortcut ? (
            <button
              id="open-settings-btn"
              className="btn btn-ghost btn-compact"
              type="button"
              onClick={actions.onOpenSettings}
            >
              Open Settings
            </button>
          ) : null}
        </div>
      ) : null}

      <button
        id="start-btn"
        className="btn btn-primary btn-block"
        type="button"
        hidden={!capabilities.showStartConversion}
        disabled={!capabilities.canStartConversion}
        onClick={() => void actions.onStartConversion()}
      >
        {conversionActions.startLabel}
      </button>

      {conversionActions.hasActiveJob ? (
        <div className="action-row" style={{ marginTop: "12px" }}>
          <button
            id="refresh-btn"
            className="btn btn-ghost btn-compact"
            type="button"
            disabled={!capabilities.canRefreshJob}
            data-tip="Re-check this conversion's status"
            onClick={() => void actions.onRefreshJob()}
          >
            Refresh
          </button>
          <button
            id="cancel-btn"
            className="btn btn-ghost btn-danger btn-compact"
            type="button"
            disabled={!capabilities.canCancelJob}
            onClick={() => void actions.onCancelJob()}
          >
            {conversionActions.cancelLabel}
          </button>
        </div>
      ) : null}
    </>
  );
}
