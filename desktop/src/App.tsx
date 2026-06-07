import { useEffect, useRef, useState } from "react";

import {
  createApiClientFromDesktopEndpoint,
  type ApiClient,
} from "./api/client";
import { ConversionPanel } from "./features/conversion/ConversionPanel";
import { useConversionJob } from "./features/conversion/useConversionJob";
import {
  BACKEND_HEALTH_CHECK_INTERVAL_MS,
  THEME_STORAGE_KEY,
  describeDesktopBackendStatus,
  isTauriRuntime,
  readDesktopBackendStatus,
  waitForDesktopEndpoint,
} from "./features/dashboard/dashboardHelpers";
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
import type {
  BackendConnectionState,
  DashboardTab,
  HealthResponse,
  ThemeMode,
} from "./types/api";

function messageFromUnknown(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message.trim()) {
    return error.message;
  }
  if (typeof error === "string" && error.trim()) {
    return error.trim();
  }
  return fallback;
}

function App() {
  const [activeTab, setActiveTab] = useState<DashboardTab>("dashboard");
  const [themeMode, setThemeMode] = useState<ThemeMode>(() => {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    return stored === "light" ? "light" : "dark";
  });
  const [connectionState, setConnectionState] = useState<BackendConnectionState>("checking");
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [statusMessage, setStatusMessage] = useState<string>("resolving backend endpoint");
  const [backendBaseUrl, setBackendBaseUrl] = useState<string>("");
  const [apiClient, setApiClient] = useState<ApiClient | null>(null);

  const library = useLibrary({
    apiClient,
    backendDisconnected: connectionState === "error",
  });

  const player = useAudioPlayer({ apiClient, library });
  const previousUiStateRef = useRef<string>("idle");

  useEffect(() => {
    document.body.setAttribute("data-theme", themeMode);
    window.localStorage.setItem(THEME_STORAGE_KEY, themeMode);
  }, [themeMode]);

  useEffect(() => {
    let cancelled = false;

    const bootstrap = async () => {
      setConnectionState("checking");
      setHealth(null);
      setStatusMessage("resolving backend endpoint");

      if (!isTauriRuntime()) {
        setApiClient(null);
        setBackendBaseUrl("");
        setConnectionState("error");
        setStatusMessage("desktop runtime unavailable; launch the Tauri app");
        return;
      }

      try {
        const endpoint = await waitForDesktopEndpoint();
        if (cancelled) {
          return;
        }
        if (!endpoint) {
          setApiClient(null);
          setBackendBaseUrl("");
          setConnectionState("error");
          setStatusMessage("desktop backend startup timed out after 60s");
          return;
        }

        setBackendBaseUrl(endpoint.base_url);
        setApiClient(createApiClientFromDesktopEndpoint(endpoint));
      } catch (error: unknown) {
        if (cancelled) {
          return;
        }
        setApiClient(null);
        setBackendBaseUrl("");
        setConnectionState("error");
        setStatusMessage(messageFromUnknown(error, "failed to request desktop backend endpoint"));
      }
    };

    void bootstrap();

    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!apiClient || !backendBaseUrl) {
      return;
    }

    let cancelled = false;
    let intervalId: number | null = null;

    const markBackendUnavailable = (message: string) => {
      if (cancelled) {
        return;
      }
      setHealth(null);
      setApiClient(null);
      setBackendBaseUrl("");
      setConnectionState("error");
      setStatusMessage(message);
    };

    const checkBackend = async () => {
      try {
        const status = await readDesktopBackendStatus();
        if (cancelled) {
          return;
        }
        if (status.lifecycle === "failed" || status.lifecycle === "stopped") {
          markBackendUnavailable(describeDesktopBackendStatus(status));
          return;
        }
        if (status.lifecycle !== "ready") {
          setHealth(null);
          setConnectionState("checking");
          setStatusMessage(describeDesktopBackendStatus(status));
          return;
        }

        const response = await apiClient.getHealth();
        if (cancelled) {
          return;
        }
        setHealth(response);
        setConnectionState("connected");
        setStatusMessage("backend connected");
      } catch (error: unknown) {
        markBackendUnavailable(messageFromUnknown(error, "backend monitor failed"));
      }
    };

    setConnectionState("checking");
    setStatusMessage(`checking ${backendBaseUrl}/health`);

    void checkBackend();
    intervalId = window.setInterval(() => {
      void checkBackend();
    }, BACKEND_HEALTH_CHECK_INTERVAL_MS);

    return () => {
      cancelled = true;
      if (intervalId !== null) {
        window.clearInterval(intervalId);
      }
    };
  }, [apiClient, backendBaseUrl]);

  const conversionJob = useConversionJob({
    apiClient,
    baseUrl: backendBaseUrl,
  });
  const refreshLibrary = library.refreshLibrary;
  const cancelActiveConversion = conversionJob.cancelActiveConversion;

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

  useEffect(() => {
    const onGlobalKeydown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const tag = target?.tagName?.toLowerCase() || "";
      if (tag === "input" || tag === "textarea" || tag === "button" || target?.isContentEditable) {
        return;
      }
      if (event.metaKey || event.ctrlKey || event.altKey) {
        return;
      }

      const key = String(event.key || "").toLowerCase();
      if (key === "s") {
        const startButton = activeTab === "convert" || activeTab === "document" ? document.getElementById("start-btn") : null;
        if (startButton instanceof HTMLButtonElement && !startButton.disabled) {
          startButton.click();
        }
      } else if (key === "r") {
        void refreshLibrary();
      } else if (key === " ") {
        event.preventDefault();
        void togglePlay();
      } else if (key === "arrowleft") {
        event.preventDefault();
        seekBy(-10);
      } else if (key === "arrowright") {
        event.preventDefault();
        seekBy(10);
      } else if (key === "n") {
        void playRelative(1);
      } else if (key === "p") {
        void playRelative(-1);
      }
    };

    document.addEventListener("keydown", onGlobalKeydown);
    return () => {
      document.removeEventListener("keydown", onGlobalKeydown);
    };
  }, [activeTab, cancelActiveConversion, playRelative, refreshLibrary, seekBy, togglePlay]);

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
            onClick={() => void cancelActiveConversion()}
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
