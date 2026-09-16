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
  onOpenLibrary?: () => void;
  onOpenProgress?: () => void;
  onOpenSettings?: () => void;
  onOpenLogs?: () => void;
  cleanerLabel?: string;
  ttsReady?: boolean | null;
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

type DashboardIconName = "book" | "pulse" | "cube" | "server";

function DashboardIcon({ name, className = "" }: { name: DashboardIconName; className?: string }) {
  const iconClass = className ? `dashboard-icon ${className}` : "dashboard-icon";
  if (name === "book") {
    return (
      <svg className={iconClass} viewBox="0 0 24 24" focusable="false" aria-hidden="true">
        <path d="M4 5.5c2.5-.8 5-.4 8 1.2v12.8c-3-1.6-5.5-2-8-1.2z" />
        <path d="M20 5.5c-2.5-.8-5-.4-8 1.2v12.8c3-1.6 5.5-2 8-1.2z" />
        <path d="M12 6.7v12.8" />
      </svg>
    );
  }
  if (name === "pulse") {
    return (
      <svg className={iconClass} viewBox="0 0 24 24" focusable="false" aria-hidden="true">
        <path d="M2.5 12h4l2.2-6 4.2 12 2.2-6h6.4" />
      </svg>
    );
  }
  if (name === "cube") {
    return (
      <svg className={iconClass} viewBox="0 0 24 24" focusable="false" aria-hidden="true">
        <path d="m12 3 8 4.5v9L12 21l-8-4.5v-9z" />
        <path d="m4 7.5 8 4.7 8-4.7M12 12.2V21" />
      </svg>
    );
  }
  return (
    <svg className={iconClass} viewBox="0 0 24 24" focusable="false" aria-hidden="true">
      <rect x="4" y="5" width="16" height="6" rx="1.5" />
      <rect x="4" y="14" width="16" height="5" rx="1.5" />
      <path d="M8 8h.01M8 16.5h.01" />
    </svg>
  );
}

function ChevronIcon() {
  return (
    <svg className="dashboard-chevron" viewBox="0 0 12 20" focusable="false" aria-hidden="true">
      <path d="m3 2 6 8-6 8" />
    </svg>
  );
}

function ArrowIcon() {
  return (
    <svg className="dashboard-arrow" viewBox="0 0 18 18" focusable="false" aria-hidden="true">
      <path d="M2.5 9h12" />
      <path d="m10 4.5 4.5 4.5-4.5 4.5" />
    </svg>
  );
}

function DashboardStatCard({
  icon,
  value,
  label,
  ariaLabel,
  onClick,
  statusClass,
}: {
  icon: DashboardIconName;
  value: string | number;
  label: string;
  ariaLabel: string;
  onClick?: () => void;
  statusClass?: string;
}) {
  const className = `dashboard-stat${onClick ? " is-clickable" : ""}`;
  const accessibleName = `${label}: ${value}. ${ariaLabel}`;
  const content = (
    <>
      <DashboardIcon name={icon} className="dashboard-stat-icon" />
      <span className="dashboard-stat-copy">
        <strong className={statusClass ? `dashboard-stat-value ${statusClass}` : "dashboard-stat-value"}>{value}</strong>
        <span className="dashboard-stat-label">{label}</span>
      </span>
      <ChevronIcon />
    </>
  );

  return onClick ? (
    <button className={className} type="button" aria-label={accessibleName} onClick={onClick}>
      {content}
    </button>
  ) : (
    <div className={className} aria-label={accessibleName}>
      {content}
    </div>
  );
}

