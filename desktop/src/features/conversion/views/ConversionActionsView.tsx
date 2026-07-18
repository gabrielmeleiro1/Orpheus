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
      <div className="panel-section form-section">
        <div className="form-row">
          <span className="form-row-label" data-tip="Local conversion service state" data-tip-pos="right">Backend</span>
          <div className="form-row-field">
            <span id="desktop-backend-state" className="field-hint status-sentence">
              {conversionActions.backendStatus}
            </span>
          </div>
        </div>
        <div className="form-row">
          <span className="form-row-label">Health</span>
          <div className="form-row-field">
            <span id="desktop-health-result" className="field-hint status-sentence">
              {conversionActions.healthStatus}
            </span>
          </div>
        </div>
        <div className="form-row">
          <span className="form-row-label">Job</span>
          <div className="form-row-field">
            <span className="field-hint status-sentence">{conversionActions.jobStatus}</span>
          </div>
        </div>
        <div className="form-row">
          <span className="form-row-label">Detail</span>
          <div className="form-row-field">
            <span className="field-hint status-sentence">{conversionActions.detailStatus}</span>
          </div>
        </div>
      </div>

      {conversionActions.blockingReason ? (
        <div id="conversion-blocking-reason" className="action-row">
          <span className="field-hint is-error" role="status" aria-live="polite">
            {conversionActions.blockingReason}
          </span>
          {conversionActions.showSettingsShortcut ? (
            <button
              id="open-settings-btn"
              className="btn btn-compact"
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
        className="btn btn-primary"
        type="button"
        hidden={!capabilities.showStartConversion}
        disabled={!capabilities.canStartConversion}
        onClick={() => void actions.onStartConversion()}
      >
        {conversionActions.startLabel}
      </button>

      <div className="action-row" style={{ marginTop: "12px", marginBottom: "10px" }}>
        <button
          id="refresh-btn"
          className="btn btn-compact"
          type="button"
          disabled={!capabilities.canRefreshJob}
          data-tip="Re-query the job status"
          onClick={() => void actions.onRefreshJob()}
        >
          Refresh
        </button>
        <button
          id="cancel-btn"
          className="btn btn-compact"
          type="button"
          disabled={!capabilities.canCancelJob}
          onClick={() => void actions.onCancelJob()}
        >
          {conversionActions.cancelLabel}
        </button>
      </div>
    </>
  );
}
