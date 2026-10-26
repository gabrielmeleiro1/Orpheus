import { Fragment } from "react";

import type { ConversionJobStatus, PerformanceSnapshot, ProgressStageView } from "../../types/api";
import { AsciiArt } from "../../ui/ascii/AsciiArt";
import { AsciiLoader } from "../../ui/ascii/AsciiLoader";
import { ProgressBar } from "../../ui/ProgressBar";
import { Icon } from "../../ui/icons";
import { StopConversionConfirm, useStopConfirm } from "../../ui/StopConversionConfirm";
import { chapterStageLabel } from "../events/progressStageModel";
import { buildProgressTerminalPresentation, formatStageDisplayName } from "./progressTerminalPresentation";
import "./progress.css";

interface ProgressPanelProps {
  uiState: ConversionJobStatus;
  statusText: string;
  progressPct: number;
  etaText: string;
  stages: ProgressStageView[];
  currentStage: string;
  currentMessage: string;
  performance: PerformanceSnapshot | null;
  activeJobId: string | null;
  isCancelling: boolean;
  cancellationError?: string;
  /** Why fewer chapters run at once than were requested, when that is the case. */
  parallelNote?: string;
  onCancel: () => Promise<void>;
}

function hasJobToShow(uiState: ConversionJobStatus): boolean {
  return (
    uiState === "running" ||
    uiState === "completed" ||
    uiState === "failed" ||
    uiState === "cancelled"
  );
}

function clampPercent(value: number): number {
  return Math.max(0, Math.min(100, Math.round(value)));
}

function stageLabel(name: string): string {
  return String(name || "").replace(/_/g, " ");
}

function stageStatus(stage: ProgressStageView): string {
  if (stage.state === "done") {
    return "Done";
  }
  if (stage.state === "active") {
    return `${clampPercent(stage.pct)}%`;
  }
  return "Queued";
}

/** "ETA ~12s" from the stream reads as "~12s left", matching the queue row. */
function remainingLabel(etaText: string): string {
  const eta = etaText.replace(/^eta\s*/i, "").trim();
  return eta ? `${eta} left` : "";
}

function StageMarker({ state, busy }: { state: ProgressStageView["state"]; busy: boolean }) {
  if (state === "done") {
    return (
      <span className="stage-node is-done" aria-hidden="true">
        <Icon name="check" />
      </span>
    );
  }
  if (state === "active") {
    return (
      <span className="stage-node is-active" aria-hidden="true">
        <AsciiLoader active={busy} variant="ring" className={`stage-spinner${busy ? " is-busy" : ""}`} />
      </span>
    );
  }
  return (
    <span className="stage-node is-pending" aria-hidden="true">
      <span className="stage-dot" />
    </span>
  );
}