function ServiceRow({
  icon,
  name,
  meta,
  status,
  tone,
  onClick,
}: {
  icon: DashboardIconName;
  name: string;
  meta: string;
  status: string;
  tone: "ok" | "neutral" | "busy" | "error";
  onClick?: () => void;
}) {
  const content = (
    <>
      <DashboardIcon name={icon} className="service-icon" />
      <span className="service-copy">
        <span className="service-name">{name}</span>
        <span className="service-meta">{meta}</span>
      </span>
      <span className="service-state">
        <span className={`status-dot is-${tone}`} aria-hidden="true" />
        <span className="service-status">{status}</span>
        <ChevronIcon />
      </span>
    </>
  );
  return onClick ? (
    <button className="service-row is-clickable" type="button" onClick={onClick} aria-label={`${name}: ${status}`}>
      {content}
    </button>
  ) : (
    <div className="service-row">{content}</div>
  );
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
  const ttsStatus =
    connectionState === "error"
      ? "Offline"
      : connectionState === "checking"
        ? "Checking"
        : ttsReady === true
          ? "Ready"
          : ttsReady === false
            ? "Unavailable"
            : "Checking";
  const ttsStatusClass = ttsStatus === "Ready" ? "is-positive" : ttsStatus === "Unavailable" || ttsStatus === "Offline" ? "is-negative" : "is-pending";
  const cleanerValue = cleanerLabel?.trim() || "—";
  const backendStatus = normalizedStatus(connectionState, health);
  const backendStatusLabel = backendStatus.toLowerCase() === "ok" ? "OK" : backendStatus;
  const backendTone = connectionState === "error" ? "error" : connectionState === "checking" ? "busy" : "ok";
  const libraryReady = !library.loadingList && !library.loadingDetail && !library.errorMessage;
  const libraryStatus = library.loadingList || library.loadingDetail ? "Loading" : library.errorMessage ? "Error" : "Ready";
  const libraryTone = library.errorMessage ? "error" : libraryReady ? "neutral" : "busy";
  return (
    <section id="tab-dashboard" className="dashboard-panel" role="tabpanel" aria-label="Dashboard" aria-labelledby="tab-btn-dashboard" hidden={hidden}>
      <section className="dashboard-stats" aria-label="Dashboard overview">
        <DashboardStatCard
          icon="book"
          value={library.indexedCount}
          label="Books"
          ariaLabel="Open library"
          onClick={onOpenLibrary}
        />
        <DashboardStatCard
          icon="pulse"
          value={ttsStatus}
          label="TTS Engine"
          ariaLabel="Open TTS settings"
          onClick={onOpenSettings}
          statusClass={ttsStatusClass}
        />
        <DashboardStatCard
          icon="cube"
          value={cleanerValue}
          label="Cleaner"
          ariaLabel="Open cleaner settings"
          onClick={onOpenSettings}
        />
      </section>

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
            <span className="empty-state-hint">Convert a source or open a book from your library.</span>
            <div className="empty-state-actions">
              {onStartConvert ? <button className="btn btn-primary" type="button" onClick={onStartConvert}>Convert a source</button> : null}
              {onOpenLibrary ? <button className="btn dashboard-secondary-action" type="button" onClick={onOpenLibrary}>Open library</button> : null}
            </div>
          </div>
        )}
      </section>

      {hasJob ? (
        <section className="panel-section v-section">
          <div className="section-title">
            <span>Conversion</span>
            {onOpenProgress ? (
              <button className="section-title-action tabular-nums" type="button" onClick={onOpenProgress}>
                {`${stateLabel} · ${conversionPercent}%`}<ChevronIcon />
              </button>
            ) : <span className="section-title-meta tabular-nums">{`${stateLabel} · ${conversionPercent}%`}</span>}
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
            {onOpenLibrary ? (
              <button className="section-title-action tabular-nums" type="button" onClick={onOpenLibrary}>
                {library.indexedCount} {library.indexedCount === 1 ? "book" : "books"}<ChevronIcon />
              </button>
            ) : (
              <span className="section-title-meta tabular-nums">{library.indexedCount} {library.indexedCount === 1 ? "book" : "books"}</span>
            )}
          </div>
          {recentBooks.length === 0 ? (
            <div className="empty-state recent-empty-card">
              <span className="empty-state-icon" aria-hidden="true"><DashboardIcon name="book" /></span>
              <span className="empty-state-title">No converted books yet</span>
              <span className="empty-state-hint">Convert a source to build your library.</span>
              {onStartConvert ? (
                <button className="btn btn-ghost dashboard-link-action" type="button" onClick={onStartConvert}>
                  Convert a source<ArrowIcon />
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
          <div className="service-list">
            <ServiceRow
              icon="server"
              name="Desktop API"
              meta={statusMessage || `Backend ${connectionState}`}
              status={backendStatusLabel}
              tone={backendTone}
              onClick={onOpenLogs}
            />
            <ServiceRow
              icon="book"
              name="Library"
              meta={library.statusMessage || (libraryReady ? (library.indexedCount > 0 ? `${library.indexedCount} ${library.indexedCount === 1 ? "book" : "books"}` : "Library empty") : "Library unavailable")}
              status={libraryStatus}
              tone={libraryTone}
              onClick={onOpenLibrary}
            />
          </div>
        </section>
      </div>
    </section>
  );
}
