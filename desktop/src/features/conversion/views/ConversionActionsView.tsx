import type {
  ConversionActionsViewModel,
  ConversionCapabilities,
  ConversionPanelActions,
  SourceQueueViewModel,
} from "../conversionPanelStateTypes";

interface ConversionActionsViewProps {
  sourceQueue: SourceQueueViewModel;
  conversionActions: ConversionActionsViewModel;
  capabilities: ConversionCapabilities;
  actions: ConversionPanelActions;
}

export function ConversionActionsView({
  sourceQueue,
  conversionActions,
  capabilities,
  actions,
}: ConversionActionsViewProps) {
  const emptySourceBlocker =
    conversionActions.blockingReason === "Select a source before starting conversion." &&
    sourceQueue.queueCount === 0 &&
    !sourceQueue.sourcePath;
  const showBlockingReason = Boolean(conversionActions.blockingReason) && !emptySourceBlocker;

  return (
    <div className="conversion-actions" role="region" aria-label="Conversion actions">
      <div className="conversion-queue-summary" role="status" aria-atomic="true">
        <strong>
          {sourceQueue.queueCount > 0
            ? `${sourceQueue.queueCount} ${sourceQueue.queueCount === 1 ? "source" : "sources"} in queue`
            : sourceQueue.sourcePath
              ? "1 source selected"
              : "Add a source to begin"}
        </strong>
        {sourceQueue.queueCount > 0 ? <span>{sourceQueue.queueStatus}</span> : null}
        {showBlockingReason ? (
          <span id="conversion-blocking-reason" className="conversion-actions-hint" role="status" aria-live="polite">
            {conversionActions.blockingReason}
          </span>
        ) : null}
      </div>

      {showBlockingReason && conversionActions.showSettingsShortcut ? (
        <button
          id="open-settings-btn"
          className="btn btn-ghost"
          type="button"
          onClick={actions.onOpenSettings}
        >
          Open Settings
        </button>
      ) : null}

      {capabilities.showRunQueue ? (
        <button
          id="run-queue-btn"
          className="btn btn-primary btn-lg"
          type="button"
          disabled={!capabilities.canRunQueue}
          onClick={() => void actions.onRunQueue()}
        >
          {sourceQueue.queueRunning ? "Running…" : "Run queue"}
        </button>
      ) : null}
      <button
        id="start-btn"
        className="btn btn-primary btn-lg"
        type="button"
        hidden={!capabilities.showStartConversion}
        disabled={!capabilities.canStartConversion}
        aria-describedby={showBlockingReason ? "conversion-blocking-reason" : undefined}
        onClick={() => void actions.onStartConversion()}
      >
        {conversionActions.startLabel}
      </button>

      {conversionActions.hasActiveJob ? (
        <div className="action-row conversion-job-actions">
          <button
            id="refresh-btn"
            className="btn btn-ghost"
            type="button"
            disabled={!capabilities.canRefreshJob}
            data-tip="Re-check this conversion's status"
            onClick={() => void actions.onRefreshJob()}
          >
            Refresh
          </button>
          <button
            id="cancel-btn"
            className="btn btn-ghost btn-danger"
            type="button"
            disabled={!capabilities.canCancelJob}
            onClick={() => void actions.onCancelJob()}
          >
            {conversionActions.cancelLabel}
          </button>
        </div>
      ) : null}
    </div>
  );
}
