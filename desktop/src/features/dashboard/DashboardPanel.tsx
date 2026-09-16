import type {
  BackendConnectionState,
  ConversionJobStatus,
  HealthResponse,
  ProgressStageView,
} from "../../types/api";
import type { UseLibraryResult } from "../library/useLibrary";
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
  jobTitle?: string;
  jobAuthor?: string;
  currentChapterLabel: string;
  playerTitle: string;
  playing: boolean;
  elapsedSec: number;
  durationSec: number;
  onStartConvert?: () => void;
  onOpenLibrary?: () => void;
  onOpenProgress?: () => void;
  onOpenSettings?: () => void;
  onOpenLogs?: () => void;
  cleanerLabel?: string;
  ttsReady?: boolean | null;
}

function clampPercent(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, Math.round(value)));
}

function normalizedStatus(state: BackendConnectionState, health: HealthResponse | null): string {
  if (state === "connected" && health?.status) return health.status;
  return state;
}

function conversionStateLabel(uiState: ConversionJobStatus): string {
  switch (uiState) {
    case "running": return "Running";
    case "completed": return "Complete";
    case "failed": return "Failed";
    case "cancelled": return "Cancelled";
    default: return "Idle";
  }
}

function StatusDot({ tone }: { tone: "ok" | "neutral" | "busy" | "error" }) {
  return <span className={`status-dot is-${tone}`} aria-hidden="true" />;
}

