import { useEffect, useState } from "react";

import type { ApiClient } from "../../api/client";
import type {
  BackendConnectionState,
  ConversionJobStatus,
  HealthResponse,
  ProgressStageView,
} from "../../types/api";
import type { UseLibraryResult } from "../library/useLibrary";
import { ProgressBar } from "../../ui/ProgressBar";
import { Icon } from "../../ui/icons";
import { formatClockWithHours } from "../../format/time";
import { dashboardNowStageLabel } from "../progress/progressTerminalPresentation";
import "./dashboard.css";

interface DashboardPanelProps {
  apiClient?: ApiClient | null;
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

export function DashboardPanel({
  apiClient,
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
  onOpenLibrary,
  onOpenProgress,
  onOpenSettings,
  onOpenLogs,
  cleanerLabel,
  ttsReady,
}: DashboardPanelProps) {
  const [dismissed, setDismissed] = useState(false);
  const [completedBookMissing, setCompletedBookMissing] = useState(false);
  useEffect(() => {
    setDismissed(false);
    setCompletedBookMissing(false);
  }, [apiClient, jobTitle, uiState]);
  useEffect(() => {
    if (uiState !== "completed" || !apiClient) return;
    let cancelled = false;
    void (async () => {
      try {
        const { job } = await apiClient.getActiveConversion();
        if (cancelled || job?.status !== "completed" || job.book_id == null) return;
        try {
          await apiClient.getLibraryBook(job.book_id);
          if (!cancelled) setCompletedBookMissing(false);
        } catch (error) {
          if (!cancelled && (error as { status?: number }).status === 404) setCompletedBookMissing(true);
        }
      } catch {
        // Keep the last job visible while the backend reconnects.
      }
    })();
    return () => { cancelled = true; };
  }, [apiClient, uiState, library.items, library.indexedCount]);
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
  const hasJob = uiState !== "idle" && !dismissed && !completedBookMissing;
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
          : "Not verified";
  const cleanerValue = cleanerLabel?.trim() || "—";
  const backendStatus = normalizedStatus(connectionState, health);
  const backendStatusLabel = backendStatus.toLowerCase() === "ok" ? "OK" : backendStatus;
  const backendTone = connectionState === "error" ? "error" : connectionState === "checking" ? "busy" : "ok";
  const libraryReady = !library.loadingList && !library.loadingDetail && !library.errorMessage;
  const libraryStatus = library.loadingList || library.loadingDetail ? "Loading" : library.errorMessage ? "Error" : "Ready";
  const libraryTone = library.errorMessage ? "error" : libraryReady ? "neutral" : "busy";
  const booksLabel = `${library.indexedCount} ${library.indexedCount === 1 ? "book" : "books"}`;
  const durationDisplay = formatClockWithHours(durationSec > 0 ? durationSec : selectedDurationSec);

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
        <button
          className="dashboard-stat"
          type="button"
          onClick={onOpenLibrary}
          aria-label={`Books: ${library.indexedCount}. Open library`}
        >
          <span className="dashboard-stat-icon" aria-hidden="true">
            <Icon name="library" />
          </span>
          <span className="dashboard-stat-copy">
            <span className="dashboard-stat-label">Books</span>
            <strong className="tabular-nums">{library.indexedCount}</strong>
          </span>
          <Icon name="chevron-right" className="dashboard-stat-chevron" />
        </button>
        <button
          className="dashboard-stat"
          type="button"
          onClick={onOpenSettings}
          aria-label={`TTS engine: ${ttsStatus}. Open settings`}
        >
          <span className="dashboard-stat-icon" aria-hidden="true">
            <Icon name="convert" />
          </span>
          <span className="dashboard-stat-copy">
            <span className="dashboard-stat-label">TTS engine</span>
            <strong className={ttsStatus === "Ready" ? "is-positive" : ""}>{ttsStatus}</strong>
          </span>
          <Icon name="chevron-right" className="dashboard-stat-chevron" />
        </button>
        <button
          className="dashboard-stat"
          type="button"
          onClick={onOpenSettings}
          aria-label={`Cleaner: ${cleanerValue}. Open settings`}
        >
          <span className="dashboard-stat-icon" aria-hidden="true">
            <Icon name="settings" />
          </span>
          <span className="dashboard-stat-copy">
            <span className="dashboard-stat-label">Cleaner</span>
            <strong>{cleanerValue}</strong>
          </span>
          <Icon name="chevron-right" className="dashboard-stat-chevron" />
        </button>
      </section>

      <section
        className={`dashboard-card now-playing-panel${hasPlayback ? "" : " is-empty"}`}
        aria-label="Audiobook overview"
      >
        {!hasPlayback ? (
          <span className="now-playing-icon" aria-hidden="true">
            <Icon name="play" />
          </span>
        ) : null}
        <div className="now-playing-main">
          <span className="card-eyebrow">{playing ? "Now playing" : hasPlayback ? "Continue listening" : "Listening"}</span>
          <h3 className="now-playing-title">
            {hasPlayback ? (selected?.title || playerTitle || "Untitled audiobook") : "Nothing playing"}
          </h3>
          <p className="now-playing-subtitle">
            {hasPlayback
              ? currentChapterLabel || "Ready to listen"
              : "Open a book from your library or convert a source."}
          </p>
          {hasPlayback ? (
            <div className="now-playing-progress">
              <div
                className="playback-progress"
                role="progressbar"
                aria-label="Playback progress"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={playbackPercent}
              >
                <span style={{ width: `${playbackPercent}%` }} />
              </div>
              <div className="now-playing-times tabular-nums">
                <span>{formatClockWithHours(elapsedSec)}</span>
                <span className="now-playing-pct">{playbackPercent}%</span>
                <span>{durationDisplay}</span>
              </div>
              <p className="now-playing-chapters">{chapterText}</p>
            </div>
          ) : null}
        </div>
        {!hasPlayback && onOpenLibrary ? (
          <div className="now-playing-actions">
            <button className="btn" type="button" onClick={onOpenLibrary}>
              <Icon name="library" />
              Open library
            </button>
          </div>
        ) : null}
      </section>

      {hasJob ? (
        <section className="dashboard-card conversion-card" aria-label="Conversion">
          <div className="dashboard-card-header">
            <div className="dashboard-card-copy">
              <span className="card-eyebrow">Conversion</span>
              <h3>{jobTitle || "Current conversion"}</h3>
              <p>{jobAuthor || (conversionIsActive ? "In progress" : "Latest job")}</p>
            </div>
            <div className="dashboard-card-actions">
              {onOpenProgress ? (
                <button className={`dashboard-card-action tabular-nums${uiState === "completed" ? " is-complete" : uiState === "failed" || uiState === "cancelled" ? " is-error" : ""}`} type="button" onClick={onOpenProgress}>
                  {stateLabel} · {conversionPercent}%
                  <Icon name="chevron-right" />
                </button>
              ) : (
                <span className={`dashboard-card-action tabular-nums${uiState === "completed" ? " is-complete" : uiState === "failed" || uiState === "cancelled" ? " is-error" : ""}`}>
                  {stateLabel} · {conversionPercent}%
                </span>
              )}
              {!conversionIsActive ? (
                <button className="btn btn-ghost btn-sm" type="button" aria-label="Dismiss conversion" onClick={() => setDismissed(true)}>
                  ×
                </button>
              ) : null}
            </div>
          </div>
          <ProgressBar
            pct={conversionPercent}
            tone={conversionIsActive ? "active" : uiState === "completed" ? "done" : uiState === "failed" || uiState === "cancelled" ? "danger" : "neutral"}
            size="hairline"
            label="Conversion progress"
          />
          <div className="metric-row metric-row-flow">
            <div className="metric">
              <span className="metric-label">Status</span>
              <strong className="metric-word">{statusTileValue}</strong>
            </div>
            {etaValue ? (
              <div className="metric">
                <span className="metric-label">ETA</span>
                <strong className="tabular-nums">{etaValue}</strong>
              </div>
            ) : null}
          </div>
        </section>
      ) : null}

      <div className="lower-grid">
        <section className="dashboard-card recent-section" aria-label="Recent books">
          <div className="card-header section-title">
            <h3 className="card-title">Recent</h3>
            {onOpenLibrary ? (
              <button className="section-title-action tabular-nums" type="button" onClick={onOpenLibrary}>
                {booksLabel}
                <Icon name="chevron-right" />
              </button>
            ) : (
              <span className="section-title-meta tabular-nums">{booksLabel}</span>
            )}
          </div>
          {recentBooks.length === 0 ? (
            <div className="empty-state dashboard-empty">
              <span className="empty-state-icon" aria-hidden="true"><Icon name="library" /></span>
              <span className="empty-state-title">No converted books yet</span>
              <span className="empty-state-hint">Convert a source to build your library.</span>
            </div>
          ) : (
            <div className="recent-list">
              {recentBooks.map((book) => {
                const pct = book.total_chapters > 0
                  ? clampPercent((book.completed_chapters / book.total_chapters) * 100)
                  : 0;
                const ready = book.completed_chapters >= book.total_chapters;
                const chapterCount = book.total_chapters || 0;
                return (
                  <div className="recent-row" key={book.id}>
                    <div>
                      <strong>{book.title || "Untitled"}</strong>
                      <span>
                        {`${book.author || "Unknown author"} · ${chapterCount} ${chapterCount === 1 ? "chapter" : "chapters"}`}
                      </span>
                    </div>
                    <span className={`recent-state${ready ? "" : " is-partial"}`}>
                      {ready ? "Ready" : "Partial"}
                    </span>
                    {!ready ? (
                      <ProgressBar
                        pct={pct}
                        tone="neutral"
                        size="hairline"
                        label={`${book.title || "Book"} completion`}
                      />
                    ) : null}
                  </div>
                );
              })}
            </div>
          )}
        </section>

        <section className="dashboard-card services-section" aria-label="Services">
          <div className="card-header section-title">
            <h3 className="card-title">Services</h3>
          </div>
          <div className="service-list">
            <button
              className="service-row"
              type="button"
              onClick={onOpenLogs}
              aria-label={`Desktop API: ${backendStatusLabel}`}
            >
              <span>
                <StatusDot tone={backendTone} />
                <strong>Desktop API</strong>
                <small>{statusMessage || `Backend ${connectionState}`}</small>
              </span>
              <em className={`service-state is-${backendTone}`}>{backendStatusLabel}</em>
              <Icon name="chevron-right" className="service-chevron" />
            </button>
            <button
              className="service-row"
              type="button"
              onClick={onOpenLibrary}
              aria-label={`Library: ${libraryStatus}`}
            >
              <span>
                <StatusDot tone={libraryTone} />
                <strong>Library</strong>
                <small>
                  {library.statusMessage
                    || (libraryReady
                      ? `${library.indexedCount} ${library.indexedCount === 1 ? "book" : "books"}`
                      : "Library unavailable")}
                </small>
              </span>
              <em className={`service-state is-${libraryTone}`}>{libraryStatus}</em>
              <Icon name="chevron-right" className="service-chevron" />
            </button>
          </div>
        </section>
      </div>
    </section>
  );
}
