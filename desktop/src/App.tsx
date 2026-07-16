import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";

import { useBackendClient } from "./app/useBackendClient";
import { useGlobalShortcuts } from "./app/useGlobalShortcuts";
import { useRuntimeIdentityLabel } from "./app/usePrimaryRuntime";
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
import { SetupPage } from "./features/setup/SetupPage";
import { shouldShowRuntimeSetup } from "./features/setup/setupModel";
import {
  DASHBOARD_TAB_CONFIGS,
  DashboardTabIcon,
  conversionPanelTabForDashboardTab,
} from "./navigation/tabs";
import { nextTabIndexAfterKeyboard, tabButtonTabIndex } from "./navigation/tabKeyboardNavigation";
import type { DashboardTab, RuntimeSetupStatusResponse } from "./types/api";
import { BrailleSpinner } from "./ui/ascii/BrailleSpinner";

type BackendClientState = ReturnType<typeof useBackendClient>;

function DashboardApp({
  backend,
  onOpenRuntimeSetup,
}: {
  backend: BackendClientState;
  onOpenRuntimeSetup: () => void;
}) {
  const [activeTab, setActiveTab] = useState<DashboardTab>("dashboard");
  const {
    apiClient,
    backendBaseUrl,
    connectionState,
    health,
    statusMessage,
  } = backend;
  const startCommandRef = useRef<(() => void) | null>(null);
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);

  const library = useLibrary({
    apiClient,
    backendDisconnected: connectionState === "error",
  });

  const runtimeIdentity = useRuntimeIdentityLabel(apiClient);
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
  const activeTabIndex = DASHBOARD_TAB_CONFIGS.findIndex((tab) => tab.id === activeTab);

  const selectTabAtIndex = (index: number) => {
    const next = DASHBOARD_TAB_CONFIGS[index];
    if (!next) {
      return;
    }
    setActiveTab(next.id);
    tabRefs.current[index]?.focus();
  };

  const selectTab = (tab: DashboardTab) => {
    const index = DASHBOARD_TAB_CONFIGS.findIndex((candidate) => candidate.id === tab);
    if (index >= 0) {
      selectTabAtIndex(index);
    }
  };

  const onTabListKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    const nextIndex = nextTabIndexAfterKeyboard({
      key: event.key,
      activeIndex: Math.max(0, activeTabIndex),
      tabCount: DASHBOARD_TAB_CONFIGS.length,
    });
    if (nextIndex === null) {
      return;
    }
    event.preventDefault();
    selectTabAtIndex(nextIndex);
  };

  return (
    <main className="app-frame" role="application" aria-label="Orpheus dashboard">
      <aside className="rail" aria-label="Orpheus navigation">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">O</span>
          <span className="brand-name">Orpheus</span>
        </div>

        <nav
          className="rail-nav"
          role="tablist"
          aria-label="Dashboard sections"
          onKeyDown={onTabListKeyDown}
        >
          {DASHBOARD_TAB_CONFIGS.map(({ id, label, panelId }, index) => {
            const isActive = activeTab === id;
            return (
              <button
                id={`tab-btn-${id}`}
                key={id}
                ref={(node) => {
                  tabRefs.current[index] = node;
                }}
                className={`rail-tab${isActive ? " active" : ""}`}
                type="button"
                role="tab"
                tabIndex={tabButtonTabIndex(activeTabIndex, index)}
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
            <p>{`${runtimeIdentity} / ${library.indexedCount} books indexed`}</p>
          </div>
          <div className="studio-controls">
            {conversionJob.uiState === "running" ? (
              <span className="control converting-control">
                <BrailleSpinner active />
                <span>converting</span>
              </span>
            ) : null}
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
            onStartConvert={() => setActiveTab("convert")}
          />

          <ConversionPanel
            activeTab={conversionPanelTab}
            hidden={!isConversionPanelVisible}
            apiClient={apiClient}
            connectionState={connectionState}
            health={health}
            statusMessage={statusMessage}
            conversionJob={conversionJob}
            onOpenSettings={() => selectTab("settings")}
            onOpenRuntimeSetup={onOpenRuntimeSetup}
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
            playing={player.playing}
            onJumpToChapter={(chapter) => {
              void player.jumpToChapter(chapter);
            }}
            onStartConvert={() => setActiveTab("convert")}
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
          canSeek={player.canSeek}
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

function App() {
  const backend = useBackendClient();
  const [setupStatus, setSetupStatus] = useState<RuntimeSetupStatusResponse | null>(null);
  const [setupError, setSetupError] = useState<string | null>(null);
  const [libraryOnlyOverride, setLibraryOnlyOverride] = useState(false);
  const [forceOpenRuntimeSetup, setForceOpenRuntimeSetup] = useState(false);

  const onSetupReady = useCallback((status: RuntimeSetupStatusResponse) => {
    setSetupStatus(status);
    if (!forceOpenRuntimeSetup) {
      setForceOpenRuntimeSetup(false);
      setLibraryOnlyOverride(false);
    }
  }, [forceOpenRuntimeSetup]);

  const onContinueLibraryOnly = useCallback(() => {
    setForceOpenRuntimeSetup(false);
    setLibraryOnlyOverride(true);
  }, []);

  const onExitSettingsSetup = useCallback(() => {
    setForceOpenRuntimeSetup(false);
    setLibraryOnlyOverride(shouldShowRuntimeSetup(setupStatus, false));
  }, [setupStatus]);

  const onOpenRuntimeSetup = useCallback(() => {
    setSetupError(null);
    setSetupStatus(null);
    setForceOpenRuntimeSetup(true);
  }, []);

  useEffect(() => {
    if (!backend.apiClient || (libraryOnlyOverride && !forceOpenRuntimeSetup)) {
      return undefined;
    }
    let active = true;
    setSetupError(null);
    void backend.apiClient.getRuntimeSetupStatus({ timeoutMs: 8_000 }).then((status) => {
      if (active) setSetupStatus(status);
    }).catch((error: unknown) => {
      if (!active) return;
      const message = error instanceof Error && error.message.trim()
        ? error.message.trim().slice(0, 512)
        : "Runtime setup status is temporarily unavailable.";
      setSetupStatus(null);
      setSetupError(message);
    });
    return () => {
      active = false;
    };
  }, [backend.apiClient, forceOpenRuntimeSetup, libraryOnlyOverride]);

  if (forceOpenRuntimeSetup || shouldShowRuntimeSetup(setupStatus, libraryOnlyOverride)) {
    return (
      <SetupPage
        apiClient={backend.apiClient}
        initialStatus={setupStatus}
        initialError={setupError}
        onReady={onSetupReady}
        onContinueLibraryOnly={onContinueLibraryOnly}
        settingsEntry={forceOpenRuntimeSetup}
        onExitSettings={forceOpenRuntimeSetup ? onExitSettingsSetup : undefined}
        onReconnectBackend={backend.reconnectBackend}
      />
    );
  }

  return (
    <DashboardApp
      backend={backend}
      onOpenRuntimeSetup={onOpenRuntimeSetup}
    />
  );
}

export default App;
