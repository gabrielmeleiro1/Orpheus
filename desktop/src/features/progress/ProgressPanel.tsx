import type { ConversionJobStatus, PerformanceSnapshot, ProgressStageView } from "../../types/api";
import { ProgressBar } from "../../ui/ProgressBar";
import { Icon } from "../../ui/icons";
import { buildProgressTerminalPresentation } from "./progressTerminalPresentation";
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
  onCancel: () => Promise<void>;
  onStartConvert?: () => void;
}

function hasJobToShow(uiState: ConversionJobStatus): boolean {
  return (
    uiState === "running" ||
    uiState === "completed" ||
    uiState === "failed" ||
    uiState === "cancelled"
  );
}

const PLACEHOLDER_VALUES = new Set([
  "",
  "—",
  "Not available",
  "Not recorded",
  "Not reported yet",
  "Elapsed not recorded",
  "Measuring…",
]);

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

function StageMarker({ state }: { state: ProgressStageView["state"] }) {
  if (state === "done") {
    return (
      <span className="stage-node is-done" aria-hidden="true">
        <Icon name="check" size={14} />
      </span>
    );
  }
  if (state === "active") {
    return (
      <span className="stage-node is-active" aria-hidden="true">
        <span className="stage-spinner" />
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
  onCancel,
  onStartConvert,
}: ProgressPanelProps) {
  if (!hasJobToShow(uiState)) {
    return (
      <div className="panel-section progress-empty card">
        <div className="empty-state">
          <span className="empty-state-icon" aria-hidden="true">
            <Icon name="progress" />
          </span>
          <span className="empty-state-title">No conversion running</span>
          <span className="empty-state-hint">
            Start a conversion from the Convert tab and its progress will appear here.
          </span>
          {onStartConvert ? (
            <button className="btn btn-primary" type="button" onClick={onStartConvert}>
              Convert a source
            </button>
          ) : null}
        </div>
      </div>
    );
  }

  const boundedProgress = clampPercent(progressPct);
  const activeStage = stages.find((stage) => stage.state === "active") ?? null;
  const canCancel = uiState === "running" && Boolean(activeJobId) && !isCancelling;
  const stageDetails = activeStage?.detail ?? [];
  const activeTitles = performance?.active_chapter_titles ?? [];
  const running = uiState === "running";
  const heroClass = `panel-section progress-hero card is-${uiState}`;
  const presentation = buildProgressTerminalPresentation({
    uiState,
    stages,
    currentStage,
    currentMessage,
    performance,
    progressPct,
  });
  const workersTip = presentation.showLiveWorkers
    ? `Chapters synthesized concurrently. Requested ${performance?.requested_parallel_chapters ?? "—"}, effective ${performance?.effective_parallel_chapters ?? "—"} after warm-up and memory limits.`
    : "Worker pool from the finished job.";
  const live = presentation.showLiveWorkers;
  // Finished jobs may have no performance data; omit their placeholder tiles.
  const hasValue = (value: string) => !PLACEHOLDER_VALUES.has(value.trim());
  const showStatusLine = uiState !== "completed";
  const terminalTiles = live
    ? []
    : [
        hasValue(presentation.workerSummary),
        hasValue(presentation.throughputSummary),
        hasValue(presentation.chaptersSummary),
        hasValue(presentation.modeSummary),
      ];
  const showTerminalPerformance = !live && terminalTiles.some(Boolean);
  const barTone = uiState === "cancelled" || uiState === "failed"
    ? "danger"
    : running
      ? "active"
      : "done";

  return (
    <>
      <div className={heroClass}>
        <div className="progress-hero-top">
          <div className="progress-hero-copy">
            <span className="card-eyebrow">Current operation</span>
            <h2 className="progress-title">{presentation.operationTitle}</h2>
          </div>
          {running && activeJobId ? (
            <button
              id="progress-cancel-btn"
              className="btn btn-danger"
              type="button"
              disabled={!canCancel}
              data-tip="Stop the running conversion. Finished chapters are kept."
              data-tip-pos="left"
              onClick={() => {
                void onCancel();
              }}
            >
              <Icon name="stop" />
              {isCancelling ? "Cancelling…" : "Cancel"}
            </button>
          ) : null}
        </div>

        <div className="progress-overall-block">
          <div className="progress-overall-meta tabular-nums">
            <span id="progress-pct" className="progress-pct-value">{`${boundedProgress}%`}</span>
            {running && etaText ? (
              <span
                id="eta-text"
                className="progress-eta"
                data-tip="Estimated time remaining, from current throughput"
              >
                {etaText}
              </span>
            ) : (
              <span className="progress-eta is-muted">{uiState === "completed" ? "Complete" : stateLabel(uiState)}</span>
            )}
          </div>
          <div id="progress-overall-bar" className="progress-overall">
            <ProgressBar
              pct={boundedProgress}
              tone={barTone}
              size="lg"
              label="Overall conversion progress"
            />
          </div>
          {showStatusLine ? (
            <span id="status-text" className="progress-status-text" role="status" aria-live="polite">
              {statusText || uiState}
            </span>
          ) : null}
        </div>
      </div>

      {presentation.showLiveActiveTask && stageDetails.length > 0 ? (
        <div className="panel-section subtask-section card" aria-label="Active stage detail">
          <div className="card-header">
            <h3 className="card-title">Active stage</h3>
          </div>
          {stageDetails.map((detail) => (
            <div className="subtask-row" key={`${detail.label}-${detail.value}`}>
              <span className="subtask-label">{detail.label}</span>
              <ProgressBar pct={detail.pct} tone={running ? "active" : "neutral"} size="hairline" label={`${detail.label} progress`} />
              <span className="subtask-value tabular-nums">{detail.value}</span>
            </div>
          ))}
        </div>
      ) : null}

      <div id="stage-list" className="panel-section stage-flow card" aria-label="Pipeline stages">
        <div className="card-header">
          <h3 className="card-title">Stages</h3>
        </div>
        {stages.map((stage) => (
          <div
            key={stage.name}
            className={`stage-row${stage.state === "active" ? " stage-active" : ""}${stage.state === "done" ? " stage-done" : ""}`}
          >
            <StageMarker state={stage.state} />
            <span className="stage-name">{stageLabel(stage.name)}</span>
            <ProgressBar
              pct={stage.pct}
              tone={running && stage.state === "active" ? "active" : stage.state === "done" ? "done" : "neutral"}
              size="hairline"
              label={`${stageLabel(stage.name)} progress`}
            />
            <span className="stage-status tabular-nums">{stageStatus(stage)}</span>
          </div>
        ))}
      </div>

      {live ? (
        <div className="panel-section performance-section card" aria-label="Performance snapshot">
          <div className="card-header">
            <h3 className="card-title">Performance</h3>
          </div>
          <div className="performance-grid">
            <div className="performance-item" data-tip={workersTip} data-tip-pos="top">
              <span className="performance-label">Workers</span>
              <strong>{presentation.workerSummary}</strong>
              <span>{presentation.queueSummary}</span>
            </div>
            <div
              className="performance-item"
              data-tip="Processing rates per stage: text cleaning, speech synthesis, audio encoding"
              data-tip-pos="top"
            >
              <span className="performance-label">Throughput</span>
              <strong>{presentation.throughputSummary}</strong>
              <span>{presentation.elapsedSummary}</span>
            </div>
            <div className="performance-item">
              <span className="performance-label">Active chapters</span>
              <strong>{activeTitles.length > 0 ? activeTitles.slice(0, 3).join(", ") : "None"}</strong>
              <span>{presentation.chaptersSummary}</span>
            </div>
            <div
              className="performance-item"
              data-tip="Worker scheduling and pipeline mode in effect for this job"
              data-tip-pos="top"
            >
              <span className="performance-label">Mode</span>
              <strong>{presentation.modeSummary}</strong>
              <span>
                {performance?.fallback_reason
                  ? `Fallback: ${performance.fallback_reason}`
                  : presentation.activeTaskSummary}
              </span>
            </div>
          </div>
        </div>
      ) : showTerminalPerformance ? (
        <div className="panel-section performance-section card" aria-label="Performance snapshot">
          <div className="card-header">
            <h3 className="card-title">Performance</h3>
          </div>
          <div className="performance-grid">
            {hasValue(presentation.workerSummary) ? (
              <div className="performance-item" data-tip={workersTip} data-tip-pos="top">
                <span className="performance-label">Workers</span>
                <strong>{presentation.workerSummary}</strong>
              </div>
            ) : null}
            {hasValue(presentation.throughputSummary) ? (
              <div
                className="performance-item"
                data-tip="Processing rates per stage: text cleaning, speech synthesis, audio encoding"
                data-tip-pos="top"
              >
                <span className="performance-label">Throughput</span>
                <strong>{presentation.throughputSummary}</strong>
                {hasValue(presentation.elapsedSummary) ? <span>{presentation.elapsedSummary}</span> : null}
              </div>
            ) : null}
            {hasValue(presentation.chaptersSummary) ? (
              <div className="performance-item">
                <span className="performance-label">Chapters</span>
                <strong>{presentation.chaptersSummary}</strong>
              </div>
            ) : null}
            {hasValue(presentation.modeSummary) ? (
              <div
                className="performance-item"
                data-tip="Worker scheduling and pipeline mode in effect for this job"
                data-tip-pos="top"
              >
                <span className="performance-label">Mode</span>
                <strong>{presentation.modeSummary}</strong>
              </div>
            ) : null}
          </div>
        </div>
      ) : null}
    </>
  );
}

function stateLabel(uiState: ConversionJobStatus): string {
  switch (uiState) {
    case "completed": return "Complete";
    case "failed": return "Failed";
    case "cancelled": return "Cancelled";
    case "running": return "Running";
    default: return "Idle";
  }
}
