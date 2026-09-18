import {
  Fragment,
  memo,
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";

import { useBackendClient } from "./app/useBackendClient";
import { useGlobalShortcuts } from "./app/useGlobalShortcuts";
import { useRuntimeIdentity } from "./app/usePrimaryRuntime";
import { ConversionPanel } from "./features/conversion/ConversionPanel";
import { useConversionJob } from "./features/conversion/useConversionJob";
import { DashboardPanel } from "./features/dashboard/DashboardPanel";
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
  conversionPanelTabForDashboardTab,
} from "./navigation/tabs";
import { nextTabIndexAfterKeyboard, tabButtonTabIndex } from "./navigation/tabKeyboardNavigation";
import type { DashboardTab, RuntimeSetupStatusResponse } from "./types/api";
import { BrailleSpinner } from "./ui/ascii/BrailleSpinner";
import { equalWhenHidden, equalWhenInvisible, memoWhenHidden } from "./app/visibilityMemo";

type BackendClientState = ReturnType<typeof useBackendClient>;

// Tabs the app may auto-switch away from when a job starts or finishes. On any
// other tab the user is actively working, so we leave their focus alone rather
// than yanking them to Progress/Library mid-action.
const PASSIVE_AUTO_SWITCH_TABS: ReadonlySet<DashboardTab> = new Set<DashboardTab>([
  "dashboard",
  "progress",
]);

export function resolveAutoTabSwitch(input: {
  previousUiState: string;
  uiState: string;
  activeTab: DashboardTab;
}): DashboardTab | null {
  if (input.previousUiState !== "running" && input.uiState === "running") {
    return PASSIVE_AUTO_SWITCH_TABS.has(input.activeTab) ? "progress" : null;
  }
  if (input.previousUiState !== "completed" && input.uiState === "completed") {
    return PASSIVE_AUTO_SWITCH_TABS.has(input.activeTab) ? "library" : null;
  }
  return null;
}

const StableDashboardPanel = memo(DashboardPanel, equalWhenHidden);
const StableConversionPanel = memo(ConversionPanel, equalWhenHidden);
const StableLibraryPanel = memo(LibraryPanel, equalWhenHidden);
const StablePlayerPanel = memo(PlayerPanel, equalWhenInvisible);

const VisibilityProgressPanel = memoWhenHidden(ProgressPanel);
const VisibilityLogPanel = memoWhenHidden(LogPanel);

function RuntimeSetupChecking({ error, onRetry }: { error: string | null; onRetry: () => void }) {
  return (
    <main className="setup-frame" aria-label="Orpheus startup">
      <section className="setup-shell setup-startup" role={error ? undefined : "status"} aria-live={error ? undefined : "polite"}>
        {error ? (
          <>
            <h1>Unable to connect to your local runtime</h1>
            <p role="alert">{error}</p>
            <button type="button" className="btn" onClick={onRetry}>Retry connection</button>
          </>
        ) : (
          <>
            <h1>Orpheus</h1>
            <div className="setup-startup-status">
              <BrailleSpinner active />
              <span>Checking your local runtime…</span>
            </div>
          </>
        )}
      </section>
    </main>
  );
}

