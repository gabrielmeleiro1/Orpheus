import type { ConversionJobStatus, PerformanceSnapshot, ProgressStageView } from "../../types/api";
import { ProgressBar } from "../../ui/ProgressBar";
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

const RING_RADIUS = 30;
const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;

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

function ProgressRing({ pct, state }: { pct: number; state: ConversionJobStatus }) {
  const bounded = clampPercent(pct);
  const offset = RING_CIRCUMFERENCE * (1 - bounded / 100);

  return (
    <div className={`progress-ring is-${state}`} aria-hidden="true">
      <svg viewBox="0 0 76 76" focusable="false">
        <circle className="progress-ring-track" cx="38" cy="38" r={RING_RADIUS} />
        <circle
          className="progress-ring-fill"
          cx="38"
          cy="38"
          r={RING_RADIUS}
          strokeDasharray={RING_CIRCUMFERENCE}
          strokeDashoffset={offset}
        />
      </svg>
      <span id="progress-pct" className="progress-ring-value">{`${bounded}%`}</span>
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
  onStartConvert,
}: ProgressPanelProps) {
  if (!hasJobToShow(uiState)) {
    return (
      <div className="panel-section progress-empty">
        <div className="empty-state">
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
  const heroClass = `panel-section progress-hero is-${uiState}`;
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

  return (
    <>
      <div className={heroClass}>
        <ProgressRing pct={boundedProgress} state={uiState} />

        <div className="progress-hero-main">
          <span className="progress-kicker">Current operation</span>
          <h2 className="progress-title">{presentation.operationTitle}</h2>
          <div id="progress-overall-bar" className="progress-overall">
            <ProgressBar
              pct={boundedProgress}
              tone={uiState === "cancelled" || uiState === "failed" ? "danger" : running ? "active" : "done"}
              label="Overall conversion progress"
            />
          </div>
          {showStatusLine || (running && etaText) ? (
            <div className="progress-hero-meta">
              {showStatusLine ? (
                <span id="status-text" className="progress-status-text" role="status" aria-live="polite">{statusText || uiState}</span>
              ) : null}
              {running && etaText ? (
                <span
                  id="eta-text"
                  className="progress-chip"
                  data-tip="Estimated time remaining, from current throughput"
                >
                  {etaText}
                </span>
              ) : null}
            </div>
          ) : null}
        </div>

        {running && activeJobId ? (
          <button
            id="progress-cancel-btn"
            className="btn btn-compact progress-cancel"
            type="button"
            disabled={!canCancel}
            data-tip="Stop the running conversion. Finished chapters are kept."
            data-tip-pos="left"
            onClick={() => {
              void onCancel();
            }}
          >
            {isCancelling ? "Cancelling…" : "Cancel"}
          </button>
        ) : null}
      </div>

      {presentation.showLiveActiveTask && stageDetails.length > 0 ? (
        <div className="panel-section subtask-section" aria-label="Active stage detail">
          {stageDetails.map((detail) => (
            <div className="subtask-row" key={`${detail.label}-${detail.value}`}>
              <span className="subtask-label">{detail.label}</span>
              <ProgressBar pct={detail.pct} tone={running ? "active" : "neutral"} size="hairline" label={`${detail.label} progress`} />
              <span className="subtask-value">{detail.value}</span>
            </div>
          ))}
        </div>
      ) : null}

      <div id="stage-list" className="panel-section stage-flow" aria-label="Pipeline stages">
        {stages.map((stage, index) => (
          <div
            key={stage.name}
            className={`stage-row${stage.state === "active" ? " stage-active" : ""}${stage.state === "done" ? " stage-done" : ""}`}
          >
            <span className="stage-node" aria-hidden="true">
              {stage.state === "done" ? (
                <svg viewBox="0 0 24 24" focusable="false">
                  <path d="M5 12.5l4.5 4.5L19 7.5" />
                </svg>
              ) : (
                <span className="stage-node-index">{String(index + 1).padStart(2, "0")}</span>
              )}
            </span>
            <span className="stage-name">{stageLabel(stage.name)}</span>
            <ProgressBar
              pct={stage.pct}
              tone={running && stage.state === "active" ? "active" : stage.state === "done" ? "done" : "neutral"}
              size="hairline"
              label={`${stageLabel(stage.name)} progress`}
            />
            <span className="stage-status">{stageStatus(stage)}</span>
          </div>
        ))}
      </div>

      {live ? (
        <div className="panel-section performance-section" aria-label="Performance snapshot">
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
        <div className="panel-section performance-section" aria-label="Performance snapshot">
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
