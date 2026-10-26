import type {
  ConversionActionsViewModel,
  ConversionCapabilities,
  ConversionPanelActions,
  SourceQueueViewModel,
} from "../conversionPanelStateTypes";
import { SOURCE_REQUIRED_REASON } from "../useConversionPanelState";
import { StopConversionConfirm, useStopConfirm } from "../../../ui/StopConversionConfirm";

interface ConversionActionsViewProps {
  sourceQueue: SourceQueueViewModel;
  conversionActions: ConversionActionsViewModel;
  capabilities: ConversionCapabilities;
  actions: ConversionPanelActions;
}

function queueSummary(sourceQueue: SourceQueueViewModel): string {
  if (sourceQueue.queueStatus) return sourceQueue.queueStatus;
  // Rows that failed before starting are not waiting to convert; count them apart.
  const unfinished = sourceQueue.queueItems.filter((item) => !item.finished);
  const failed = unfinished.filter((item) => item.statusClassName?.includes("status-failed")).length;
  const pending = unfinished.length - failed;
  if (pending > 0) return `${pending} ${pending === 1 ? "book" : "books"} to convert`;
  if (failed > 0) return `${failed} ${failed === 1 ? "book" : "books"} could not start`;
  if (sourceQueue.queueItems.length > 0) return "Queue finished";
  if (sourceQueue.sourceInput === "paste" && sourceQueue.pastedText.trim()) return "Pasted text ready";
  return SOURCE_REQUIRED_REASON;
}

/**
 * One summary line and one primary action. While a conversion runs the primary
 * slot becomes a quiet Cancel; Refresh appears only when a start needs re-checking.
 */
export function ConversionActionsView({
  sourceQueue,
  conversionActions,
  capabilities,
  actions,
}: ConversionActionsViewProps) {
  const running = conversionActions.hasActiveJob;
  const stopConfirm = useStopConfirm(running ? conversionActions.cancelJobId ?? null : null);
  const showBlockingReason = Boolean(conversionActions.blockingReason) && !running;
  const summary = running
    ? conversionActions.isStarting
      ? conversionActions.startLabel
      : conversionActions.runningStatus
        || (sourceQueue.queueRunning && sourceQueue.queueStatus ? sourceQueue.queueStatus : "Converting…")
    : showBlockingReason ? conversionActions.blockingReason : queueSummary(sourceQueue);
  // One white button per view: the waiting books when they can start, else the selected book.
  const runQueueIsPrimary = !capabilities.showStartConversion || capabilities.canRunQueue;
  const recovery = showBlockingReason && conversionActions.showSettingsShortcut ? (
    <button id="open-settings-btn" className="btn btn-ghost btn-sm btn-flush-end" type="button" onClick={actions.onOpenSettings}>
      Open Settings
    </button>
  ) : capabilities.canRetryRuntimeOptions ? (
    <button id="retry-runtime-options-btn" className="btn btn-ghost btn-sm btn-flush-end" type="button" onClick={actions.onRetryRuntimeOptions}>
      Retry
    </button>
  ) : null;

  return (
    <div className="conversion-actions" role="region" aria-label="Conversion actions">
      {/* A long backend error scrolls inside a short box; it is a Tab stop
          while it holds an error so it can be scrolled from the keyboard. */}
      <div id="form-error" className="form-error" role="alert" aria-live="assertive" tabIndex={sourceQueue.formError ? 0 : undefined}>{sourceQueue.formError}</div>
      <div className="conversion-summary-row">
        <p
          id={showBlockingReason ? "conversion-blocking-reason" : undefined}
          className="conversion-queue-summary"
          role="status"
          aria-live="polite"
          aria-atomic="true"
        >
          {summary}
        </p>
        {recovery}
      </div>

      <div className="conversion-action-row">
        {conversionActions.showRefreshJob ? (
          <button
            id="refresh-btn"
            className="btn btn-secondary"
            type="button"
            disabled={!capabilities.canRefreshJob}
            data-tip="Check whether the conversion started"
            onClick={() => void actions.onRefreshJob()}
          >
            Refresh
          </button>
        ) : null}
        {running && stopConfirm.armed ? (
          <StopConversionConfirm
            id="cancel-confirm"
            onKeep={stopConfirm.keep}
            onStop={() => {
              stopConfirm.confirmed();
              void actions.onConfirmCancelJob();
            }}
          />
        ) : running ? (
          <button
            id="cancel-btn"
            ref={stopConfirm.triggerRef}
            className="btn btn-secondary btn-md btn-quiet-danger conversion-cancel-btn"
            type="button"
            disabled={!capabilities.canCancelJob}
            onClick={(event) => {
              // A running book asks first; a pending start or wait stops at once.
              if (conversionActions.cancelJobId) {
                if (actions.acceptsCancelClick(event.detail)) stopConfirm.arm();
                return;
              }
              void actions.onCancelJob(event.detail);
            }}
          >
            {conversionActions.cancelLabel}
          </button>
        ) : null}
        {capabilities.showRunQueue ? (
          <button
            id="run-queue-btn"
            className={`btn btn-md ${runQueueIsPrimary ? "btn-primary" : "btn-secondary"}`}
            type="button"
            hidden={running}
            disabled={!capabilities.canRunQueue}
            aria-describedby={showBlockingReason ? "conversion-blocking-reason" : undefined}
            onClick={() => void actions.onRunQueue()}
          >
            {conversionActions.isStarting ? conversionActions.startLabel : sourceQueue.queueRunning ? "Running…" : "Start all"}
          </button>
        ) : null}
        <button
          id="start-btn"
          className={`btn btn-md ${capabilities.showRunQueue && runQueueIsPrimary ? "btn-secondary" : "btn-primary"}`}
          type="button"
          hidden={!capabilities.showStartConversion || running}
          disabled={!capabilities.canStartConversion}
          aria-describedby={showBlockingReason ? "conversion-blocking-reason" : undefined}
          onClick={() => void actions.onStartConversion()}
        >
          {conversionActions.startLabel}
        </button>
      </div>
    </div>
  );
}
