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
          <span className="form-row-label">backend</span>
          <div className="form-row-field">
            <span id="desktop-backend-state" className="field-hint">
              {conversionActions.backendStatus}
            </span>
          </div>
        </div>
        <div className="form-row">
          <span className="form-row-label">health</span>
          <div className="form-row-field">
            <span id="desktop-health-result" className="field-hint">
              {conversionActions.healthStatus}
            </span>
          </div>
        </div>
        <div className="form-row">
          <span className="form-row-label">job</span>
          <div className="form-row-field">
            <span className="field-hint">{conversionActions.jobStatus}</span>
          </div>
        </div>
        <div className="form-row">
          <span className="form-row-label">detail</span>
          <div className="form-row-field">
            <span className="field-hint">{conversionActions.detailStatus}</span>
          </div>
        </div>
      </div>

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
          onClick={() => void actions.onRefreshJob()}
        >
          refresh
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
