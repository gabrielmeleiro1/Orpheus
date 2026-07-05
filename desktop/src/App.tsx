import { useEffect, useRef, useState } from "react";

import { useBackendClient } from "./app/useBackendClient";
import { useGlobalShortcuts } from "./app/useGlobalShortcuts";
import { useThemeMode } from "./app/useThemeMode";
import { ConversionPanel } from "./features/conversion/ConversionPanel";
import { useConversionJob } from "./features/conversion/useConversionJob";
import { DashboardPanel } from "./features/dashboard/DashboardPanel";
import { LiveClock } from "./features/dashboard/LiveClock";
import { LibraryPanel } from "./features/library/LibraryPanel";
import { useLibrary } from "./features/library/useLibrary";
import { LogPanel } from "./features/logs/LogPanel";
import { PlayerPanel } from "./features/player/PlayerPanel";
import { useAudioPlayer } from "./features/player/useAudioPlayer";
import { ProgressPanel } from "./features/progress/ProgressPanel";
import {
  DASHBOARD_TAB_CONFIGS,
  DashboardTabIcon,
  conversionPanelTabForDashboardTab,
} from "./navigation/tabs";
import type { DashboardTab } from "./types/api";

export { nextBrowserHealthState, resolveBackendMonitorFailure } from "./app/useBackendClient";

