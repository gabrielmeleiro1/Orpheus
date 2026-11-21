// Isolated production-browser fixture. Imported only by frontend_stress/performance.mjs.
import { createRoot } from "react-dom/client";
import { flushSync } from "react-dom";

import { LibraryPanel } from "../features/library/LibraryPanel";
import { useAudioPlayer } from "../features/player/useAudioPlayer";
import type { UseLibraryResult } from "../features/library/useLibrary";
import type { LibraryBookDetail, LibraryBookSummary } from "../types/api";
import "../styles.css";

const asyncNoop = async () => undefined;
const noop = () => undefined;
const nextFrame = () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));

function fixture(chapterCount: number, bookCount: number): UseLibraryResult {
  const detail: LibraryBookDetail = {
    id: 1, title: "Performance fixture", author: "Fixture author", source_path: null,
    source_type: "epub", conversion_mode: "audiobook", tags: "", favorite: false,
    source_available: true, total_chapters: chapterCount, playback_path: null,
    playback_status: "ready", playback_position_sec: 0,
    chapters: Array.from({ length: chapterCount }, (_, index) => ({
      id: index + 1, number: index + 1, title: `Chapter ${index + 1}`,
      duration_ms: 60_000, mp3_path: `/fixture/chapter-${index + 1}.mp3`,
    })),
  };
  const items: LibraryBookSummary[] = Array.from({ length: bookCount }, (_, index) => ({
    id: index + 1, title: `Book ${index + 1}`, author: "Fixture author", source_path: null,
    conversion_mode: "audiobook", tags: "", favorite: false, source_available: true,
    total_chapters: chapterCount, completed_chapters: chapterCount,
  }));
  return {
    searchQuery: "", setSearchQuery: noop, modeFilter: "all", setModeFilter: noop,
    items, unfilteredItems: items, indexedCount: bookCount, hasMore: false, selectedBookId: 1,
    selectedBookIndex: 0, selectedBookDetail: detail, selectedBookPlayable: false,
    loadingList: false, loadingMore: false, loadingDetail: false,
    errorMessage: "", statusMessage: "", bookDetailText: "", maintenanceReport: null,
    maintenanceLoading: false, maintenanceMessage: "", canOpenAppData: false,
    refreshLibrary: asyncNoop, loadMore: asyncNoop, selectBook: async () => detail,
    selectBookById: async () => detail,
    selectRelative: async () => detail, updateSelectedBookMetadata: asyncNoop,
    loadCleaningPreview: async () => null, repairSelectedBook: asyncNoop,
    loadMaintenanceDiagnostics: asyncNoop, backupDatabase: asyncNoop,
    cleanupStorage: asyncNoop, openSelectedBookFolder: asyncNoop,
    openAppDataFolder: asyncNoop, deleteSelectedBook: async () => false,
    setSelectedPlaybackPosition: noop,
  };
}