function Chevron() {
  return <span className="dashboard-chevron" aria-hidden="true">›</span>;
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
  elapsedSec,
  durationSec,
  onStartConvert,
  onOpenLibrary,
  onOpenProgress,
  onOpenSettings,
  onOpenLogs,
  cleanerLabel,
  ttsReady,
}: DashboardPanelProps) {
  const selected = library.selectedBookDetail;
  const recentBooks = library.items.slice(0, 3);
  const nowLabel = dashboardNowStageLabel(uiState, stages);
  const selectedChapterCount = selected?.chapters.length ?? 0;
  const selectedDurationSec = selected?.chapters.reduce(
    (total, chapter) => total + Math.max(0, Number(chapter.duration_ms || 0) / 1000),
    0,
  ) ?? 0;
  const playbackPercent = durationSec > 0 ? clampPercent((elapsedSec / durationSec) * 100) : 0;
  const conversionPercent = clampPercent(progressPct);
  const conversionIsActive = uiState === "running";
  const hasPlayback = playing || durationSec > 0;
  const hasJob = uiState !== "idle";
  const chapterText = selectedChapterCount > 0
    ? `${selectedChapterCount} ${selectedChapterCount === 1 ? "chapter" : "chapters"}`
    : "No chapters";
  const stateLabel = conversionStateLabel(uiState);
  const statusTileValue = conversionIsActive ? nowLabel : stateLabel;
  const etaValue = etaText ? etaText.replace(/^eta\s*/i, "").trim() : "";
  const ttsStatus = connectionState === "error"
    ? "Offline"
    : connectionState === "checking"
      ? "Checking"
      : ttsReady === true
        ? "Ready"
        : ttsReady === false
          ? "Unavailable"
          : "Checking";
  const cleanerValue = cleanerLabel?.trim() || "—";
  const backendStatus = normalizedStatus(connectionState, health);
  const backendStatusLabel = backendStatus.toLowerCase() === "ok" ? "OK" : backendStatus;
  const backendTone = connectionState === "error" ? "error" : connectionState === "checking" ? "busy" : "ok";
  const libraryReady = !library.loadingList && !library.loadingDetail && !library.errorMessage;
  const libraryStatus = library.loadingList || library.loadingDetail ? "Loading" : library.errorMessage ? "Error" : "Ready";
  const libraryTone = library.errorMessage ? "error" : libraryReady ? "neutral" : "busy";

  return (
    <section
      id="tab-dashboard"
      className="dashboard-panel"
      role="tabpanel"
      aria-label="Dashboard"
      aria-labelledby="tab-btn-dashboard"
      hidden={hidden}
    >
      <section className="dashboard-stats" aria-label="Dashboard overview">
        <button className="dashboard-stat" type="button" onClick={onOpenLibrary} aria-label={`Books: ${library.indexedCount}. Open library`}>
          <span className="dashboard-stat-copy"><strong>{library.indexedCount}</strong><span>Books</span></span>
          <Chevron />
        </button>
        <button className="dashboard-stat" type="button" onClick={onOpenSettings} aria-label={`TTS engine: ${ttsStatus}. Open settings`}>
          <span className="dashboard-stat-copy"><strong className={ttsStatus === "Ready" ? "is-positive" : ""}>{ttsStatus}</strong><span>TTS engine</span></span>
          <Chevron />
        </button>
        <button className="dashboard-stat" type="button" onClick={onOpenSettings} aria-label={`Cleaner: ${cleanerValue}. Open settings`}>
          <span className="dashboard-stat-copy"><strong>{cleanerValue}</strong><span>Cleaner</span></span>
          <Chevron />
        </button>
      </section>

      <section className={`dashboard-card now-playing-panel${hasPlayback ? "" : " is-empty"}`} aria-label="Audiobook overview">
        <div className="dashboard-card-header">
          <div>
            <span className="dashboard-card-kicker">Now playing</span>
            <h3>{hasPlayback ? (selected?.title || playerTitle || "Untitled audiobook") : "Nothing playing"}</h3>
            <p>{hasPlayback ? currentChapterLabel || "Ready to listen" : "Open a book from your library or convert a source."}</p>
          </div>
          {hasPlayback ? <span className="dashboard-percent tabular-nums">{playbackPercent}%</span> : null}
        </div>
        {hasPlayback ? (
          <>
            <div className="playback-progress" role="progressbar" aria-label="Playback progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={playbackPercent}>
              <span style={{ width: `${playbackPercent}%` }} />
            </div>
            <div className="metric-row">
              <div className="metric"><span className="metric-label">Elapsed</span><strong>{formatClockWithHours(elapsedSec)}</strong></div>
              <div className="metric"><span className="metric-label">Duration</span><strong>{formatClockWithHours(durationSec > 0 ? durationSec : selectedDurationSec)}</strong></div>
              <div className="metric"><span className="metric-label">Chapters</span><strong className="metric-word">{chapterText}</strong></div>
            </div>
          </>
        ) : (
          <div className="empty-state-actions">
            {onStartConvert ? <button className="btn btn-primary" type="button" onClick={onStartConvert}>Convert a source</button> : null}
            {onOpenLibrary ? <button className="btn btn-ghost" type="button" onClick={onOpenLibrary}>Open library</button> : null}
          </div>
        )}
      </section>

      {hasJob ? (
        <section className="dashboard-card conversion-card" aria-label="Conversion">
          <div className="dashboard-card-header">
            <div><span className="dashboard-card-kicker">Conversion</span><h3>{jobTitle || "Current conversion"}</h3><p>{jobAuthor || (conversionIsActive ? "In progress" : "Latest job")}</p></div>
            {onOpenProgress ? <button className="dashboard-card-action tabular-nums" type="button" onClick={onOpenProgress}>{stateLabel} · {conversionPercent}% <Chevron /></button> : <span className="dashboard-card-action tabular-nums">{stateLabel} · {conversionPercent}%</span>}
          </div>
          <ProgressBar pct={conversionPercent} tone={conversionIsActive ? "active" : "neutral"} size="hairline" label="Conversion progress" />
          <div className="metric-row metric-row-flow">
            <div className="metric"><span className="metric-label">Status</span><strong className="metric-word">{statusTileValue}</strong></div>
            {etaValue ? <div className="metric"><span className="metric-label">ETA</span><strong>{etaValue}</strong></div> : null}
          </div>
        </section>
      ) : null}

      <div className="lower-grid">
        <section className="dashboard-card recent-section" aria-label="Recent books">
          <div className="section-title"><span>Recent</span>{onOpenLibrary ? <button className="section-title-action tabular-nums" type="button" onClick={onOpenLibrary}>{library.indexedCount} {library.indexedCount === 1 ? "book" : "books"} <Chevron /></button> : <span className="section-title-meta tabular-nums">{library.indexedCount} books</span>}</div>
          {recentBooks.length === 0 ? (
            <div className="empty-state"><span className="empty-state-title">No converted books yet</span><span className="empty-state-hint">Convert a source to build your library.</span>{onStartConvert ? <button className="btn btn-ghost" type="button" onClick={onStartConvert}>Convert a source</button> : null}</div>
          ) : (
            <div className="recent-list">
              {recentBooks.map((book) => {
                const pct = book.total_chapters > 0 ? clampPercent((book.completed_chapters / book.total_chapters) * 100) : 0;
                const ready = book.completed_chapters >= book.total_chapters;
                const chapterCount = book.total_chapters || 0;
                return <div className="recent-row" key={book.id}><div><strong>{book.title || "Untitled"}</strong><span>{`${book.author || "Unknown author"} · ${chapterCount} ${chapterCount === 1 ? "chapter" : "chapters"}`}</span></div><span className={`recent-state${ready ? "" : " is-partial"}`}>{ready ? "Ready" : "Partial"}</span>{!ready ? <ProgressBar pct={pct} tone="neutral" size="hairline" label={`${book.title || "Book"} completion`} /> : null}</div>;
              })}
            </div>
          )}
        </section>

        <section className="dashboard-card services-section" aria-label="Services">
          <div className="section-title"><span>Services</span></div>
          <div className="service-list">
            <button className="service-row" type="button" onClick={onOpenLogs} aria-label={`Desktop API: ${backendStatusLabel}`}><span><StatusDot tone={backendTone} /><strong>Desktop API</strong><small>{statusMessage || `Backend ${connectionState}`}</small></span><em className={`service-state is-${backendTone}`}>{backendStatusLabel}</em><Chevron /></button>
            <button className="service-row" type="button" onClick={onOpenLibrary} aria-label={`Library: ${libraryStatus}`}><span><StatusDot tone={libraryTone} /><strong>Library</strong><small>{library.statusMessage || (libraryReady ? `${library.indexedCount} ${library.indexedCount === 1 ? "book" : "books"}` : "Library unavailable")}</small></span><em className={`service-state is-${libraryTone}`}>{libraryStatus}</em><Chevron /></button>
          </div>
        </section>
      </div>
    </section>
  );
}