function DashboardApp({
  backend,
  onOpenRuntimeSetup,
}: {
  backend: BackendClientState;
  onOpenRuntimeSetup: () => void;
}) {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => window.innerWidth < 760);
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

  const runtimeIdentity = useRuntimeIdentity(apiClient);
  const player = useAudioPlayer({ apiClient, library });
  const previousUiStateRef = useRef<string>("idle");
  const visitedTabsRef = useRef<Set<DashboardTab>>(new Set(["dashboard"]));
  visitedTabsRef.current.add(activeTab);

  const conversionJob = useConversionJob({
    apiClient,
    baseUrl: backendBaseUrl,
  });
  const refreshLibrary = library.refreshLibrary;

  useEffect(() => {
    const previous = previousUiStateRef.current;
    previousUiStateRef.current = conversionJob.uiState;

    // Refresh the library after every terminal outcome. Failed and cancelled
    // jobs can still leave a recoverable partial book behind, so limiting this
    // to successful completion hides useful output until the user clicks
    // Refresh manually.
    const reachedTerminalState =
      conversionJob.uiState === "completed" ||
      conversionJob.uiState === "failed" ||
      conversionJob.uiState === "cancelled";
    if (previous !== conversionJob.uiState && reachedTerminalState) {
      void refreshLibrary();
    }

    const nextTab = resolveAutoTabSwitch({
      previousUiState: previous,
      uiState: conversionJob.uiState,
      activeTab,
    });
    if (nextTab) {
      setActiveTab(nextTab);
    }
  }, [activeTab, conversionJob.uiState, refreshLibrary]);

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
  const conversionPanelMounted = visitedTabsRef.current.has("convert")
    || visitedTabsRef.current.has("document")
    || visitedTabsRef.current.has("settings");
  const renderedConversionPanelId = conversionPanelMounted
    ? conversionPanelTab === "config"
      ? "tab-config"
      : conversionPanelTab === "document"
        ? "tab-document"
        : "tab-convert"
    : null;
  const activeTabIndex = DASHBOARD_TAB_CONFIGS.findIndex((tab) => tab.id === activeTab);

  const selectTabAtIndex = useCallback((index: number) => {
    const next = DASHBOARD_TAB_CONFIGS[index];
    if (!next) {
      return;
    }
    setActiveTab(next.id);
    tabRefs.current[index]?.focus();
  }, []);

  const selectTab = useCallback((tab: DashboardTab) => {
    const index = DASHBOARD_TAB_CONFIGS.findIndex((candidate) => candidate.id === tab);
    if (index >= 0) {
      selectTabAtIndex(index);
    }
  }, [selectTabAtIndex]);

  const onTabListKeyDown = useCallback((event: KeyboardEvent<HTMLElement>) => {
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
  }, [activeTabIndex, selectTabAtIndex]);

  const openConvert = useCallback(() => setActiveTab("convert"), []);
  const openSettings = useCallback(() => selectTab("settings"), [selectTab]);
  const openLibrary = useCallback(() => selectTab("library"), [selectTab]);
  const openProgress = useCallback(() => selectTab("progress"), [selectTab]);
  const openLogs = useCallback(() => selectTab("logs"), [selectTab]);
  const playPrevious = useCallback(() => {
    void playRelative(-1);
  }, [playRelative]);
  const playNext = useCallback(() => {
    void playRelative(1);
  }, [playRelative]);

  return (
    <main className={`app-frame${sidebarCollapsed ? " sidebar-collapsed" : ""}`} role="application" aria-label="Orpheus dashboard">
      <aside className="rail" aria-label="Orpheus navigation" hidden={sidebarCollapsed}>
        <div className="brand">
          <span className="brand-name">Orpheus</span>
          <button type="button" className="sidebar-toggle" aria-label="Hide sidebar" onClick={() => setSidebarCollapsed(true)}>‹</button>
        </div>

        <nav
          className="rail-nav"
          role="tablist"
          aria-label="Dashboard sections"
          aria-orientation="vertical"
          onKeyDown={onTabListKeyDown}
        >
          {DASHBOARD_TAB_CONFIGS.map(({ id, label, panelId }, index) => {
            const isActive = activeTab === id;
            return (
              <Fragment key={id}>
                {index === 0 ? <span className={`rail-group-label${["dashboard", "convert", "document"].includes(activeTab) ? " is-active" : ""}`} role="presentation">Audiobooks</span> : null}
                {id === "library" ? <span className={`rail-group-label rail-section-label${activeTab === "library" ? " is-active" : ""}`} role="presentation">Library</span> : null}
                {id === "progress" ? <span className={`rail-group-label rail-section-label${["progress", "logs"].includes(activeTab) ? " is-active" : ""}`} role="presentation">Activity</span> : null}
                {id === "settings" ? <span className={`rail-group-label rail-section-label${activeTab === "settings" ? " is-active" : ""}`} role="presentation">System</span> : null}
                <button
                  id={`tab-btn-${id}`}
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
                  <span className="rail-label">{label}</span>
                </button>
              </Fragment>
            );
          })}
        </nav>

        <div className="rail-bottom">
          {conversionJob.uiState === "running" ? (
            <button
              className="rail-action stop"
              type="button"
              disabled={conversionJob.isCancelling}
              data-tip="Stop the running conversion. Finished chapters are kept."
              data-tip-pos="right"
              onClick={() => void conversionJob.cancelActiveConversion()}
            >
              <span className="rail-dot" aria-hidden="true" />
              <span>{conversionJob.isCancelling ? "Stopping" : "Stop job"}</span>
            </button>
          ) : null}
          <button
            className="rail-action"
            type="button"
            data-tip="Reload the library index"
            data-tip-pos="right"
            onClick={() => void refreshLibrary()}
          >
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

      <div className={`workspace-shell page-${activeTab === "library" || activeTab === "logs" ? "data" : activeTab === "dashboard" || activeTab === "progress" ? "working" : "focused"}`}>
        <header className="studio-status">
          <div className="page-heading">
            {sidebarCollapsed ? <button type="button" className="btn sidebar-expand" onClick={() => setSidebarCollapsed(false)} aria-label="Show sidebar">☰ <span>Sidebar</span></button> : null}
            <h1>{DASHBOARD_TAB_CONFIGS[activeTabIndex]?.label ?? "Orpheus"}</h1>
          </div>
          <div className="studio-controls">
            {activeTab === "dashboard" ? <button className="btn btn-primary" type="button" onClick={openConvert}>Convert a source</button> : null}
            {conversionJob.uiState === "running" ? (
              <span className="control converting-control">
                <BrailleSpinner active />
                <span>Converting</span>
              </span>
            ) : null}
            <span className={`connection-badge is-${connectionState}`} role="status" aria-label={`Backend ${connectionState}`}>
              <span className={`status-dot${connectionState === "connected" ? " is-connected" : connectionState === "error" ? " is-error" : " is-busy"}`} aria-hidden="true" />
              {connectionState === "connected" ? "Connected" : connectionState === "error" ? "Disconnected" : "Connecting"}
            </span>
          </div>
        </header>

        <div className="main-workspace">
          <StableDashboardPanel
            hidden={activeTab !== "dashboard"}
            library={library}
            connectionState={connectionState}
            health={health}
            statusMessage={statusMessage}
            uiState={conversionJob.uiState}
            progressPct={conversionJob.progressPct}
            etaText={conversionJob.etaText}
            stages={conversionJob.stages}
            jobTitle={conversionJob.activeJobTitle}
            jobAuthor={conversionJob.activeJobAuthor}
            currentChapterLabel={player.currentChapterLabel}
            playerTitle={player.title}
            playing={player.playing}
            elapsedSec={player.elapsedSec}
            durationSec={player.durationSec}
            onOpenLibrary={openLibrary}
            onOpenProgress={openProgress}
            onOpenSettings={openSettings}
            onOpenLogs={openLogs}
            cleanerLabel={runtimeIdentity.cleanerLabel}
            ttsReady={runtimeIdentity.ttsReady}
          />

          {conversionPanelMounted ? <StableConversionPanel
            activeTab={conversionPanelTab}
            hidden={!isConversionPanelVisible}
            apiClient={apiClient}
            connectionState={connectionState}
            health={health}
            statusMessage={statusMessage}
            conversionJob={conversionJob}
            onOpenSettings={openSettings}
            onOpenRuntimeSetup={onOpenRuntimeSetup}
            startCommandRef={startCommandRef}
          /> : null}
          {["tab-convert", "tab-document", "tab-config"].map((panelId) => (
            panelId === renderedConversionPanelId ? null : (
              <section key={panelId} id={panelId} role="tabpanel" hidden />
            )
          ))}

          {visitedTabsRef.current.has("progress") ? <section
            id="tab-progress"
            className="panel"
            role="tabpanel"
            aria-label="Conversion progress"
            aria-labelledby="tab-btn-progress"
            hidden={activeTab !== "progress"}
          >
            <VisibilityProgressPanel
              hidden={activeTab !== "progress"}
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
              onStartConvert={openConvert}
            />
          </section> : <section id="tab-progress" role="tabpanel" aria-label="Conversion progress" aria-labelledby="tab-btn-progress" hidden />}

          {visitedTabsRef.current.has("logs") ? <section
            id="tab-logs"
            className="panel logs-panel"
            role="tabpanel"
            aria-label="Process logs"
            aria-labelledby="tab-btn-logs"
            hidden={activeTab !== "logs"}
          >
            <VisibilityLogPanel hidden={activeTab !== "logs"} logs={conversionJob.logs} />
          </section> : <section id="tab-logs" role="tabpanel" aria-label="Process logs" hidden />}

          {visitedTabsRef.current.has("library") ? <StableLibraryPanel
            hidden={activeTab !== "library"}
            library={library}
            currentChapterNumber={player.currentChapter?.number ?? null}
            playing={player.playing}
            onJumpToChapter={player.jumpToChapter}
            onStartConvert={openConvert}
          /> : <section id="tab-library" role="tabpanel" aria-label="Library and playback" aria-labelledby="tab-btn-library" hidden />}
        </div>

        <StablePlayerPanel
          enabled={player.playerEligible}
          visible={player.playerVisible}
          playing={player.playing}
          title={player.title}
          bookTitle={library.selectedBookDetail?.title}
          chapterTitle={player.currentChapterLabel}
          elapsedSec={player.elapsedSec}
          durationSec={player.durationSec}
          canSeek={player.canSeek}
          volume={player.volume}
          canGoPrevious={player.canGoPrevious}
          canGoNext={player.canGoNext}
          audioRef={player.audioRef}
          audio={player.audioProps}
          onTogglePlay={togglePlay}
          onSeekBy={seekBy}
          onSeekTo={seekTo}
          onPrevious={playPrevious}
          onNext={playNext}
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
    // Cold-start status can legitimately take ~20s: the capability probe boots
    // a subprocess and cached model assets get hashed for the first receipt
    // verification. An 8s timeout guaranteed a spurious first-launch error.
    void backend.apiClient.getRuntimeSetupStatus({ timeoutMs: 120_000 }).then((status) => {
      if (!active) return;
      setSetupStatus(status);
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

  if (
    !forceOpenRuntimeSetup
    && !libraryOnlyOverride
    && setupStatus === null
    && setupError === null
  ) {
    return (
      <RuntimeSetupChecking
        error={backend.connectionState === "error" ? backend.statusMessage : null}
        onRetry={backend.reconnectBackend}
      />
    );
  }

  if (
    forceOpenRuntimeSetup
    || shouldShowRuntimeSetup(setupStatus, libraryOnlyOverride)
  ) {
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