async function measureLibrary(chapterCount: number, bookCount: number) {
  const library = fixture(chapterCount, bookCount);
  const container = document.getElementById("root")!;
  const root = createRoot(container);
  const render = (chapter: number) => root.render(
    <LibraryPanel hidden={false} library={library} albumOpen onOpenAlbum={noop} onCloseAlbum={noop}
      currentChapterNumber={chapter} playing onJumpToChapter={noop} />,
  );
  const started = performance.now();
  flushSync(() => render(1));
  void container.offsetHeight;
  const mountMs = performance.now() - started;
  await nextFrame();
  await nextFrame();

  const chapterUpdates: number[] = [];
  const titleEdits: number[] = [];
  const input = container.querySelector<HTMLInputElement>("#book-title-edit")!;
  const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
  for (let index = 0; index < 24; index += 1) {
    const updateStart = performance.now();
    flushSync(() => render((index + 1) % chapterCount + 1));
    void container.offsetHeight;
    if (index >= 4) chapterUpdates.push(performance.now() - updateStart);
    const editStart = performance.now();
    flushSync(() => {
      setValue.call(input, `Edited title ${index}`);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    void container.offsetHeight;
    if (index >= 4) titleEdits.push(performance.now() - editStart);
    await nextFrame();
  }
  const chapterRows = container.querySelectorAll(".chapter-row").length;
  // The album replaces the shelf, so none of the 5,000 book cards may render.
  const bookRows = container.querySelectorAll(".book-card").length;
  if (chapterRows !== chapterCount || bookRows !== 0 || input.value !== "Edited title 23"
    || !container.querySelector('.chapter-row.is-current .chapter-main[aria-current="true"]')) {
    throw new Error("Library workload did not reach its expected state");
  }
  flushSync(() => root.unmount());
  return { chapterCount, bookCount, chapterRows, bookRows, mountMs, chapterUpdates, titleEdits };
}

// The shelf stays on screen while a book plays; each playback second
// re-renders the panel with a new position.
async function measureShelf(bookCount: number) {
  const library = { ...fixture(12, bookCount), selectedBookPlayable: true };
  const container = document.getElementById("root")!;
  const root = createRoot(container);
  const render = (elapsedSec: number) => root.render(
    <LibraryPanel hidden={false} library={library} albumOpen={false} onOpenAlbum={noop} onCloseAlbum={noop}
      currentChapterNumber={1} playing playerLoaded elapsedSec={elapsedSec} durationSec={720}
      onTogglePlay={noop} onJumpToChapter={noop} />,
  );
  const started = performance.now();
  flushSync(() => render(1));
  void container.offsetHeight;
  const mountMs = performance.now() - started;
  await nextFrame();
  await nextFrame();
  const tickMs: number[] = [];
  for (let index = 0; index < 24; index += 1) {
    const tickStart = performance.now();
    flushSync(() => render(index + 2));
    void container.offsetHeight;
    if (index >= 4) tickMs.push(performance.now() - tickStart);
    await nextFrame();
  }
  const cards = container.querySelectorAll(".book-card").length;
  if (cards !== bookCount || !container.querySelector(".continue-card")) {
    throw new Error("Shelf workload did not reach its expected state");
  }
  flushSync(() => root.unmount());
  return { bookCount, cards, mountMs, tickMs };
}

async function measurePlayer(chapterCount: number) {
  const library = { ...fixture(chapterCount, 1), selectedBookPlayable: true };
  const container = document.getElementById("root")!;
  const root = createRoot(container);
  function Player() {
    const player = useAudioPlayer({ apiClient: null, library });
    return <audio ref={player.audioRef} onTimeUpdate={player.audioProps.onTimeUpdate}
      data-chapter={player.currentChapter?.number} />;
  }
  flushSync(() => root.render(<Player />));
  await nextFrame();
  const audio = container.querySelector("audio")!;
  // Paused media events exercise the same position update without media, API
  // calls or audio rendering. Every event changes the displayed second.
  const tickBatchMs = [];
  for (let batch = 0; batch < 8; batch += 1) {
    const started = performance.now();
    for (let tick = 0; tick < 500; tick += 1) {
      audio.currentTime = (batch * 500 + tick) % (chapterCount * 60);
      flushSync(() => audio.dispatchEvent(new Event("timeupdate", { bubbles: true })));
    }
    if (batch > 1) tickBatchMs.push(performance.now() - started);
    await nextFrame();
  }
  if (Number(audio.dataset.chapter) !== Math.floor(audio.currentTime / 60) + 1) {
    throw new Error("Player workload did not update the current chapter");
  }
  flushSync(() => root.unmount());
  return { chapterCount, ticksPerBatch: 500, tickBatchMs };
}

declare global {
  interface Window {
    orpheusPerformance: { measureLibrary: typeof measureLibrary; measureShelf: typeof measureShelf; measurePlayer: typeof measurePlayer };
  }
}
window.orpheusPerformance = { measureLibrary, measureShelf, measurePlayer };
