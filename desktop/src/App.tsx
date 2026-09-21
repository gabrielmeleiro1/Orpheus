import {
  Fragment,
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";

import { useBackendClient } from "./app/useBackendClient";
import { syncSetupPreferences } from "./features/setup/setupPreferences";
import { invalidateRuntimeBootstrap } from "./app/runtimeBootstrapCache";
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
import { Icon } from "./ui/icons";
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
              <span className="setup-startup-spinner" aria-hidden="true" />
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
  setupStatus,
  onOpenRuntimeSetup,
}: {
  backend: BackendClientState;
  setupStatus: RuntimeSetupStatusResponse | null;
  onOpenRuntimeSetup: () => void;
}) {
  const compactQuery = "(max-width: 760px), (max-height: 540px)";
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => window.matchMedia(compactQuery).matches);
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
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  const sidebarRef = useRef<HTMLElement | null>(null);
  const showSidebarRef = useRef<HTMLButtonElement | null>(null);
  const pendingFocusRef = useRef<"heading" | "sidebar" | "toggle" | null>(null);
  const lastFocusedRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const query = window.matchMedia(compactQuery);
    const onChange = () => {
      if (query.matches && sidebarRef.current?.contains(document.activeElement)) {
        pendingFocusRef.current = "toggle";
      } else if (!query.matches && document.activeElement === showSidebarRef.current) {
        pendingFocusRef.current = "sidebar";
      }
      setSidebarCollapsed(query.matches);
    };
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);

  useLayoutEffect(() => {
    const target = pendingFocusRef.current;
    pendingFocusRef.current = null;
    if (target === "sidebar") {
      tabRefs.current[DASHBOARD_TAB_CONFIGS.findIndex((tab) => tab.id === activeTab)]?.focus();
    } else if (target === "toggle") {
      showSidebarRef.current?.focus();
    } else if (target === "heading" || lastFocusedRef.current?.closest("[hidden]")) {
      headingRef.current?.focus();
    }
  });

  const library = useLibrary({
    apiClient,
    backendDisconnected: connectionState === "error",
  });

  const runtimeIdentity = useRuntimeIdentity(apiClient, setupStatus);
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
    if (tab === activeTab) {
      headingRef.current?.focus();
      return;
    }
    pendingFocusRef.current = "heading";
    setActiveTab(tab);
  }, [activeTab]);

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

  const openConvert = useCallback(() => selectTab("convert"), [selectTab]);
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
    <main className={`app-frame${sidebarCollapsed ? " sidebar-collapsed" : ""}`} aria-label="Orpheus dashboard" onFocusCapture={(event) => { lastFocusedRef.current = event.target as HTMLElement; }}>
      <a className="skip-link" href="#workspace-heading" onClick={(event) => { event.preventDefault(); headingRef.current?.focus(); }}>Skip to content</a>
      <span className="sr-only" role="status" aria-atomic="true">{conversionJob.uiState === "idle" ? "" : conversionJob.isCancelling ? "Stopping conversion." : `Conversion ${conversionJob.uiState === "running" ? "started" : conversionJob.uiState}.`}</span>
      <aside ref={sidebarRef} id="app-sidebar" className="rail" aria-label="Orpheus navigation" hidden={sidebarCollapsed}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            pendingFocusRef.current = "toggle";
            setSidebarCollapsed(true);
          }
        }}
        onBlurCapture={(event) => {
          if (window.matchMedia(compactQuery).matches && event.relatedTarget instanceof Node && !event.currentTarget.contains(event.relatedTarget)) {
            setSidebarCollapsed(true);
          }
        }}
      >
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">O</span>
          <span className="brand-name">Orpheus</span>
          <button type="button" className="icon-btn sidebar-toggle" aria-label="Hide sidebar" aria-controls="app-sidebar" aria-expanded="true" data-tip="Hide sidebar" data-tip-pos="right" onClick={() => { pendingFocusRef.current = "toggle"; setSidebarCollapsed(true); }}>
            <Icon name="sidebar" />
          </button>
        </div>

        <nav
          className="rail-nav"
          role="tablist"
          aria-label="Dashboard sections"
          aria-orientation="vertical"
          onKeyDown={onTabListKeyDown}
        >
          {DASHBOARD_TAB_CONFIGS.map(({ id, label, panelId, icon, group }, index) => {
            const isActive = activeTab === id;
            const startsGroup = index === 0 || DASHBOARD_TAB_CONFIGS[index - 1]?.group !== group;
            return (
              <Fragment key={id}>
                {startsGroup ? <span className={`rail-group-label${index === 0 ? "" : " rail-section-label"}`} role="presentation">{group}</span> : null}
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
                  onClick={() => {
                    setActiveTab(id);
                    if (window.matchMedia(compactQuery).matches) {
                      pendingFocusRef.current = "heading";
                      setSidebarCollapsed(true);
                    }
                  }}
                >
                  <Icon name={icon} className="rail-icon" />
                  <span className="rail-label">{label}</span>
                  {id === "progress" && conversionJob.uiState === "running" ? <span className="rail-badge-dot" aria-hidden="true" /> : null}
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
              <Icon name="stop" className="rail-icon" />
              <span>{conversionJob.isCancelling ? "Stopping…" : "Stop conversion"}</span>
            </button>
          ) : null}
          <div className="rail-status">
            <span className={`connection-badge is-${connectionState}`} role="status" aria-label={`Backend ${connectionState}`}>
              <span className={`status-dot${connectionState === "connected" ? " is-connected" : connectionState === "error" ? " is-error" : " is-busy"}`} aria-hidden="true" />
              {connectionState === "connected" ? "Connected" : connectionState === "error" ? "Disconnected" : "Connecting…"}
            </span>
            <button
              className="icon-btn"
              type="button"
              aria-label="Refresh library"
              data-tip="Reload the library index"
              data-tip-pos="right"
              onClick={() => void refreshLibrary()}
            >
              <Icon name="refresh" />
            </button>
          </div>
        </div>
      </aside>

      <div className="workspace-shell">
        <header className="studio-status">
          <div className="page-heading">
            {sidebarCollapsed ? <button ref={showSidebarRef} type="button" className="icon-btn sidebar-expand" onClick={() => { pendingFocusRef.current = "sidebar"; setSidebarCollapsed(false); }} aria-label="Show sidebar" aria-controls="app-sidebar" aria-expanded="false" data-tip="Show sidebar" data-tip-pos="bottom"><Icon name="sidebar" /></button> : null}
            <div className="page-title-block">
              <h1 ref={headingRef} id="workspace-heading" tabIndex={-1}>{DASHBOARD_TAB_CONFIGS[activeTabIndex]?.label ?? "Orpheus"}</h1>
              <p className="page-description">{DASHBOARD_TAB_CONFIGS[activeTabIndex]?.description}</p>
            </div>
          </div>
          <div className="studio-controls">
            {conversionJob.uiState === "running" && activeTab !== "progress" ? (
              <button type="button" className="btn btn-ghost converting-control" onClick={openProgress}>
                <BrailleSpinner active />
                <span>Converting</span>
              </button>
            ) : null}
            {sidebarCollapsed || connectionState !== "connected" ? (
              <span className={`connection-badge is-${connectionState}`} {...(sidebarCollapsed ? { role: "status", "aria-label": `Backend ${connectionState}` } : { "aria-hidden": true })}>
                <span className={`status-dot${connectionState === "connected" ? " is-connected" : connectionState === "error" ? " is-error" : " is-busy"}`} aria-hidden="true" />
                {connectionState === "connected" ? "Connected" : connectionState === "error" ? "Disconnected" : "Connecting…"}
              </span>
            ) : null}
            {activeTab === "dashboard" ? <button className="btn btn-primary" type="button" onClick={openConvert}>Convert a source</button> : null}
          </div>
        </header>

        <div className={`main-workspace${activeTab === "convert" || activeTab === "document" ? " has-conversion" : ""}`}>
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
          onClose={() => { player.closePlayer(); headingRef.current?.focus(); }}
        />
      </div>
    </main>
  );
}

