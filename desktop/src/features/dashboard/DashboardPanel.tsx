import type {
  BackendConnectionState,
  ConversionJobStatus,
  HealthResponse,
  PerformanceSnapshot,
  ProgressStageView,
} from "../../types/api";
import type { UseLibraryResult } from "../library/useLibrary";
import type { AudioAnalyserHandle } from "../player/useAudioAnalyser";
import { ConversionFlowCanvas } from "./ConversionFlowCanvas";
import { NowPlayingCanvas } from "./NowPlayingCanvas";
import { ProgressBar } from "../../ui/ProgressBar";
import { formatClockWithHours } from "../../format/time";
import { dashboardNowStageLabel } from "../progress/progressTerminalPresentation";
import "./dashboard.css";

interface DashboardPanelProps {
  hidden: boolean;
  library: UseLibraryResult;
  connectionState: BackendConnectionState;
  health: HealthResponse | null;
  statusMessage: string;
  uiState: ConversionJobStatus;
  progressPct: number;
  etaText: string;
  stages: ProgressStageView[];
  performance: PerformanceSnapshot | null;
  jobTitle?: string;
  jobAuthor?: string;
  currentChapterLabel: string;
  playerTitle: string;
  playing: boolean;
  audioVisualizer: AudioAnalyserHandle;
  elapsedSec: number;
  durationSec: number;
  onStartConvert?: () => void;
}

function clampPercent(value: number): number {
  if (!Number.isFinite(value)) {
    return 0;
  }
  return Math.max(0, Math.min(100, Math.round(value)));
}

function normalizedStatus(state: BackendConnectionState, health: HealthResponse | null): string {
  if (state === "connected" && health?.status) {
    return health.status;
  }
  return state;
}

function conversionStateLabel(uiState: ConversionJobStatus): string {
  switch (uiState) {
    case "running":
      return "Running";
    case "completed":
      return "Complete";
    case "failed":
      return "Failed";
    case "cancelled":
      return "Cancelled";
    default:
      return "Idle";
  }
}

