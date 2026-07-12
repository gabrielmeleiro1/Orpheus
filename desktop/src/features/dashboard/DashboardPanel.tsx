import type { CSSProperties } from "react";
import type {
  BackendConnectionState,
  ConversionJobStatus,
  HealthResponse,
  PerformanceSnapshot,
  ProgressStageView,
} from "../../types/api";
import type { UseLibraryResult } from "../library/useLibrary";
import type { AudioAnalyserHandle } from "../player/useAudioAnalyser";
import { AudioWaveformCanvas } from "./AudioWaveformCanvas";
import { GlyphActivity } from "../../ui/ascii/GlyphActivity";
import { formatClockWithHours, formatDurationClockWithHours } from "../../format/time";
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

function stageName(stage: ProgressStageView | undefined): string {
  return String(stage?.name || "waiting").replace(/_/g, " ");
}

function progressWidth(value: number): { width: string } {
  return { width: `${clampPercent(value)}%` };
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
  performance,
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
  const completedStageCount = stages.filter((stage) => stage.state === "done").length;
  const nowLabel = dashboardNowStageLabel(uiState, stages);
  const selectedChapterCount = selected?.chapters.length ?? 0;
  const selectedDurationSec =
    selected?.chapters.reduce((total, chapter) => total + Math.max(0, Number(chapter.duration_ms || 0) / 1000), 0) ?? 0;
  const playbackPercent = durationSec > 0 ? clampPercent((elapsedSec / durationSec) * 100) : 0;
  const conversionPercent = clampPercent(progressPct);
  const conversionIsActive = uiState === "running";
  const conversionActivityStyle = {
    "--conversion-activity-progress": `${conversionPercent}%`,
  } as CSSProperties;
  const queueText =
    uiState === "running" && typeof performance?.max_workers === "number"
      ? `${performance.active_workers ?? 0}/${performance.max_workers} workers`
      : uiState === "running"
        ? `${performance?.queued_chapters ?? 0} queued`
        : "idle";
  const chapterText =
    selectedChapterCount > 0
      ? `${selectedChapterCount} ${selectedChapterCount === 1 ? "chapter" : "chapters"}`
      : "no chapters selected";
  const showEmptyLibraryAction = recentBooks.length === 0 && Boolean(onStartConvert);
  return (
    <section id="tab-dashboard" className="dashboard-panel" role="tabpanel" aria-label="Dashboard" hidden={hidden}>
      <section className="panel-section v-section dashboard-hero" aria-label="Audiobook overview">
        <div className="section-title">
          <span>ORPHEUS</span>
        </div>
        <div className="primary-grid">
          <div className="panel-section v-panel now-playing-panel">
            <div className="panel-title-row">
              <div>
                <h2 className="panel-title">Now Playing</h2>
                <p className="panel-subtitle">
                  {selected?.title ? `${selected.title} / ${currentChapterLabel}` : playerTitle}
                </p>
              </div>
              <span className="tiny-badge">{`${playbackPercent}%`}</span>
            </div>

            <div className="wave-card" aria-hidden="true">
              <div className="wave-grid">
                <AudioWaveformCanvas visualizer={audioVisualizer} playing={playing} active={!hidden} />
              </div>
            </div>

            <div className="metric-row">
              <div className="metric">
                <span className="metric-label">elapsed</span>
                <strong>{formatClockWithHours(elapsedSec)}</strong>
              </div>
              <div className="metric">
                <span className="metric-label">duration</span>
                <strong>{durationSec > 0 ? formatClockWithHours(durationSec) : formatClockWithHours(selectedDurationSec)}</strong>
              </div>
              <div className="metric">
                <span className="metric-label">chapters</span>
                <strong>{chapterText}</strong>
              </div>
            </div>
          </div>

          <div className="side-stack">
            <div className="panel-section v-panel status-card">
              <span className="status-label">Playback</span>
              <strong className="status-number">{`${playbackPercent}%`}</strong>
              <span className="status-detail">{selected?.title || "select a playable book"}</span>
            </div>
            <div className="panel-section v-panel status-card">
              <span className="status-label">Queue</span>
              <strong className="status-number">{uiState === "running" ? "1 job" : "idle"}</strong>
              <span className="status-detail">{queueText}</span>
            </div>
            <div className="panel-section v-panel status-card">
              <span className="status-label">Library</span>
              <strong className="status-number">{library.items.length}</strong>
              <span className="status-detail">{`${library.items.length === 1 ? "book" : "books"} indexed`}</span>
            </div>
          </div>
        </div>
      </section>

      <section className="panel-section v-section">
        <div className="section-title">
          <span>CONVERSION</span>
          <span>{`${uiState} · ${conversionPercent}%`}</span>
        </div>
        <div className="library-strip">
          <div className="panel-section v-panel book-panel">
            <div
              className={`conversion-activity${conversionIsActive ? " is-running" : " is-idle"}`}
              style={conversionActivityStyle}
              aria-hidden="true"
            >
              <div className="conversion-activity-glyphs">
                <GlyphActivity active={conversionIsActive} />
              </div>
              <div className="conversion-activity-progress">
                <span className="conversion-activity-progress-fill" />
              </div>
              <div className="conversion-activity-readout">
                <span>{conversionIsActive ? "run" : "idle"}</span>
                <span>{nowLabel}</span>
              </div>
            </div>
            <div className="book-main">
              <div className="book-heading">
                <div>
                  <h2 className="book-title">{selected?.title || "No book selected"}</h2>
                  <p className="book-subtitle">{selected ? `${selected.author || "unknown author"} / ${chapterText}` : "library idle"}</p>
                </div>
                <span className="tiny-badge">{`${conversionPercent}%`}</span>
              </div>
              <div className="chapter-lines" aria-hidden="true">
                <span />
                <span />
                <span />
                <span />
              </div>
              <div className="metric-row">
                <div className="metric">
                  <span className="metric-label">now</span>
                  <strong>{nowLabel}</strong>
                </div>
                <div className="metric">
                  <span className="metric-label">eta</span>
                  <strong>{etaText || "--"}</strong>
                </div>
                <div className="metric">
                  <span className="metric-label">state</span>
                  <strong>{uiState}</strong>
                </div>
              </div>
            </div>
          </div>

          <div className="panel-section v-panel pipeline-panel">
            <div className="pipeline-head">Pipeline</div>
            {stages.map((stage) => (
              <div className="pipeline-row" key={stage.name}>
                <div>
                  <span className="pipeline-name">{stageName(stage)}</span>
                  <span className="pipeline-meta">{stage.state}</span>
                </div>
                <div className="dashboard-bar">
                  <span style={progressWidth(stage.pct)} />
                </div>
                <span className="percent">{`${clampPercent(stage.pct)}%`}</span>
              </div>
            ))}
          </div>
        </div>
      </section>

      <div className="lower-grid">
        <section className="panel-section v-section recent-section">
          <div className="section-title">
            <span>RECENT LIBRARY</span>
          </div>
          <div className="recent-list">
            {recentBooks.length === 0 ? (
              <div className="recent-row">
                <span className="square" />
                <div>
                  <span className="recent-title">No converted books yet</span>
                  <span className="recent-meta">
                    {showEmptyLibraryAction ? "convert a source to populate the library" : "drop a source in Convert to begin"}
                  </span>
                  {showEmptyLibraryAction ? (
                    <button className="btn btn-compact" type="button" onClick={onStartConvert}>
                      Convert a source
                    </button>
                  ) : null}
                </div>
                <span className="recent-state">--:--</span>
                <span className="recent-state">empty</span>
                <div className="dashboard-bar"><span style={progressWidth(0)} /></div>
              </div>
            ) : (
              recentBooks.map((book) => {
                const pct =
                  book.total_chapters > 0 ? clampPercent((book.completed_chapters / book.total_chapters) * 100) : 0;
                return (
                  <div className="recent-row" key={book.id}>
                    <span className="square" />
                    <div>
                      <span className="recent-title">{book.title || "untitled"}</span>
                      <span className="recent-meta">{`${book.author || "unknown"} / ${book.total_chapters || 0} chapters`}</span>
                    </div>
                    <span className="recent-state">{formatDurationClockWithHours(selected?.id === book.id ? selectedDurationSec * 1000 : null)}</span>
                    <span className="recent-state">{book.completed_chapters >= book.total_chapters ? "ready" : "partial"}</span>
                    <div className="dashboard-bar"><span style={progressWidth(pct)} /></div>
                  </div>
                );
              })
            )}
          </div>
        </section>

        <section className="panel-section v-section services-section">
          <div className="section-title">
            <span>SERVICES</span>
          </div>
          <div className="service-group">Backends</div>
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
              <span className="service-meta">{library.statusMessage || "library idle"}</span>
            </div>
            <span className="service-status">{library.loadingList || library.loadingDetail ? "loading" : "ready"}</span>
          </div>
          <div className="service-row">
            <div>
              <span className="service-name">Pipeline</span>
              <span className="service-meta">{`${completedStageCount}/${stages.length} stages complete`}</span>
            </div>
            <span className="service-status">{uiState}</span>
          </div>
        </section>
      </div>
    </section>
  );
}
