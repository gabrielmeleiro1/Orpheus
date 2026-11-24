import {
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";

import { useBackendClient } from "./app/useBackendClient";
import { resolveCompletionNotice } from "./app/completionNotice";
import { showDashboardDuringRuntimeCheck } from "./app/runtimeStartup";
import { conversionRuntimeState, runtimeTtsReady } from "./app/runtimeReadiness";
import { useVerifiedConversionJob } from "./app/useVerifiedConversionJob";
import { useGlobalShortcuts } from "./app/useGlobalShortcuts";
import { useRuntimeBootstrapRefresh } from "./app/useRuntimeBootstrapRefresh";
import { useRuntimeSetupStatus } from "./app/useRuntimeSetupStatus";
import { ConversionPanel } from "./features/conversion/ConversionPanel";
import type { ConversionRuntimeState } from "./features/conversion/conversionPanelStateTypes";
import { useConversionJob } from "./features/conversion/useConversionJob";
import { LibraryMaintenanceSection } from "./features/library/LibraryMaintenanceSection";
import { rememberLastPlayedBookId } from "./features/library/lastPlayedBook";
import { LibraryPanel } from "./features/library/LibraryPanel";
import { useLibrary } from "./features/library/useLibrary";
import { NowPlayingView } from "./features/player/NowPlayingView";
import { PlayerPanel } from "./features/player/PlayerPanel";
import { usePlaybackRate } from "./features/player/usePlaybackExtras";
import { formatClock } from "./format/time";
import { useAudioPlayer } from "./features/player/useAudioPlayer";
import { ConversionDetailsDrawer } from "./features/progress/ConversionDetailsDrawer";
import { SetupPage } from "./features/setup/SetupPage";
import { SettingsPage } from "./features/setup/SettingsPage";
import { isCheckingConfiguredRuntime, shouldShowRuntimeSetup } from "./features/setup/setupModel";
import {
  DASHBOARD_TAB_CONFIGS,
  conversionPanelTabForDashboardTab,
} from "./navigation/tabs";
import { nextTabIndexAfterKeyboard, tabButtonTabIndex } from "./navigation/tabKeyboardNavigation";
import type { DashboardTab, LibraryBookSummary, RuntimeSetupStatusResponse } from "./types/api";
import { AsciiLoader } from "./ui/ascii/AsciiLoader";
import { BookCover } from "./ui/BookCover";
import { BrandMark } from "./ui/BrandMark";
import { CoverClientContext, CoverGenerationContext, CoverRefreshContext, createCoverRefreshSignal } from "./ui/coverArt";
import { Icon } from "./ui/icons";
import { ProgressBar } from "./ui/ProgressBar";
import { TooltipLayer } from "./ui/TooltipLayer";
import { installFocusRescue } from "./ui/focusRescue";
import { equalWhenClosed, equalWhenHidden, equalWhenInvisible } from "./app/visibilityMemo";

type BackendClientState = ReturnType<typeof useBackendClient>;

// The finished-book notice dismisses itself; it is an acknowledgement, not a task.
const COMPLETION_NOTICE_MS = 10_000;
const COMPLETED_BOOK_LOOKUP_TIMEOUT_MS = 8_000;
// Narrow or short windows overlay the sidebar instead of docking it.
const COMPACT_VIEWPORT_QUERY = "(max-width: 760px), (max-height: 540px)";
const isCompactViewport = () => window.matchMedia(COMPACT_VIEWPORT_QUERY).matches;

interface CompletionNotice {
  id: number;
  kind: "completed" | "failed";
  title: string;
  bookId: number | null;
  /** The book was already in the library (a repair), so it was updated rather than added. */
  updated?: boolean;
}

const StableConversionPanel = memo(ConversionPanel, (previous, next) =>
  previous.apiClient === next.apiClient
  && previous.conversionJob === next.conversionJob
  && previous.runtimeSelectionGeneration === next.runtimeSelectionGeneration
  && previous.runtimeState === next.runtimeState
  && equalWhenHidden(previous, next),
);
const StableLibraryPanel = memo(LibraryPanel, equalWhenHidden);
const StablePlayerPanel = memo(PlayerPanel, equalWhenInvisible);
// Settings wraps the always-mounted conversion panel. Its section elements are
// built with useMemo below, so playback ticks reach none of them, and an SSE
// event re-renders the page only to pass the new job to the panel. A replaced
// client always reaches the setup page, whose request guards are keyed on it.
const StableSettingsPage = memo(SettingsPage);
type SetupPageProps = Parameters<typeof SetupPage>[0] & { hidden: boolean };
const StableSettingsSetupPage = memo(SetupPage, (previous, next) =>
  previous.apiClient === next.apiClient && equalWhenHidden(previous as SetupPageProps, next as SetupPageProps));
const StableLibraryMaintenanceSection = memo(LibraryMaintenanceSection);
const StableConversionDetailsDrawer = memo(ConversionDetailsDrawer, equalWhenClosed);
const StableNowPlayingView = memo(NowPlayingView, equalWhenClosed);
const EMPTY_CHAPTERS: never[] = [];

function RuntimeSetupChecking({ error, exhausted, onRetry, onContinueLibraryOnly }: {
  error: string | null;
  exhausted: boolean;
  onRetry: () => void;
  onContinueLibraryOnly: () => void;
}) {
  const [longCheck, setLongCheck] = useState(false);
  useEffect(() => {
    setLongCheck(false);
    if (error || exhausted) return;
    const timer = window.setTimeout(() => setLongCheck(true), 5_000);
    return () => window.clearTimeout(timer);
  }, [error, exhausted]);

  return (
    <main className="setup-frame" aria-label="Orpheus startup">
      <section className="setup-shell setup-startup" role={error || exhausted ? undefined : "status"} aria-live={error || exhausted ? undefined : "polite"}>
        {error ? (
          <>
            <h1>Couldn't connect to the local runtime</h1>
            <div role="alert">
              <p>Retry, or restart Orpheus if this keeps happening.</p>
              <span className="error-detail">{error}</span>
            </div>
            <button type="button" className="btn btn-secondary" onClick={onRetry}>Retry</button>
          </>
        ) : exhausted ? (
          <>
            <h1>Runtime check needs more time</h1>
            <p role="alert">Checking your local runtime is taking longer than expected. Retry the check or use your library for now.</p>
            <button type="button" className="btn btn-secondary" onClick={onRetry}>Retry</button>
            <button type="button" className="btn btn-secondary" onClick={onContinueLibraryOnly}>Use library only</button>
          </>
        ) : (
          <>
            <h1>Orpheus</h1>
            <div className="setup-startup-status">
              <AsciiLoader active width={8} className="setup-startup-spinner" />
              <span>Checking your local runtime…</span>
            </div>
            {longCheck ? <p>First launch checks your GPU and model files — this can take up to a minute.</p> : null}
          </>
        )}
      </section>
    </main>
  );
}

function ConnectionBadge({ state }: { state: Exclude<BackendClientState["connectionState"], "connected"> }) {
  const error = state === "error";
  return (
    <span className={`connection-badge is-${state}`} role="status" aria-label={error ? "Backend disconnected" : "Connecting to backend"}>
      <span className={`status-dot ${error ? "is-error" : "is-busy"}`} aria-hidden="true" />
      {error ? "Disconnected" : "Connecting…"}
    </span>
  );
}

// The dashboard re-renders on every playback tick and progress event; the
// sidebar shelf only changes with the books, the selection or play state.
const RailLibrary = memo(function RailLibrary({ items, indexedCount, selectedBookId, albumOpen, playingBookId, onOpenBook }: {
  items: LibraryBookSummary[];
  indexedCount: number;
  selectedBookId: number | null;
  /** The selected book's album page is showing. */
  albumOpen: boolean;
  /** The book audibly playing, or null. */
  playingBookId: number | null;
  onOpenBook: (bookId: number) => void;
}) {
  return (
    <section className="rail-library" aria-label="Your library">
      <div className="rail-library-header">
        <span className="eyebrow rail-group-label">Your library</span>
        <span className="rail-library-count tabular-nums">{indexedCount}</span>
      </div>
      <div className="rail-library-list">
        {items.map((item) => {
          const isCurrent = albumOpen && item.id === selectedBookId;
          const isPlaying = item.id === playingBookId;
          return (
            <button
              key={item.id}
              type="button"
              className={`rail-book${isCurrent ? " is-current" : ""}${isPlaying ? " is-playing" : ""}`}
              aria-current={isCurrent ? "true" : undefined}
              onClick={() => onOpenBook(item.id)}
            >
              <BookCover bookId={item.id} sourceKey={item.source_path} title={item.title} author={item.author} size="sm" />
              <span className="rail-book-text">
                <span className="rail-book-title">{item.title || "Untitled"}</span>
                {isPlaying || item.author ? (
                  <span className="rail-book-author">{isPlaying ? "Playing" : item.author}</span>
                ) : null}
              </span>
            </button>
          );
        })}
      </div>
    </section>
  );
});

function DashboardApp({
  hidden = false,
  backend,
  setupStatus,
  conversionRuntime,
  runtimeSelectionGeneration,
  onSettingsSetupReady,
  onContinueLibraryOnly,
  runtimeNotice = null,
}: {
  hidden?: boolean;
  backend: BackendClientState;
  setupStatus: RuntimeSetupStatusResponse | null;
  /** Library-only use keeps Create visible; this says why it cannot start. */
  conversionRuntime: ConversionRuntimeState;
  runtimeSelectionGeneration: number;
  onSettingsSetupReady: (status: RuntimeSetupStatusResponse) => void;
  onContinueLibraryOnly: () => void;
  /** Shown in the workspace notice row, above any completion notice. */
  runtimeNotice?: ReactNode;
}) {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(isCompactViewport);
  const [activeTab, setActiveTab] = useState<DashboardTab>("library");
  const [albumOpen, setAlbumOpen] = useState(false);
  const [libraryNavigationGeneration, setLibraryNavigationGeneration] = useState(0);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [notice, setNotice] = useState<CompletionNotice | null>(null);
  const {
    apiClient,
    backendBaseUrl,
    connectionState,
  } = backend;
  const startCommandRef = useRef<(() => void) | null>(null);
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  const sidebarRef = useRef<HTMLElement | null>(null);
  const showSidebarRef = useRef<HTMLButtonElement | null>(null);
  const pendingFocusRef = useRef<"heading" | "sidebar" | "toggle" | null>(null);
  const lastFocusedRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const query = window.matchMedia(COMPACT_VIEWPORT_QUERY);
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
    if (hidden) return;
    const target = pendingFocusRef.current;
    pendingFocusRef.current = null;
    if (target === "sidebar") {
      tabRefs.current[DASHBOARD_TAB_CONFIGS.findIndex((tab) => tab.id === activeTab)]?.focus();
    } else if (target === "toggle") {
      showSidebarRef.current?.focus();
    } else if (target === "heading" || lastFocusedRef.current?.parentElement?.closest("[hidden]")) {
      // A page change hid the focused control's whole panel. A control hidden
      // on its own (Start giving way to Cancel) is left to the focus rescue,
      // which picks the nearest surviving control instead.
      headingRef.current?.focus();
    }
  });

  const library = useLibrary({
    apiClient,
    backendDisconnected: connectionState === "error",
  });

  // Covers whose request failed retry once each library load finishes.
  const [coverRefresh] = useState(createCoverRefreshSignal);
  const listWasLoadingRef = useRef(library.loadingList);
  useEffect(() => {
    const wasLoading = listWasLoadingRef.current;
    listWasLoadingRef.current = library.loadingList;
    if (wasLoading && !library.loadingList) coverRefresh.notify();
  }, [coverRefresh, library.loadingList]);

  useRuntimeBootstrapRefresh(apiClient);
  const player = useAudioPlayer({ apiClient, library });
  const { playbackRate, setPlaybackRate } = usePlaybackRate(player.audioRef, player.audioProps.src);
  const { sleepTimer, sleepRemainingSec, setSleepTimer } = player;
  const [nowPlayingOpen, setNowPlayingOpen] = useState(false);
  const nowPlayingVisible = nowPlayingOpen && player.playerVisible;
  // NowPlayingView returns focus to whichever control opened it.
  const closeNowPlaying = useCallback(() => {
    setNowPlayingOpen(false);
  }, []);
  // The book that last played is the one the next launch opens on.
  // The audio element follows the playing book, not the book being viewed.
  const viewingPlayingBook = player.playingBookId !== null && player.playingBookId === library.selectedBookId;
  const playingBookId = player.playing ? player.playingBookId : null;
  useEffect(() => {
    if (playingBookId !== null) rememberLastPlayedBookId(playingBookId);
  }, [playingBookId]);
  const sleepLabel = sleepTimer.kind === "chapter"
    ? "Sleep · chapter end"
    : sleepRemainingSec !== null ? `Sleep ${formatClock(sleepRemainingSec)}` : undefined;
  const previousUiStateRef = useRef<string>("idle");
  const previousJobIdRef = useRef<string | null>(null);
  const noticeSequenceRef = useRef(0);
  const noticeClientRef = useRef(apiClient);
  useLayoutEffect(() => {
    noticeClientRef.current = apiClient;
  }, [apiClient]);
  useEffect(() => () => { noticeSequenceRef.current += 1; }, []);
  useEffect(() => {
    noticeSequenceRef.current += 1;
    setNotice(null);
    previousUiStateRef.current = "idle";
    previousJobIdRef.current = null;
  }, [apiClient]);
  // Panels mount on first visit and then stay mounted so their state survives tab switches.
  const [visitedTabs, setVisitedTabs] = useState<ReadonlySet<DashboardTab>>(() => new Set<DashboardTab>(["library"]));
  if (!visitedTabs.has(activeTab)) {
    setVisitedTabs(new Set(visitedTabs).add(activeTab));
  }

  const liveConversionJob = useConversionJob({
    apiClient,
    baseUrl: backendBaseUrl,
  });
  const conversionJob = useVerifiedConversionJob(liveConversionJob,
    runtimeTtsReady(setupStatus) === true && !isCheckingConfiguredRuntime(setupStatus));
  const refreshLibrary = library.refreshLibrary;
  const jobRunning = conversionJob.uiState === "running";
  const jobPercent = Math.max(0, Math.min(100, Math.round(Number(conversionJob.progressPct) || 0)));

  useEffect(() => {
    const previous = previousUiStateRef.current;
    const completedJobId = previousJobIdRef.current;
    previousJobIdRef.current = conversionJob.activeJobId;
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

    const kind = previous === conversionJob.uiState ? null : resolveCompletionNotice({ previousUiState: previous, uiState: conversionJob.uiState });
    if (!kind) return;
    const title = conversionJob.activeJobTitle || (kind === "completed" ? "Your audiobook" : "The conversion");
    const id = ++noticeSequenceRef.current;
    setNotice({ id, kind, title, bookId: null });
    if (kind !== "completed" || !apiClient) return;
    // The finished job names its book, so the notice can open it directly.
    void apiClient.getActiveConversion({ timeoutMs: COMPLETED_BOOK_LOOKUP_TIMEOUT_MS }).then(({ job }) => {
      if (noticeClientRef.current === apiClient && noticeSequenceRef.current === id
        && job?.status === "completed" && job.book_id != null
        && (!completedJobId || job.id === completedJobId)) {
        setNotice((current) => (current?.id === id ? { ...current, bookId: job.book_id ?? null, updated: job.repair } : current));
      }
    }).catch(() => undefined);
  }, [apiClient, conversionJob.activeJobId, conversionJob.activeJobTitle, conversionJob.uiState, refreshLibrary]);

  const noticeId = notice?.id;
  useEffect(() => {
    if (noticeId === undefined) return undefined;
    const timer = window.setTimeout(() => setNotice(null), COMPLETION_NOTICE_MS);
    return () => window.clearTimeout(timer);
  }, [noticeId]);

  const { playRelative, seekBy, seekTo, togglePlay } = player;
  useGlobalShortcuts({
    enabled: !hidden && !detailsOpen,
    startEnabled: !nowPlayingVisible,
    activeTab,
    startCommandRef,
    refreshLibrary,
    playRelative,
    seekBy,
    togglePlay,
  });

  const isConversionPanelVisible = activeTab === "convert" || activeTab === "settings";
  const conversionPanelTab = conversionPanelTabForDashboardTab(activeTab);
  const conversionPanelMounted = visitedTabs.has("convert")
    || visitedTabs.has("settings");
  const renderedConversionPanelId = conversionPanelMounted
    ? conversionPanelTab === "config" ? "tab-config" : "tab-convert"
    : null;
  const activeTabIndex = DASHBOARD_TAB_CONFIGS.findIndex((tab) => tab.id === activeTab);

  const selectTabAtIndex = useCallback((index: number) => {
    const next = DASHBOARD_TAB_CONFIGS[index];
    if (!next) {
      return;
    }
    setActiveTab(next.id);
    setLibraryNavigationGeneration((generation) => generation + 1);
    if (next.id === "library") setAlbumOpen(false);
    tabRefs.current[index]?.focus();
  }, []);

  const selectTab = useCallback((tab: DashboardTab) => {
    setLibraryNavigationGeneration((generation) => generation + 1);
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

  const { selectBook, selectBookById } = library;
  const showAlbum = useCallback((select: () => Promise<unknown>) => {
    setLibraryNavigationGeneration((generation) => generation + 1);
    void select();
    setAlbumOpen(true);
    if (activeTab !== "library") {
      setActiveTab("library");
    }
  }, [activeTab]);
  const openAlbum = useCallback((index: number) => showAlbum(() => selectBook(index)), [selectBook, showAlbum]);
  const openAlbumById = useCallback((bookId: number) => showAlbum(() => selectBookById(bookId)), [selectBookById, showAlbum]);
  const closeAlbum = useCallback(() => {
    setAlbumOpen(false);
  }, []);
  const openRailBook = useCallback((bookId: number) => {
    openAlbumById(bookId);
    if (isCompactViewport()) setSidebarCollapsed(true);
  }, [openAlbumById]);
  const openBookById = useCallback((bookId: number | null) => {
    if (bookId !== null) {
      openAlbumById(bookId);
    } else {
      setAlbumOpen(false);
      selectTab("library");
    }
  }, [openAlbumById, selectTab]);

  const openConvert = useCallback(() => selectTab("convert"), [selectTab]);
  const openSettings = useCallback(() => selectTab("settings"), [selectTab]);
  const openDetails = useCallback(() => setDetailsOpen(true), []);
  const closeDetails = useCallback(() => setDetailsOpen(false), []);
  const playPrevious = useCallback(() => {
    void playRelative(-1);
  }, [playRelative]);
  const playNext = useCallback(() => {
    void playRelative(1);
  }, [playRelative]);
  const { playSelectedBook } = player;
  const playSelected = useCallback(() => {
    void playSelectedBook();
  }, [playSelectedBook]);
  const { jumpToChapter, closePlayer } = player;
  const jumpToChapterFromNowPlaying = useCallback((chapter: Parameters<typeof jumpToChapter>[0]) => {
    void jumpToChapter(chapter);
  }, [jumpToChapter]);
  const closePlayerPanel = useCallback(() => {
    setNowPlayingOpen(false);
    closePlayer();
    headingRef.current?.focus();
  }, [closePlayer]);
  const toggleNowPlaying = useCallback(() => {
    if (nowPlayingVisible) closeNowPlaying();
    else setNowPlayingOpen(true);
  }, [closeNowPlaying, nowPlayingVisible]);

  const settingsVisited = visitedTabs.has("settings");
  const settingsHidden = activeTab !== "settings";
  const { reconnectBackend } = backend;
  const settingsModels = useMemo(() => (settingsVisited ? (
    <StableSettingsSetupPage
      embedded
      hidden={settingsHidden}
      apiClient={apiClient}
      initialStatus={setupStatus}
      initialError={null}
      onReady={onSettingsSetupReady}
      onContinueLibraryOnly={onContinueLibraryOnly}
      settingsEntry
      onReconnectBackend={reconnectBackend}
    />
  ) : null), [apiClient, onContinueLibraryOnly, onSettingsSetupReady, reconnectBackend, settingsHidden, settingsVisited, setupStatus]);
  const selectedDetailId = library.selectedBookDetail?.id ?? null;
  const settingsMaintenance = useMemo(
    () => <StableLibraryMaintenanceSection library={library} selectedDetailId={selectedDetailId} />,
    [library, selectedDetailId],
  );
  const conversionPanel = useMemo(() => (
    <StableConversionPanel
      activeTab={conversionPanelTab}
      hidden={!isConversionPanelVisible}
      apiClient={apiClient}
      runtimeSelectionGeneration={runtimeSelectionGeneration}
      runtimeState={conversionRuntime}
      connectionState={connectionState}
      conversionJob={conversionJob}
      onOpenSettings={openSettings}
      onOpenConversionDetails={openDetails}
      startCommandRef={startCommandRef}
    />
  ), [apiClient, connectionState, conversionJob, conversionPanelTab, conversionRuntime, isConversionPanelVisible,
    openDetails, openSettings, runtimeSelectionGeneration]);

  const pageLabel = activeTab === "library" && albumOpen && library.selectedBookDetail
    ? library.selectedBookDetail.title || "Untitled"
    : DASHBOARD_TAB_CONFIGS[activeTabIndex]?.label ?? "Orpheus";

  return (
    <CoverClientContext.Provider value={apiClient}>
    <CoverGenerationContext.Provider value={library.libraryGeneration ?? 0}>
    <CoverRefreshContext.Provider value={coverRefresh}>
    <main className={`app-frame${sidebarCollapsed ? " sidebar-collapsed" : ""}`} aria-label="Orpheus" onFocusCapture={(event) => { lastFocusedRef.current = event.target as HTMLElement; }}>
      <a className="skip-link" hidden={nowPlayingVisible} href="#workspace-heading" onClick={(event) => { event.preventDefault(); headingRef.current?.focus(); }}>Skip to content</a>
      <span className="sr-only" role="status" aria-atomic="true">{conversionJob.uiState === "idle" || conversionJob.uiState === "unknown" ? "" : conversionJob.isCancelling ? "Stopping conversion." : `Conversion ${conversionJob.uiState === "running" ? "started" : conversionJob.uiState}.`}</span>
      <aside ref={sidebarRef} id="app-sidebar" className="rail" aria-label="Orpheus navigation" hidden={sidebarCollapsed}
        {...(nowPlayingVisible || detailsOpen ? { inert: "" } : {})}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            pendingFocusRef.current = "toggle";
            setSidebarCollapsed(true);
          }
        }}
        onBlurCapture={(event) => {
          if (isCompactViewport() && event.relatedTarget instanceof Node && !event.currentTarget.contains(event.relatedTarget)) {
            setSidebarCollapsed(true);
          }
        }}
      >
        <div className="brand">
          <BrandMark />
          <span className="brand-name">Orpheus</span>
          <button type="button" className="btn btn-ghost btn-sm btn-icon sidebar-toggle" aria-label="Hide sidebar" aria-controls="app-sidebar" aria-expanded="true" data-tip="Hide sidebar" data-tip-pos="right" onClick={() => { pendingFocusRef.current = "toggle"; setSidebarCollapsed(true); }}>
            <Icon name="sidebar" />
          </button>
        </div>

        <nav
          className="rail-nav"
          role="tablist"
          aria-label="Sections"
          aria-orientation="vertical"
          onKeyDown={onTabListKeyDown}
        >
          {DASHBOARD_TAB_CONFIGS.map(({ id, label, panelId, icon }, index) => {
            const isActive = activeTab === id;
            return (
              <button
                key={id}
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
                  // The Library item always leads back to the shelf.
                  if (id === "library") setAlbumOpen(false);
                  setActiveTab(id);
                  if (isCompactViewport()) {
                    pendingFocusRef.current = "heading";
                    setSidebarCollapsed(true);
                  }
                }}
              >
                <Icon name={icon} className="rail-icon" />
                <span className="rail-label">{label}</span>
                {id === "convert" && jobRunning ? <span className="rail-badge-dot" aria-hidden="true" /> : null}
              </button>
            );
          })}
        </nav>

        {library.unfilteredItems.length > 0 ? (
          <RailLibrary
            items={library.unfilteredItems}
            indexedCount={library.indexedCount}
            selectedBookId={library.selectedBookId}
            albumOpen={activeTab === "library" && albumOpen}
            playingBookId={playingBookId}
            onOpenBook={openRailBook}
          />
        ) : null}

        {jobRunning || connectionState !== "connected" ? (
          <div className="rail-bottom">
            {jobRunning ? (
              <button
                type="button"
                className="rail-job"
                aria-label={`Converting ${conversionJob.activeJobTitle || "a book"}, ${jobPercent}%. Show details`}
                data-tip="Show stages and the event log"
                data-tip-pos="right"
                onClick={openDetails}
              >
                <span className="rail-job-top">
                  <AsciiLoader active={!hidden} width={4} className="rail-job-spinner" />
                  <span className="rail-job-label">{conversionJob.isCancelling ? "Stopping…" : conversionJob.connectionLost ? "Reconnecting…" : "Converting"}</span>
                  <span className="rail-job-pct tabular-nums">{jobPercent}%</span>
                </span>
                {conversionJob.activeJobTitle ? <span className="rail-job-title">{conversionJob.activeJobTitle}</span> : null}
                <ProgressBar pct={jobPercent} tone="active" size="hairline" variant="ascii" cells={26} label="Conversion progress" />
              </button>
            ) : null}
            {connectionState !== "connected" ? (
              <div className="rail-status">
                <ConnectionBadge state={connectionState} />
              </div>
            ) : null}
          </div>
        ) : null}
      </aside>

      <div className="workspace-shell" {...(nowPlayingVisible || detailsOpen ? { inert: "" } : {})}>
        <header className="studio-status">
          <div className="page-container page-header">
            <h1 ref={headingRef} id="workspace-heading" tabIndex={-1} className="sr-only" data-focus-landing="">{pageLabel}</h1>
            {sidebarCollapsed ? (
              <div className="studio-controls">
                <button ref={showSidebarRef} type="button" className="btn btn-ghost btn-sm btn-icon sidebar-expand" onClick={() => { pendingFocusRef.current = "sidebar"; setSidebarCollapsed(false); }} aria-label="Show sidebar" aria-controls="app-sidebar" aria-expanded="false" data-tip="Show sidebar" data-tip-pos="bottom"><Icon name="sidebar" /></button>
                {jobRunning ? (
                  <button type="button" className="btn btn-ghost converting-control" onClick={openDetails}>
                    <AsciiLoader active width={5} />
                    <span className="tabular-nums">{conversionJob.connectionLost ? "Reconnecting…" : "Converting"} · {jobPercent}%</span>
                  </button>
                ) : null}
                {connectionState !== "connected" ? <ConnectionBadge state={connectionState} /> : null}
              </div>
            ) : null}
          </div>
        </header>

        <div className={`main-workspace${activeTab === "convert" ? " has-conversion" : ""}`}>
          {conversionPanelMounted ? (
            <StableSettingsPage
              active={!settingsHidden}
              apiClient={apiClient}
              models={settingsModels}
              maintenance={settingsMaintenance}
            >
              {conversionPanel}
            </StableSettingsPage>
          ) : null}
          {["tab-convert", "tab-config"].map((panelId) => (
            panelId === renderedConversionPanelId ? null : (
              <section key={panelId} id={panelId} role="tabpanel" hidden />
            )
          ))}

          <StableLibraryPanel
            navigationGeneration={libraryNavigationGeneration}
            hidden={activeTab !== "library"}
            library={library}
            albumOpen={albumOpen}
            onOpenAlbum={openAlbum}
            onCloseAlbum={closeAlbum}
            currentChapterNumber={viewingPlayingBook ? player.currentChapter?.number ?? null : null}
            currentChapterLabel={viewingPlayingBook ? player.currentChapterLabel : undefined}
            playing={viewingPlayingBook && player.playing}
            playerLoaded={viewingPlayingBook && player.canSeek}
            elapsedSec={viewingPlayingBook ? player.elapsedSec : 0}
            durationSec={viewingPlayingBook ? player.durationSec : 0}
            onTogglePlay={playSelected}
            onJumpToChapter={player.jumpToChapter}
            onReleaseBookAudio={player.releaseBookAudio}
            onStartConvert={openConvert}
          />
        </div>
        {/* In the workspace flow, under the page: a notice never covers a control. */}
        <div className="workspace-notices">
        {runtimeNotice}
        {notice ? (
          <div className={`completion-notice is-${notice.kind}`} role="status" aria-live="polite">
            <span className="completion-notice-mark" aria-hidden="true">{notice.kind === "completed" ? "✓" : "!"}</span>
            <span className="completion-notice-copy">
              <strong>{notice.kind === "completed" ? `${notice.title} is ready` : `${notice.title} failed`}</strong>
              <span>{notice.kind === "completed" ? notice.updated ? "Updated in your library." : "Added to your library." : "Finished chapters are kept. The log says what went wrong."}</span>
            </span>
            {notice.kind === "completed" ? (
              <button type="button" className="btn btn-secondary btn-sm" onClick={() => { setNotice(null); openBookById(notice.bookId); }}>
                Open
              </button>
            ) : (
              <button type="button" className="btn btn-secondary btn-sm" onClick={() => { setNotice(null); openDetails(); }}>
                Details
              </button>
            )}
            <button type="button" className="btn btn-ghost btn-sm btn-icon" aria-label="Dismiss" onClick={() => setNotice(null)}>
              <Icon name="x" />
            </button>
          </div>
        ) : null}
        </div>
      </div>

      <StableConversionDetailsDrawer open={detailsOpen} conversionJob={conversionJob} onClose={closeDetails} />

      <StableNowPlayingView
        open={nowPlayingVisible}
        bookId={player.playbackDetail?.id ?? null}
        bookSourceKey={player.playbackDetail?.source_path ?? null}
        bookTitle={player.playbackDetail?.title}
        bookAuthor={player.playbackDetail?.author}
        chapters={player.playbackDetail?.chapters ?? EMPTY_CHAPTERS}
        currentChapterNumber={player.currentChapter?.number ?? null}
        playing={player.playing}
        elapsedSec={player.elapsedSec}
        durationSec={player.durationSec}
        canSeek={player.canSeek}
        chapterPlaylist={player.chapterPlaylist}
        playbackRate={playbackRate}
        sleepTimer={sleepTimer}
        sleepRemainingSec={sleepRemainingSec}
        onJumpToChapter={jumpToChapterFromNowPlaying}
        onSeekTo={seekTo}
        onSeekBy={seekBy}
        onPlaybackRateChange={setPlaybackRate}
        onSleepTimerChange={setSleepTimer}
        onClose={closeNowPlaying}
      />
      <StablePlayerPanel
        enabled={player.playerEligible}
        visible={player.playerVisible}
        playing={player.playing}
        title={player.title}
        bookId={player.playbackDetail?.id ?? null}
        bookSourceKey={player.playbackDetail?.source_path ?? null}
        bookTitle={player.playbackDetail?.title}
        bookAuthor={player.playbackDetail?.author}
        chapterTitle={player.currentChapterLabel}
        chapters={player.playbackDetail?.chapters}
        elapsedSec={player.elapsedSec}
        durationSec={player.durationSec}
        canSeek={player.canSeek}
        volume={player.volume}
        canGoPrevious={player.canGoPrevious}
        canGoNext={player.canGoNext}
        chapterPlaylist={player.chapterPlaylist}
        chapterTransport={player.chapterTransport}
        audioRef={player.audioRef}
        audio={player.audioProps}
        onTogglePlay={togglePlay}
        onSeekBy={seekBy}
        onSeekTo={seekTo}
        onPrevious={playPrevious}
        onNext={playNext}
        onVolumeChange={player.setPlayerVolume}
        onClose={closePlayerPanel}
        onToggleNowPlaying={toggleNowPlaying}
        nowPlayingOpen={nowPlayingVisible}
        playbackRate={playbackRate}
        sleepLabel={sleepLabel}
      />
    </main>
    </CoverRefreshContext.Provider>
    </CoverGenerationContext.Provider>
    </CoverClientContext.Provider>
  );
}