export function DashboardPanel({
  hidden,
  library,
  connectionState,
  health,
  statusMessage,
  uiState,
  progressPct,
  etaText,
  stages,
  jobTitle,
  jobAuthor,
  currentChapterLabel,
  playerTitle,
  playing,
  audioVisualizer,
  elapsedSec,
  durationSec,
  onStartConvert,
}: DashboardPanelProps) {
  const selected = library.selectedBookDetail;
  const recentBooks = library.items.slice(0, 3);
  const nowLabel = dashboardNowStageLabel(uiState, stages);
  const selectedChapterCount = selected?.chapters.length ?? 0;
  const selectedDurationSec =
    selected?.chapters.reduce((total, chapter) => total + Math.max(0, Number(chapter.duration_ms || 0) / 1000), 0) ?? 0;
  const playbackPercent = durationSec > 0 ? clampPercent((elapsedSec / durationSec) * 100) : 0;
  const conversionPercent = clampPercent(progressPct);
  const conversionIsActive = uiState === "running";
  const hasPlayback = playing || durationSec > 0;
  const hasJob =
    uiState === "running" || uiState === "completed" || uiState === "failed" || uiState === "cancelled";
  const chapterText =
    selectedChapterCount > 0
      ? `${selectedChapterCount} ${selectedChapterCount === 1 ? "chapter" : "chapters"}`
      : "No chapters";
  const stateLabel = conversionStateLabel(uiState);
  // Show the live stage while running and the outcome once terminal.
  const statusTileValue = conversionIsActive ? nowLabel : stateLabel;
  // Avoid the "Complete Complete" echo when the stage label restates the state.
  const readoutStage = nowLabel.toLowerCase() === stateLabel.toLowerCase() ? "" : nowLabel;
  const etaValue = etaText ? etaText.replace(/^eta\s*/i, "").trim() : "";
  return (
    <section id="tab-dashboard" className="dashboard-panel" role="tabpanel" aria-label="Dashboard" aria-labelledby="tab-btn-dashboard" hidden={hidden}>
      <section className={`v-section now-playing-panel${hasPlayback ? "" : " is-empty"}`} aria-label="Audiobook overview">
        {hasPlayback ? (
          <>
            <div className="panel-title-row">
              <div>
                <h2 className="panel-title">Now playing</h2>
                <p className="panel-subtitle">
                  {selected?.title ? `${selected.title} — ${currentChapterLabel}` : playerTitle}
                </p>
              </div>
              <span
                className="tiny-badge"
                data-tip="Position in the current chapter"
                data-tip-pos="left"
              >
                {`${playbackPercent}%`}
              </span>
            </div>

            <div className="wave-card" aria-hidden="true">
              <NowPlayingCanvas visualizer={audioVisualizer} playing={playing} active={!hidden} />
            </div>

            <div className="metric-row">
              <div className="metric">
                <span className="metric-label">Elapsed</span>
                <strong>{formatClockWithHours(elapsedSec)}</strong>
              </div>
              <div className="metric">
                <span className="metric-label">Duration</span>
                <strong>
                  {durationSec > 0 ? formatClockWithHours(durationSec) : formatClockWithHours(selectedDurationSec)}
                </strong>
              </div>
              <div className="metric">
                <span className="metric-label">Chapters</span>
                <strong className="metric-word">{chapterText}</strong>
              </div>
            </div>
          </>
        ) : (
          <div className="empty-state">
            <span className="empty-state-title">Nothing playing</span>
            <span className="empty-state-hint">Open a book from your library to start listening.</span>
            {onStartConvert ? (
              <button className="btn btn-primary" type="button" onClick={onStartConvert}>
                Convert a source
              </button>
            ) : null}
          </div>
        )}
      </section>

      {hasJob ? (
        <section className="panel-section v-section">
          <div className="section-title">
            <span>Conversion</span>
            <span className="section-title-meta tabular-nums">{`${stateLabel} · ${conversionPercent}%`}</span>
          </div>
          <div className="library-strip">
            <div className="panel-section v-panel book-panel">
              <div
                className={`conversion-activity${conversionIsActive ? " is-running" : " is-idle"}`}
              >
                <ConversionFlowCanvas
                  active={conversionIsActive}
                  visible={!hidden}
                  className="conversion-activity-canvas"
                />
                <ProgressBar
                  pct={conversionPercent}
                  tone={conversionIsActive ? "active" : "neutral"}
                  size="hairline"
                  label="Conversion progress"
                />
                <div className="conversion-activity-readout">
                  <span>{stateLabel}</span>
                  {readoutStage ? <span>{readoutStage}</span> : null}
                </div>
              </div>
              <div className="book-main">
                <div className="book-heading">
                  <div>
                    <h2 className="book-title">{jobTitle || "Current conversion"}</h2>
                    <p className="book-subtitle">
                      {jobAuthor || (conversionIsActive ? "In progress" : "Latest job")}
                    </p>
                  </div>
                </div>
                <div className="metric-row metric-row-flow">
                  <div className="metric">
                    <span className="metric-label">Status</span>
                    <strong className="metric-word metric-capitalize">{statusTileValue}</strong>
                  </div>
                  {etaValue ? (
                    <div className="metric">
                      <span className="metric-label">ETA</span>
                      <strong>{etaValue}</strong>
                    </div>
                  ) : null}
                </div>
              </div>
            </div>
          </div>
        </section>
      ) : null}

      <div className="lower-grid">
        <section className="panel-section v-section recent-section">
          <div className="section-title">
            <span>Recent</span>
            <span className="section-title-meta tabular-nums">{library.indexedCount} {library.indexedCount === 1 ? "book" : "books"}</span>
          </div>
          {recentBooks.length === 0 ? (
            <div className="empty-state">
              <span className="empty-state-title">No converted books yet</span>
              <span className="empty-state-hint">Convert a source to build your library.</span>
              {onStartConvert ? (
                <button className="btn btn-ghost" type="button" onClick={onStartConvert}>
                  Convert a source
                </button>
              ) : null}
            </div>
          ) : (
            <div className="recent-list">
              {recentBooks.map((book) => {
                const pct =
                  book.total_chapters > 0 ? clampPercent((book.completed_chapters / book.total_chapters) * 100) : 0;
                const ready = book.completed_chapters >= book.total_chapters;
                const chapterCount = book.total_chapters || 0;
                return (
                  <div className={`recent-row${ready ? "" : " is-partial"}`} key={book.id}>
                    <div>
                      <span className="recent-title">{book.title || "Untitled"}</span>
                      <span className="recent-meta">
                        {`${book.author || "Unknown author"} — ${chapterCount} ${chapterCount === 1 ? "chapter" : "chapters"}`}
                      </span>
                    </div>
                    <span
                      className="recent-state"
                      data-tip={ready ? undefined : "Some chapters are missing audio. Open Library to repair."}
                    >
                      {ready ? "Ready" : "Partial"}
                    </span>
                    {!ready ? <ProgressBar pct={pct} tone="neutral" size="hairline" label={`${book.title || "Book"} completion`} /> : null}
                  </div>
                );
              })}
            </div>
          )}
        </section>

        <section className="panel-section v-section services-section">
          <div className="section-title">
            <span>Services</span>
          </div>
          <div className="service-row">
            <div>
              <span className="service-name">Desktop API</span>
              <span className="service-meta">{statusMessage}</span>
            </div>
            <span className="service-status">{normalizedStatus(connectionState, health)}</span>
          </div>
          <div className="service-row">
            <div>
              <span className="service-name">Library</span>
              <span className="service-meta">{library.statusMessage || "Ready"}</span>
            </div>
            <span className="service-status">{library.loadingList || library.loadingDetail ? "loading" : "ready"}</span>
          </div>
        </section>
      </div>
    </section>
  );
}