export function ProgressPanel({
  uiState,
  statusText,
  progressPct,
  etaText,
  stages,
  currentStage,
  currentMessage,
  performance,
  activeJobId,
  isCancelling,
  cancellationError,
  parallelNote,
  onCancel,
}: ProgressPanelProps) {
  const running = uiState === "running";
  const stopConfirm = useStopConfirm(running && !isCancelling ? activeJobId : null);
  if (!hasJobToShow(uiState)) {
    const unknown = uiState === "unknown";
    return (
      <div className="panel-section progress-empty">
        <div className="empty-state">
          <AsciiArt name="idle" className="progress-empty-art" />
          <span className="empty-state-title">{unknown ? "Conversion status unavailable" : "No conversion running"}</span>
          <span className="empty-state-hint" role={unknown ? "status" : undefined}>
            {unknown ? statusText || "Waiting to reconnect and check the current conversion."
              : "Start a conversion from Create and its progress will appear here."}
          </span>
        </div>
      </div>
    );
  }

  const boundedProgress = clampPercent(progressPct);
  const canCancel = running && Boolean(activeJobId) && !isCancelling;
  const presentation = buildProgressTerminalPresentation({
    uiState,
    stages,
    currentStage,
    currentMessage,
    performance,
    progressPct,
  });
  const stageTitle = formatStageDisplayName(presentation.operationTitle);
  const title = running ? chapterStageLabel(stages, stageTitle) : stageTitle;
  const remaining = running ? remainingLabel(etaText) : "";
  const showStatusLine = uiState !== "completed" && Boolean(statusText);
  const barTone = uiState === "cancelled" || uiState === "failed"
    ? "danger"
    : running
      ? "active"
      : "done";

  return (
    <div className="progress-panel">
      <div className={`panel-section progress-hero is-${uiState}`}>
        <div className="progress-headline">
          {running ? <AsciiLoader active variant="ring" className="progress-hero-loader" /> : null}
          <h2 className="card-title progress-title">{title}</h2>
          {uiState !== "completed" ? (
            <span id="progress-pct" className="progress-pct-value tabular-nums">{`${boundedProgress}% overall`}</span>
          ) : null}
          {running && activeJobId && !stopConfirm.armed ? (
            <button
              id="progress-cancel-btn"
              ref={stopConfirm.triggerRef}
              className="btn btn-ghost btn-sm btn-flush-end btn-quiet-danger progress-cancel-btn"
              type="button"
              disabled={!canCancel}
              data-tip="Finished chapters are kept"
              data-tip-pos="left"
              onClick={stopConfirm.arm}
            >
              <Icon name="stop" />
              {isCancelling ? "Cancelling…" : "Cancel"}
            </button>
          ) : null}
        </div>
        {running && stopConfirm.armed ? (
          <StopConversionConfirm
            id="progress-cancel-confirm"
            className="progress-stop-confirm"
            onKeep={stopConfirm.keep}
            onStop={() => {
              stopConfirm.confirmed();
              void onCancel();
            }}
          />
        ) : null}
        <div id="progress-overall-bar" className="progress-overall">
          <ProgressBar
            pct={boundedProgress}
            tone={barTone}
            size="lg"
            variant="ascii"
            cells="fit"
            label="Overall conversion progress"
          />
        </div>
        {showStatusLine || remaining ? (
          <div className="progress-subline">
            {showStatusLine ? (
              <span id="status-text" className="progress-status-text" role="status" aria-live="polite">
                {statusText}
              </span>
            ) : <span />}
            {remaining ? (
              <span id="eta-text" className="progress-eta tabular-nums" data-tip="Estimated from the current speed" data-tip-pos="left">
                {remaining}
              </span>
            ) : null}
          </div>
        ) : null}
        {running && parallelNote ? <p id="parallel-limit-note" className="field-hint">{parallelNote}</p> : null}
        {running && cancellationError ? <p role="alert" className="field-hint is-error">{cancellationError}</p> : null}
      </div>

      <div className="progress-summary">
        <div id="stage-list" className="panel-section stage-flow" aria-label="Pipeline stages">
          <h3 className="eyebrow">Stages</h3>
          <div className="stage-grid">
            {stages.map((stage) => {
              const details = presentation.showLiveActiveTask && stage.state === "active" ? stage.detail : [];
              return (
                <Fragment key={stage.name}>
                  <div className={`stage-row${stage.state === "active" ? " stage-active" : ""}${stage.state === "done" ? " stage-done" : ""}`}>
                    <StageMarker state={stage.state} busy={running} />
                    <span className="stage-name">{formatStageDisplayName(stage.name)}</span>
                    <ProgressBar
                      pct={stage.pct}
                      tone={running && stage.state === "active" ? "active" : stage.state === "done" ? "done" : "neutral"}
                      size="hairline"
                      variant="ascii"
                      cells="fit"
                      label={`${stageLabel(stage.name)} progress`}
                    />
                    <span className="stage-status tabular-nums">{stageStatus(stage)}</span>
                  </div>
                  {details.length > 0 ? (
                    <div className="subtask-section" aria-label="Active stage detail">
                      {details.map((detail) => (
                        <div className="subtask-row" key={`${detail.label}-${detail.value}`}>
                          <span aria-hidden="true" />
                          <span className="subtask-label">{formatStageDisplayName(detail.label)}</span>
                          <ProgressBar pct={detail.pct} tone={running ? "active" : "neutral"} size="hairline" variant="ascii" cells="fit" label={`${detail.label} progress`} />
                          <span className="subtask-value tabular-nums">{detail.value.charAt(0).toUpperCase() + detail.value.slice(1)}</span>
                        </div>
                      ))}
                    </div>
                  ) : null}
                </Fragment>
              );
            })}
          </div>
        </div>

        <details className="panel-section performance-section disclosure" aria-label="Performance snapshot">
          <summary>
            <span className="eyebrow">Performance</span>
            {presentation.elapsed ? <span className="performance-summary-meta tabular-nums">{presentation.elapsed}</span> : null}
          </summary>
          {presentation.metrics.length > 0 ? (
            <dl className="performance-grid">
              {presentation.metrics.map((metric) => (
                <div className="performance-item" key={metric.label} {...(metric.tip ? { "data-tip": metric.tip, "data-tip-pos": "top" } : {})}>
                  <dt className="performance-label">{metric.label}</dt>
                  <dd className="performance-value">
                    <strong className="tabular-nums">{metric.value}</strong>
                    {metric.note ? <span className="performance-note">{metric.note}</span> : null}
                  </dd>
                </div>
              ))}
            </dl>
          ) : (
            <p className="performance-empty">No measurements yet</p>
          )}
        </details>
      </div>
    </div>
  );
}