function App() {
  const backend = useBackendClient();
  const {
    readyRuntimeHint,
    setupStatus,
    currentSetupStatus,
    setupError,
    checkExhausted,
    libraryOnly,
    runtimeSelectionGeneration,
    onSetupReady,
    onSettingsSetupReady,
    continueLibraryOnly,
    retryCheck,
  } = useRuntimeSetupStatus(backend.apiClient);
  // Keyboard focus never falls back to <body> when its control goes away.
  useEffect(() => installFocusRescue(), []);

  // Screen gating uses the last known status, so losing the backend keeps the
  // dashboard (and its backend-error badge) instead of reopening setup. Only the
  // dashboard's readiness is scoped to the current client.
  const backgroundSetupCheck = showDashboardDuringRuntimeCheck(readyRuntimeHint, setupStatus, setupError);
  const checkingSetup = !libraryOnly
    && !backgroundSetupCheck
    && (setupStatus === null || isCheckingConfiguredRuntime(setupStatus))
    && setupError === null;
  const showingSetup = !checkingSetup && !backgroundSetupCheck && shouldShowRuntimeSetup(setupStatus, libraryOnly);
  const dashboardHidden = checkingSetup || showingSetup;
  // Once shown, the dashboard stays mounted behind setup so playback and jobs survive.
  const [dashboardMounted, setDashboardMounted] = useState(false);
  if (!dashboardMounted && !dashboardHidden) {
    setDashboardMounted(true);
  }

  return (
    <>
      <TooltipLayer />
      {checkingSetup ? (
        <RuntimeSetupChecking
          error={backend.connectionState === "error" ? backend.statusMessage : null}
          exhausted={checkExhausted}
          onRetry={() => {
            if (backend.connectionState === "error") backend.reconnectBackend();
            else retryCheck();
          }}
          onContinueLibraryOnly={continueLibraryOnly}
        />
      ) : showingSetup ? (
        <SetupPage
          apiClient={backend.apiClient}
          initialStatus={setupStatus}
          initialError={setupError}
          onReady={onSetupReady}
          onContinueLibraryOnly={continueLibraryOnly}
          onReconnectBackend={backend.reconnectBackend}
        />
      ) : null}
      {dashboardMounted || !dashboardHidden ? (
        <div hidden={dashboardHidden}>
          <DashboardApp
            hidden={dashboardHidden}
            backend={backend}
            setupStatus={currentSetupStatus}
            conversionRuntime={conversionRuntimeState(currentSetupStatus, {
              checking: isCheckingConfiguredRuntime(currentSetupStatus),
              checkFailed: setupError !== null,
            })}
            runtimeSelectionGeneration={runtimeSelectionGeneration}
            onSettingsSetupReady={onSettingsSetupReady}
            onContinueLibraryOnly={continueLibraryOnly}
            runtimeNotice={backgroundSetupCheck || checkExhausted ? (
              <p className="runtime-check-notice" role="status">
                {checkExhausted ? "Runtime check needs more time. Conversions are unavailable until it finishes." : "Checking your local runtime… You can browse your library while conversions are unavailable."}
                {checkExhausted ? <button type="button" className="btn btn-secondary btn-sm" onClick={retryCheck}>Retry</button> : null}
              </p>
            ) : null}
          />
        </div>
      ) : null}
    </>
  );
}

export default App;
