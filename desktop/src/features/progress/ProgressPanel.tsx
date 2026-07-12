import type { ConversionJobStatus, PerformanceSnapshot, ProgressStageView } from "../../types/api";
import { GlyphProgress } from "../../ui/ascii/GlyphProgress";
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
}

function clampPercent(value: number): number {
  return Math.max(0, Math.min(100, Math.round(value)));
}

function stageBadge(index: number, total: number): string {
  return `stage ${String(index + 1).padStart(2, "0")}/${String(total).padStart(2, "0")}`;
}

function stageLabel(name: string): string {
  return String(name || "").replace(/_/g, " ");
}

function stageStatus(stage: ProgressStageView): string {
  if (stage.state === "done") {
    return "done";
  }
  if (stage.state === "active") {
    return `${clampPercent(stage.pct)}%`;
  }
  return "queued";
}

function ProgressBar({
  pct,
  active = false,
  label,
}: {
  pct: number;
  active?: boolean;
  label: string;
}) {
  const bounded = clampPercent(pct);

  return (
    <div
      className={`progress-strip${active ? " is-active" : ""}`}
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={bounded}
    >
      <span className="progress-strip-fill" style={{ width: `${bounded}%` }} />
    </div>
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
}: ProgressPanelProps) {
  const boundedProgress = clampPercent(progressPct);
  const activeStage = stages.find((stage) => stage.state === "active") ?? null;
  const canCancel = uiState === "running" && Boolean(activeJobId) && !isCancelling;
  const stageDetails = activeStage?.detail ?? [];
  const activeTitles = performance?.active_chapter_titles ?? [];
  const progressFocusClass = uiState === "cancelled" ? "panel-section progress-focus is-canceled" : "panel-section progress-focus";
  const presentation = buildProgressTerminalPresentation({
    uiState,
    stages,
    currentStage,
    currentMessage,
    performance,
    progressPct,
  });
  const requestedParallelText =
    presentation.showLiveWorkers && typeof performance?.requested_parallel_chapters === "number"
      ? `requested ${performance.requested_parallel_chapters}`
      : presentation.showLiveWorkers
        ? "requested --"
        : presentation.snapshotKind === "final"
          ? "final snapshot"
          : "not applicable";
  const effectiveParallelText =
    presentation.showLiveWorkers && typeof performance?.effective_parallel_chapters === "number"
      ? `effective ${performance.effective_parallel_chapters}`
      : presentation.showLiveWorkers
        ? "effective --"
        : presentation.snapshotKind === "neutral"
          ? "metrics unavailable"
          : "job finished";

  return (
    <>
      <div className={progressFocusClass}>
        <div className="progress-focus-header">
          <div>
            <span className="progress-kicker">current operation</span>
            <h2 className="progress-title">{presentation.operationTitle}</h2>
          </div>
          <span id="progress-pct" className="progress-percent">{`${boundedProgress}%`}</span>
        </div>

        <div id="progress-overall-bar" className="progress-overall">
          <ProgressBar pct={boundedProgress} active={uiState === "running"} label="overall conversion progress" />
        </div>

        <div className="progress-focus-meta">
          <span id="status-text">{statusText || uiState}</span>
          <GlyphProgress pct={boundedProgress} className="progress-glyphs" />
          <span id="eta-text">{uiState === "running" ? etaText || "eta: --:--" : "eta: —"}</span>
        </div>

        <div className="subtask-list" aria-label="Active stage detail">
          {presentation.showLiveActiveTask && stageDetails.length > 0 ? (
            stageDetails.map((detail) => (
              <div className="subtask-row" key={`${detail.label}-${detail.value}`}>
                <span className="subtask-label">{detail.label}</span>
                <ProgressBar pct={detail.pct} active={uiState === "running"} label={`${detail.label} progress`} />
                <span className="subtask-value">{detail.value}</span>
              </div>
            ))
          ) : (
            <div className="subtask-row">
              <span className="subtask-label">task</span>
              {presentation.showLiveActiveTask ? (
                <ProgressBar pct={activeStage?.pct ?? 0} active={uiState === "running"} label="active task progress" />
              ) : (
                <span className="field-hint" aria-hidden="true" />
              )}
              <span className="subtask-value">{presentation.activeTaskSummary}</span>
            </div>
          )}
        </div>
      </div>

      <div className="panel-section performance-section" aria-label="Performance snapshot">
        <div className="performance-grid">
          <div className="performance-item">
            <span className="performance-label">workers</span>
            <strong>{presentation.workerSummary}</strong>
            <span>{`${requestedParallelText} / ${effectiveParallelText} · ${presentation.queueSummary}`}</span>
          </div>
          <div className="performance-item">
            <span className="performance-label">throughput</span>
            <strong>{presentation.throughputSummary}</strong>
            <span>{presentation.elapsedSummary}</span>
          </div>
          <div className="performance-item">
            <span className="performance-label">{presentation.showLiveWorkers ? "active chapters" : "chapters"}</span>
            <strong>
              {presentation.showLiveWorkers
                ? activeTitles.length > 0
                  ? activeTitles.slice(0, 3).join(", ")
                  : "none"
                : "—"}
            </strong>
            <span>{presentation.chaptersSummary}</span>
          </div>
          <div className="performance-item">
            <span className="performance-label">mode</span>
            <strong>{presentation.modeSummary}</strong>
            <span>
              {presentation.showLiveWorkers && performance?.fallback_reason
                ? `fallback: ${performance.fallback_reason}`
                : presentation.showLiveWorkers
                  ? `eta ${typeof performance?.eta_seconds === "number" ? `${Math.round(performance.eta_seconds)}s` : "--"}`
                  : `${presentation.snapshotKind} summary`}
            </span>
          </div>
        </div>
      </div>

      <div id="stage-list" aria-label="Pipeline stages">
        {stages.map((stage, index) => (
          <div
            key={stage.name}
            className={`stage-row${stage.state === "active" ? " stage-active" : ""}${stage.state === "done" ? " stage-done" : ""}`}
          >
            <span className="stage-badge">{stageBadge(index, stages.length)}</span>
            <span className="stage-name">{stageLabel(stage.name)}</span>
            <ProgressBar pct={stage.pct} active={uiState === "running" && stage.state === "active"} label={`${stageLabel(stage.name)} progress`} />
            <span className="stage-status">{stageStatus(stage)}</span>
          </div>
        ))}
      </div>

      <div className="panel-section form-section">
        <div className="form-row">
          <span className="form-row-label">stage</span>
          <div className="form-row-field">
            <span className="field-hint">{currentStage || "waiting for stage updates"}</span>
          </div>
        </div>
        <div className="form-row">
          <span className="form-row-label">message</span>
          <div className="form-row-field">
            <span className="field-hint">{currentMessage || statusText || "waiting for logs"}</span>
          </div>
        </div>
      </div>

      <div className="action-row">
        <button
          id="progress-cancel-btn"
          className="btn btn-compact"
          type="button"
          disabled={!canCancel}
          onClick={() => {
            void onCancel();
          }}
        >
          {isCancelling ? "cancelling..." : "cancel"}
        </button>
      </div>
    </>
  );
}