function App() {
  const backend = useBackendClient();
  const [setupStatus, setSetupStatus] = useState<RuntimeSetupStatusResponse | null>(null);
  const [setupStatusClient, setSetupStatusClient] = useState<BackendClientState["apiClient"]>(null);
  const [setupError, setSetupError] = useState<string | null>(null);
  const [libraryOnlyOverride, setLibraryOnlyOverride] = useState(false);
  const [forceOpenRuntimeSetup, setForceOpenRuntimeSetup] = useState(false);

  const onSetupReady = useCallback((status: RuntimeSetupStatusResponse) => {
    syncSetupPreferences(status);
    if (backend.apiClient) invalidateRuntimeBootstrap(backend.apiClient);
    setSetupStatusClient(backend.apiClient);
    setSetupStatus(status);
    if (!forceOpenRuntimeSetup) {
      setLibraryOnlyOverride(false);
    }
  }, [backend.apiClient, forceOpenRuntimeSetup]);

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
    if (!backend.apiClient) {
      return undefined;
    }
    let active = true;
    setSetupError(null);
    // Allow time for the cold-start subprocess probe and first-time model hashing.
    void backend.apiClient.getRuntimeSetupStatus({ timeoutMs: 120_000 }).then((status) => {
      if (!active) return;
      syncSetupPreferences(status);
      setSetupStatusClient(backend.apiClient);
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
  }, [backend.apiClient, forceOpenRuntimeSetup]);

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
      setupStatus={setupStatusClient === backend.apiClient ? setupStatus : null}
      onOpenRuntimeSetup={onOpenRuntimeSetup}
    />
  );
}

export default App;