function App() {
  const [activeTab, setActiveTab] = useState<DashboardTab>("dashboard");
  const { themeMode, setThemeMode } = useThemeMode();
  const {
    apiClient,
    backendBaseUrl,
    connectionState,
    health,
    statusMessage,
  } = useBackendClient();
  const startCommandRef = useRef<(() => void) | null>(null);

  const library = useLibrary({
    apiClient,
    backendDisconnected: connectionState === "error",
  });

  const player = useAudioPlayer({ apiClient, library });
  const previousUiStateRef = useRef<string>("idle");

  const conversionJob = useConversionJob({
    apiClient,
    baseUrl: backendBaseUrl,
  });
  const refreshLibrary = library.refreshLibrary;

  useEffect(() => {
    const previous = previousUiStateRef.current;
    previousUiStateRef.current = conversionJob.uiState;

    if (previous !== "running" && conversionJob.uiState === "running") {
      setActiveTab("progress");
      return;
    }

    if (previous !== "completed" && conversionJob.uiState === "completed") {
      void refreshLibrary();
      setActiveTab("library");
    }
  }, [conversionJob.uiState, refreshLibrary]);

  const { playRelative, seekBy, seekTo, togglePlay } = player;
  useGlobalShortcuts({
    activeTab,
    startCommandRef,
    refreshLibrary,
    playRelative,
    seekBy,
    togglePlay,
  });

  const isConversionPanelVisible = activeTab === "convert" || activeTab === "document" || activeTab === "settings";
  const conversionPanelTab = conversionPanelTabForDashboardTab(activeTab);

  return (
    <main className="app-frame" role="application" aria-label="Orpheus dashboard">
      <aside className="rail" aria-label="Orpheus navigation">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">A</span>
          <span className="brand-name">Orpheus</span>
        </div>

        <nav className="rail-nav" role="tablist" aria-label="Dashboard sections">
          {DASHBOARD_TAB_CONFIGS.map(({ id, label, panelId }) => {
            const isActive = activeTab === id;
            return (
              <button
                id={`tab-btn-${id}`}
                key={id}
                className={`rail-tab${isActive ? " active" : ""}`}
                type="button"
                role="tab"
                aria-controls={panelId}
                aria-label={label}
                aria-selected={isActive}
                onClick={() => setActiveTab(id)}
              >
                <span className="rail-icon" aria-hidden="true"><DashboardTabIcon tab={id} /></span>
                <span className="rail-label">{label}</span>
              </button>
            );
          })}
        </nav>

        <div className="rail-bottom">
          <button
            className="rail-action stop"
            type="button"
            disabled={!conversionJob.activeJobId || conversionJob.isCancelling}
            onClick={() => void conversionJob.cancelActiveConversion()}
          >
            <span className="rail-dot" aria-hidden="true" />
            <span>{conversionJob.isCancelling ? "Stopping" : "Stop job"}</span>
          </button>
          <button
            id="theme-toggle"
            className="rail-action"
            type="button"
            onClick={() => setThemeMode((mode) => (mode === "dark" ? "light" : "dark"))}
          >
            <span className="rail-action-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" focusable="false">
                <path d="M12 4v3M12 17v3M4 12h3M17 12h3" />
                <path d="M7.5 7.5l2 2M14.5 14.5l2 2M16.5 7.5l-2 2M9.5 14.5l-2 2" />
              </svg>
            </span>
            <span>{themeMode === "dark" ? "Light mode" : "Dark mode"}</span>
          </button>
          <button className="rail-action" type="button" onClick={() => void refreshLibrary()}>
            <span className="rail-action-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" focusable="false">
                <path d="M20 6v5h-5" />
                <path d="M4 18v-5h5" />
                <path d="M18.5 9A7 7 0 0 0 6.2 6.3L4 8.5" />
                <path d="M5.5 15A7 7 0 0 0 17.8 17.7L20 15.5" />
              </svg>
            </span>
            <span>Refresh</span>
          </button>
        </div>
      </aside>

      <div className="workspace-shell">
        <header className="studio-status">
          <div>
            <h1>orpheus.local</h1>
            <p>{`local / mlx / ${library.items.length} books indexed`}</p>
          </div>
          <div className="studio-controls">
            <span className={`status-dot${connectionState === "connected" ? " is-connected" : ""}`} title={connectionState} aria-hidden="true" />
            <span className="sr-only" role="status">{`backend ${connectionState}`}</span>
            <span className="control clock-control"><LiveClock /></span>
          </div>
        </header>

        <div className="main-workspace">
          <DashboardPanel
            hidden={activeTab !== "dashboard"}
            library={library}
            connectionState={connectionState}
            health={health}
            statusMessage={statusMessage}
            uiState={conversionJob.uiState}
            progressPct={conversionJob.progressPct}
            etaText={conversionJob.etaText}
            stages={conversionJob.stages}
            performance={conversionJob.performanceSnapshot}
            currentChapterLabel={player.currentChapterLabel}
            playerTitle={player.title}
            playing={player.playing}
            audioVisualizer={player.audioVisualizer}
            elapsedSec={player.elapsedSec}
            durationSec={player.durationSec}
          />

          <ConversionPanel
            activeTab={conversionPanelTab}
            hidden={!isConversionPanelVisible}
            apiClient={apiClient}
            connectionState={connectionState}
            health={health}
            statusMessage={statusMessage}
            conversionJob={conversionJob}
            startCommandRef={startCommandRef}
          />

          <section
            id="tab-progress"
            className="panel"
            role="tabpanel"
            aria-label="Conversion progress"
            hidden={activeTab !== "progress"}
          >
            <div className="panel-header">
              <span>progress</span>
              <span className="panel-header-meta">{conversionJob.uiState} · {Math.round(conversionJob.progressPct)}%</span>
            </div>
            <ProgressPanel
              uiState={conversionJob.uiState}
              statusText={conversionJob.statusText}
              progressPct={conversionJob.progressPct}
              etaText={conversionJob.etaText}
              stages={conversionJob.stages}
              currentStage={conversionJob.currentStage}
              currentMessage={conversionJob.currentMessage}
              performance={conversionJob.performanceSnapshot}
              activeJobId={conversionJob.activeJobId}
              isCancelling={conversionJob.isCancelling}
              onCancel={conversionJob.cancelActiveConversion}
            />
          </section>

          <section
            id="tab-logs"
            className="panel logs-panel"
            role="tabpanel"
            aria-label="Process logs"
            hidden={activeTab !== "logs"}
          >
            <LogPanel logs={conversionJob.logs} />
          </section>

          <LibraryPanel
            hidden={activeTab !== "library"}
            library={library}
            currentChapterNumber={player.currentChapter?.number ?? null}
            onJumpToChapter={(chapter) => {
              void player.jumpToChapter(chapter);
            }}
          />
        </div>

        <PlayerPanel
          enabled={player.playerEligible}
          visible={player.playerVisible}
          playing={player.playing}
          title={player.title}
          statusText={player.statusText}
          elapsedSec={player.elapsedSec}
          durationSec={player.durationSec}
          volume={player.volume}
          canGoPrevious={player.canGoPrevious}
          canGoNext={player.canGoNext}
          audioRef={player.audioRef}
          audio={player.audioProps}
          onTogglePlay={() => {
            void togglePlay();
          }}
          onSeekBy={seekBy}
          onSeekTo={seekTo}
          onPrevious={() => {
            void playRelative(-1);
          }}
          onNext={() => {
            void playRelative(1);
          }}
          onVolumeChange={player.setPlayerVolume}
          onClose={player.closePlayer}
        />
      </div>
    </main>
  );
}

export default App;
