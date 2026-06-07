import type { ConversionJobStatus, PerformanceSnapshot, ProgressStageView } from "../../types/api";
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

function formatMetric(value: number | undefined, suffix = ""): string {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "--";
  }
  const formatted = value >= 100 ? Math.round(value).toString() : value.toFixed(value >= 10 ? 1 : 2);
  return suffix ? `${formatted} ${suffix}` : formatted;
}

function formatSeconds(value: number | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "--";
  }
  const total = Math.max(0, Math.round(value));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`;
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
  const activeStageName = stageLabel(activeStage?.name || currentStage || "waiting");
  const stageDetails = activeStage?.detail ?? [];
  const activeTitles = performance?.active_chapter_titles ?? [];
  const progressFocusClass = uiState === "cancelled" ? "panel-section progress-focus is-canceled" : "panel-section progress-focus";
  const workerText =
    typeof performance?.max_workers === "number"
      ? `${performance.active_workers ?? 0}/${performance.max_workers}`
      : "--";
  const requestedParallelText =
    typeof performance?.requested_parallel_chapters === "number"
      ? `requested ${performance.requested_parallel_chapters}`
      : "requested --";
  const effectiveParallelText =
    typeof performance?.effective_parallel_chapters === "number"
      ? `effective ${performance.effective_parallel_chapters}`
      : "effective --";
  const selectedWorkerMode = performance?.parallel_worker_mode || "waiting";
  const queueText = `${performance?.running_chapters ?? 0} running / ${performance?.queued_chapters ?? 0} queued`;
  const throughputText = [
    `${formatMetric(performance?.cleaner_chunks_per_second, "chunk/s")} clean`,
    `${formatMetric(performance?.tts_chars_per_second, "char/s")} tts`,
    `${formatMetric(performance?.encoder_chapters_per_second, "chapter/s")} encode`,
  ].join(" · ");

  return (
    <>
      <div className={progressFocusClass}>
        <div className="progress-focus-header">
          <div>
            <span className="progress-kicker">current operation</span>
            <h2 className="progress-title">{activeStageName}</h2>
          </div>
          <span id="progress-pct" className="progress-percent">{`${boundedProgress}%`}</span>
        </div>

        <div id="progress-overall-bar" className="progress-overall">
          <ProgressBar pct={boundedProgress} active={uiState === "running"} label="overall conversion progress" />
        </div>

        <div className="progress-focus-meta">
          <span id="status-text">{statusText || uiState}</span>
          <span id="eta-text">{etaText || "eta: --:--"}</span>
        </div>

        <div className="subtask-list" aria-label="Active stage detail">
          {stageDetails.length > 0 ? (
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
              <ProgressBar pct={activeStage?.pct ?? 0} active={uiState === "running"} label="active task progress" />
              <span className="subtask-value">{currentMessage || "waiting for updates"}</span>
            </div>
          )}
        </div>
      </div>

      <div className="panel-section performance-section" aria-label="Performance snapshot">
        <div className="performance-grid">
          <div className="performance-item">
            <span className="performance-label">workers</span>
            <strong>{workerText}</strong>
            <span>{`${requestedParallelText} / ${effectiveParallelText} · ${queueText}`}</span>
          </div>
          <div className="performance-item">
            <span className="performance-label">throughput</span>
            <strong>{throughputText}</strong>
            <span>{`elapsed ${formatSeconds(performance?.elapsed_seconds)}`}</span>
          </div>
          <div className="performance-item">
            <span className="performance-label">active chapters</span>
            <strong>{activeTitles.length > 0 ? activeTitles.slice(0, 3).join(", ") : "none"}</strong>
            <span>{`${performance?.completed_chapters ?? 0} done / ${performance?.failed_chapters ?? 0} failed`}</span>
          </div>
          <div className="performance-item">
            <span className="performance-label">mode</span>
            <strong>{`${selectedWorkerMode} / ${performance?.pipeline_mode || "waiting"}`}</strong>
            <span>{performance?.fallback_reason ? `fallback: ${performance.fallback_reason}` : `eta ${formatSeconds(performance?.eta_seconds)}`}</span>
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
