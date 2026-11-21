import { memo, useEffect, useMemo, useRef, useState } from "react";

import type { LibraryBookSummary, LibraryChapterDetail } from "../../types/api";
import { formatListeningLength, formatRemainingClock } from "../../format/time";
import { BookCover } from "../../ui/BookCover";
import { Icon } from "../../ui/icons";
import { AsciiLoader } from "../../ui/ascii/AsciiLoader";
import { ProgressBar } from "../../ui/ProgressBar";
import { TermSelect } from "../conversion/TermSelect";
import type { UseLibraryResult } from "./useLibrary";
import { LibraryChapterList } from "./LibraryChapterList";
import { useLibraryPanelState } from "./useLibraryPanelState";
import { bookAvailabilityAdvice, bookAvailabilityNote } from "./libraryDisplayModel";
import { readPlaybackResumePosition } from "../player/playbackCheckpoint";
import { chapterStartSeconds } from "../player/chapterPositionModel";
import { PlayingBars } from "../player/PlayingBars";
import { useMountedRef } from "../../state/generationGuard";
import "./library.css";

interface LibraryPanelProps {
  hidden: boolean;
  library: UseLibraryResult;
  /** The album page for the selected book is showing instead of the shelf. */
  albumOpen: boolean;
  onOpenAlbum: (index: number) => void;
  onCloseAlbum: () => void;
  currentChapterNumber?: number | null;
  currentChapterLabel?: string;
  playing?: boolean;
  /** The selected book's audio is loaded in the player. */
  playerLoaded?: boolean;
  elapsedSec?: number;
  durationSec?: number;
  onTogglePlay?: () => void;
  onJumpToChapter?: (chapter: LibraryChapterDetail) => void;
  onStartConvert?: () => void;
  onReleaseBookAudio?: (bookId: number) => void;
  navigationGeneration?: number;
}

const LIBRARY_MODE_OPTIONS = [
  { value: "all", label: "All types" },
  { value: "audiobook", label: "Audiobooks" },
  { value: "document_audio", label: "Documents" },
];

const SKELETON_CARDS = 8;

function clampPercent(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, Math.round(value)));
}

// The edit fields stop at the lengths the library accepts; the count shows once the end is close.
const METADATA_LIMITS = { title: 200, author: 200, tags: 500 } as const;
const LIMIT_HINT_FROM = 0.9;

function limitHint(value: string, limit: number): string | null {
  return value.length >= limit * LIMIT_HINT_FROM ? `${value.length}/${limit}` : null;
}

function chapterCountLabel(count: number): string {
  return `${count} ${count === 1 ? "chapter" : "chapters"}`;
}

// The panel re-renders on every playback tick; a card only changes with its
// own book, its current/playing state, or its position on the shelf.
const BookCard = memo(function BookCard({ book, index, current, playing, onOpenAlbum }: {
  book: LibraryBookSummary;
  index: number;
  current: boolean;
  playing: boolean;
  onOpenAlbum: (index: number) => void;
}) {
  const onOpen = () => onOpenAlbum(index);
  const title = book.title || "Untitled";
  const author = book.author?.trim() || "";
  const note = bookAvailabilityNote(book);
  const advice = bookAvailabilityAdvice(book);
  return (
    <button
      type="button"
      className={`book-card${current ? " is-current" : ""}`}
      aria-current={current ? "true" : undefined}
      aria-label={`${title}${author ? `, ${author}` : ""}${note ? `, ${note}` : ""}`}
      data-tip={note && advice ? `${note}. ${advice}` : undefined}
      onClick={onOpen}
    >
      <span className="book-card-cover">
        <BookCover bookId={book.id} sourceKey={book.source_path} title={book.title} author={book.author} size="tile" />
        {current && playing ? <span className="book-card-playing"><PlayingBars /></span> : null}
      </span>
      {/* No title attributes: the shared tooltip shows whatever an ellipsis cuts. */}
      <span className="book-card-title">{title}</span>
      {author || note ? (
        <span className="book-card-meta">
          {note ? <span className="book-card-partial tabular-nums">{note}</span> : null}
          {note && author ? " · " : null}
          {author}
        </span>
      ) : null}
    </button>
  );
});

export function LibraryPanel({
  hidden,
  library,
  albumOpen,
  onOpenAlbum,
  onCloseAlbum,
  currentChapterNumber,
  currentChapterLabel,
  playing = false,
  playerLoaded = false,
  elapsedSec = 0,
  durationSec = 0,
  onTogglePlay,
  onJumpToChapter,
  onStartConvert,
  onReleaseBookAudio,
  navigationGeneration = 0,
}: LibraryPanelProps) {
  const showEmpty = !library.loadingList && !library.errorMessage && library.items.length === 0;
  // A search/filter may hide the currently playing book. Keep its detail in
  // the library model for the global player, but do not show stale edit fields
  // on the shelf while the book is outside the visible result set. An open
  // album shows its book wherever it is, e.g. one opened by id from a notice.
  const selected = albumOpen || library.selectedBookIndex >= 0 ? library.selectedBookDetail : null;
  const showAlbum = albumOpen && Boolean(selected);
  const chapters = useMemo(() => selected?.chapters ?? [], [selected?.chapters]);
  // Memoized so metadata edits do not re-format every chapter duration.
  const totalDurationMs = useMemo(
    () => chapters.reduce((total, chapter) => total + Math.max(0, Number(chapter.duration_ms || 0)), 0),
    [chapters],
  );
  const totalDurationLabel = useMemo(() => (totalDurationMs > 0 ? formatListeningLength(totalDurationMs / 1000) : ""), [totalDurationMs]);
  const detailTransitionPending = Boolean(
    selected && (library.loadingDetail || selected.id !== library.selectedBookId),
  );
  const [deleteConfirmationBookId, setDeleteConfirmationBookId] = useState<number | null>(null);
  const [deleteFeedback, setDeleteFeedback] = useState<"pending" | "success" | "failure" | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const panelRef = useRef<HTMLElement>(null);
  const previewRef = useRef<HTMLElement>(null);
  const backRef = useRef<HTMLButtonElement>(null);
  const deleteStatusRef = useRef<HTMLParagraphElement>(null);
  const deleteTriggerRef = useRef<HTMLButtonElement>(null);
  const deleteCancelRef = useRef<HTMLButtonElement>(null);
  const restoreDeleteFocusRef = useRef(false);
  const lastAlbumIdRef = useRef<number | null>(null);
  const mountedRef = useMountedRef(true);
  const deleteOwnerRef = useRef<symbol | null>(null);
  const deleteViewOwnershipRef = useRef<(() => boolean) | null>(null);
  const viewKey = `${navigationGeneration}:${library.libraryGeneration ?? 0}:${hidden}`;
  const viewKeyRef = useRef(viewKey);
  viewKeyRef.current = viewKey;
  useEffect(() => { setDeleteFeedback(null); }, [viewKey]);
  useEffect(() => {
    if (deleteFeedback && deleteViewOwnershipRef.current && !deleteViewOwnershipRef.current()) setDeleteFeedback(null);
  });

  useEffect(() => {
    if (deleteConfirmationBookId !== null) {
      deleteCancelRef.current?.focus();
    } else if (restoreDeleteFocusRef.current) {
      restoreDeleteFocusRef.current = false;
      deleteTriggerRef.current?.focus();
    }
  }, [deleteConfirmationBookId]);

  useEffect(() => {
    if (hidden || deleteFeedback === null) return;
    const target = deleteFeedback === "failure" ? deleteTriggerRef.current
      : deleteFeedback === "success" ? panelRef.current?.querySelector<HTMLElement>('.book-card, .library-empty button')
      : deleteStatusRef.current;
    (target ?? deleteStatusRef.current)?.focus();
  }, [deleteFeedback, hidden]);

  // Opening an album moves focus to its back button; closing returns it to the book's card.
  useEffect(() => {
    if (hidden) return;
    if (showAlbum) {
      lastAlbumIdRef.current = selected?.id ?? null;
      backRef.current?.focus({ preventScroll: true });
      panelRef.current?.closest(".main-workspace")?.scrollTo?.({ top: 0 });
    } else if (lastAlbumIdRef.current !== null) {
      const index = library.items.findIndex((item) => item.id === lastAlbumIdRef.current);
      lastAlbumIdRef.current = null;
      panelRef.current?.querySelectorAll<HTMLElement>(".book-card")[index]?.focus();
    }
    // Only an album opening or closing moves focus, not list or detail updates.
  }, [showAlbum, hidden]);

  const cancelDelete = () => {
    restoreDeleteFocusRef.current = true;
    setDeleteConfirmationBookId(null);
  };

  const {
    editTitle,
    editAuthor,
    editTags,
    setEditTitle,
    setEditAuthor,
    setEditTags,
    metadataDirty,
    saveMetadata,
    cleaningPreview,
    cleaningPreviewLoading,
    loadCleaningPreview,
    closeCleaningPreview,
  } = useLibraryPanelState({ library });

  useEffect(() => {
    setDeleteConfirmationBookId(null);
    setEditOpen(false);
  }, [library.selectedBookId, selected?.id]);

  const titleLimit = limitHint(editTitle, METADATA_LIMITS.title);
  const authorLimit = limitHint(editAuthor, METADATA_LIMITS.author);
  const tagsLimit = limitHint(editTags, METADATA_LIMITS.tags);

  const showCleaningPreview = Boolean(cleaningPreview) || cleaningPreviewLoading;
  // The preview opens below the chapter list; bring it into view from a long list.
  useEffect(() => {
    if (showCleaningPreview) previewRef.current?.scrollIntoView?.({ block: "nearest" });
  }, [showCleaningPreview]);
  // Shown beside Save, where the eye is, instead of at the top of the page.
  const metadataSaved = library.statusMessage === "Metadata saved";
  // Hide selection/empty-state echoes, but retain save, repair, and error messages.
  const isRedundantStatus =
    showEmpty || (!library.loadingDetail && library.statusMessage.startsWith("Selected:")) ||
    (!library.loadingDetail && metadataSaved) ||
    (Boolean(library.pendingDeletionPath) && library.statusMessage === "Removed from library, but some files couldn't be deleted");
  const showDetailStatus =
    !library.errorMessage && !isRedundantStatus && (Boolean(library.statusMessage) || library.loadingDetail);
  const availability = selected ? {
    total_chapters: selected.total_chapters,
    audio_available: selected.audio_available,
    source_available: selected.source_available,
    completed_chapters: selected.completed_chapters ?? chapters.filter((chapter) => chapter.mp3_path).length,
  } : null;
  const albumMeta = selected && availability ? [
    selected.conversion_mode === "document_audio" ? "Document" : "Audiobook",
    chapterCountLabel(chapters.length),
    totalDurationLabel,
    selected.tags?.trim(),
    bookAvailabilityNote(availability),
  ].filter(Boolean).join(" · ") : "";
  const albumAdvice = availability ? bookAvailabilityAdvice(availability) : null;
  const filtered = Boolean(library.searchQuery.trim()) || library.modeFilter !== "all";
  // A bare count beside the heading, as in the sidebar and the queue.
  const bookCountLabel = `${library.items.length}${library.hasMore ? "+" : ""}`;
  // Only a book with listening progress resumes; the player reports chapter one even at 0:00.
  const savedPositionSec = selected ? readPlaybackResumePosition(selected) : 0;
  const chapterPlaylist = selected?.playback_status === "chapter_audio";
  const loadedBookPositionSec = elapsedSec + (chapterPlaylist && currentChapterNumber != null
    ? chapterStartSeconds(chapters, currentChapterNumber, false) : 0);
  const resumable = playerLoaded ? loadedBookPositionSec > 0 : savedPositionSec > 0;

  // Continue listening: the book in the player, once it has been started.
  const continueElapsedSec = playerLoaded ? loadedBookPositionSec : savedPositionSec;
  const continueDurationSec = playerLoaded && !chapterPlaylist && durationSec > 0 ? durationSec : totalDurationMs / 1000;
  const showContinue = Boolean(
    selected && library.selectedBookPlayable && chapters.length > 0 && (playing || continueElapsedSec > 0),
  );
  const continuePercent = continueDurationSec > 0 ? clampPercent((continueElapsedSec / continueDurationSec) * 100) : 0;
  const continueRemaining = continueDurationSec > continueElapsedSec
    ? `${formatRemainingClock(continueDurationSec, continueElapsedSec)} left`
    : "";

  const playSelected = () => {
    if (onTogglePlay) {
      onTogglePlay();
      return;
    }
    const resumeChapter = chapters.find((chapter) => chapter.number === currentChapterNumber) ?? chapters[0];
    if (resumeChapter) onJumpToChapter?.(resumeChapter);
  };

  const statusMessages = (
    <>
      {deleteFeedback ? (
        <p ref={deleteStatusRef} tabIndex={-1} role="status" className="field-hint status-sentence">
          {deleteFeedback === "pending" ? "Deleting book…" : deleteFeedback === "failure" ? "Couldn't delete the book. Try again." : "Book deleted"}
        </p>
      ) : null}
      {library.errorMessage ? (
        <p className="field-hint is-error status-sentence" role="alert">{library.errorMessage}</p>
      ) : null}
      {library.pendingDeletionPath ? (
        <div className="field-hint status-sentence" role="status">
          Removed from library, but some files couldn't be deleted.
          <button className="btn btn-ghost btn-sm" type="button" onClick={() => void library.openPendingDeletionFolder?.()}>
            Open folder
          </button>
          <button className="btn btn-ghost btn-sm" type="button" onClick={() => library.dismissPendingDeletion?.()}>
            Dismiss
          </button>
        </div>
      ) : null}
      {showDetailStatus && showAlbum ? (
        <p className="field-hint status-sentence" role="status" aria-live="polite">
          {library.loadingDetail ? <><AsciiLoader active={!hidden} />Loading book…</> : library.statusMessage}
        </p>
      ) : null}
    </>
  );

  return (
    <section
      ref={panelRef}
      id="tab-library"
      className={`panel page-container library-panel${showAlbum ? " is-album" : ""}`}
      role="tabpanel"
      aria-label={showAlbum ? `${selected?.title || "Untitled"} — album` : "Library"}
      aria-labelledby="tab-btn-library"
      hidden={hidden}
    >
      {showAlbum && selected ? (
        <article key={selected.id} className="album-view view-enter" aria-busy={detailTransitionPending}>
          <button ref={backRef} type="button" className="btn btn-ghost btn-sm album-back" onClick={() => {
            deleteOwnerRef.current = null;
            setDeleteFeedback(null);
            onCloseAlbum();
          }}>
            <Icon name="chevron-left" />
            Library
          </button>
          {statusMessages}

          <header className="album-hero">
            <BookCover bookId={selected.id} sourceKey={selected.source_path} title={selected.title} author={selected.author} size="xl" className="album-cover" />
            <div className="album-heading">
              <h2 className="album-title">{selected.title || "Untitled"}</h2>
              {selected.author?.trim() ? <p className="album-author">{selected.author}</p> : null}
              <p className="album-meta tabular-nums">{albumMeta}</p>
              {albumAdvice ? <p id="book-availability-advice" className="album-advice">{albumAdvice}</p> : null}
              {/* A book the library reports as having no audio offers nothing to play or resume. */}
              {selected.audio_available === false ? null : (
                <div className="album-actions">
                  <button
                    id="library-play-btn"
                    className="btn btn-primary album-play"
                    type="button"
                    disabled={detailTransitionPending || !library.selectedBookPlayable || chapters.length === 0 || (!onTogglePlay && !onJumpToChapter)}
                    onClick={playSelected}
                  >
                    <Icon name={playing ? "pause" : "play"} />
                    {playing ? "Pause" : resumable ? "Resume" : "Play"}
                  </button>
                </div>
              )}
            </div>
          </header>

          <LibraryChapterList
            chapters={chapters}
            currentChapterNumber={currentChapterNumber}
            playing={playing}
            detailTransitionPending={detailTransitionPending}
            cleaningPreviewLoading={cleaningPreviewLoading}
            chapterPlaylist={chapterPlaylist}
            playable={library.selectedBookPlayable}
            onJumpToChapter={onJumpToChapter}
            onLoadCleaningPreview={loadCleaningPreview}
          />

          {showCleaningPreview ? (
            <section ref={previewRef} className="album-section cleaning-preview" aria-label="Cleaning preview">
              <div className="album-section-header">
                <h3 className="album-section-title">Original and cleaned text</h3>
                <button className="btn btn-ghost btn-sm" type="button" aria-label="Close text comparison" onClick={closeCleaningPreview}>
                  Close
                </button>
              </div>
              {cleaningPreviewLoading ? <p className="library-preview-loading" role="status"><AsciiLoader active={!hidden} />Loading text…</p> : !cleaningPreview?.available ? (
                <p className="hint">No cleaned text for this chapter yet.</p>
              ) : (
                <div className="cleaning-comparison">
                  <div><h4>Original</h4><pre tabIndex={0} aria-label="Original text">{cleaningPreview.before_text || "Original text isn't available for books converted before this version."}</pre></div>
                  <div><h4>Cleaned</h4><pre tabIndex={0} aria-label="Cleaned text">{cleaningPreview.cleaned_text || "(missing cleaned text)"}</pre></div>
                </div>
              )}
            </section>
          ) : null}

          <section className="album-manage" aria-label="Manage book">
            <div className="album-manage-bar">
              <button
                id="library-edit-toggle"
                className="btn btn-ghost btn-sm"
                type="button"
                aria-expanded={editOpen}
                aria-controls="library-detail-form"
                onClick={() => setEditOpen((open) => !open)}
              >
                Edit details
              </button>
              <button
                id="library-open-folder-btn"
                className="btn btn-ghost btn-sm"
                type="button"
                disabled={detailTransitionPending || !library.selectedBookId}
                data-tip="Show the audio files"
                onClick={() => void library.openSelectedBookFolder()}
              >
                Open folder
              </button>
              <button
                id="library-repair-btn"
                className="btn btn-ghost btn-sm"
                type="button"
                disabled={detailTransitionPending || !library.selectedBookId || !selected.source_available}
                data-tip="Re-create chapters whose audio is missing"
                onClick={() => void library.repairSelectedBook(onReleaseBookAudio)}
              >
                Repair audio
              </button>
              {!selected.source_available ? (
                <span id="book-source-status" className="album-manage-note">Source file missing</span>
              ) : null}
              {deleteConfirmationBookId !== selected.id ? (
                <button
                  id="library-delete-btn"
                  ref={deleteTriggerRef}
                  className="btn btn-ghost btn-sm btn-quiet-danger album-delete"
                  type="button"
                  disabled={detailTransitionPending || !library.selectedBookId}
                  onClick={() => setDeleteConfirmationBookId(selected.id)}
                >
                  Delete book
                </button>
              ) : null}
            </div>

            {deleteConfirmationBookId === selected.id ? (
              <div
                className="inline-confirm"
                onKeyDown={(event) => {
                  if (event.key === "Escape") {
                    event.preventDefault();
                    event.stopPropagation();
                    cancelDelete();
                  }
                }}
              >
                <span className="library-delete-confirmation" role="status">
                  Delete {selected.title || "this book"} and its audio? This cannot be undone.
                </span>
                <button
                  id="library-delete-cancel-btn"
                  ref={deleteCancelRef}
                  className="btn btn-secondary btn-sm"
                  type="button"
                  onClick={cancelDelete}
                >
                  Cancel
                </button>
                <button
                  id="library-delete-confirm-btn"
                  className="btn btn-danger btn-sm"
                  type="button"
                  disabled={detailTransitionPending}
                  onClick={() => {
                    const owner = Symbol();
                    deleteOwnerRef.current = owner;
                    const ownsNavigation = library.captureNavigation?.() ?? (() => true);
                    const ownsView = () => mountedRef.current && deleteOwnerRef.current === owner
                      && viewKeyRef.current === viewKey && ownsNavigation();
                    deleteViewOwnershipRef.current = ownsView;
                    setDeleteConfirmationBookId(null);
                    setDeleteFeedback("pending");
                    void library.deleteSelectedBook(onReleaseBookAudio).then(
                      (deleted) => {
                        if (!ownsView()) return;
                        if (deleted) {
                          // The book is gone, so its album closes and focus lands on the shelf instead.
                          lastAlbumIdRef.current = null;
                          onCloseAlbum();
                        }
                        setDeleteFeedback(deleted ? "success" : "failure");
                      },
                      () => { if (ownsView()) setDeleteFeedback("failure"); },
                    );
                  }}
                >
                  Delete book
                </button>
              </div>
            ) : null}

            {/* Stays mounted while collapsed so the fields keep the book's values. */}
            <div id="library-detail-form" className="library-detail-form" role="group" aria-label="Edit details" hidden={!editOpen}>
              <div className="field">
                <label className="field-label" htmlFor="book-title-edit">Title</label>
                <input
                  id="book-title-edit"
                  maxLength={METADATA_LIMITS.title}
                  className="field-input"
                  type="text"
                  value={editTitle}
                  aria-invalid={!editTitle.trim()}
                  aria-describedby={!editTitle.trim() ? "book-title-error" : titleLimit ? "book-title-limit" : undefined}
                  disabled={detailTransitionPending}
                  onChange={(event) => setEditTitle(event.target.value)}
                />
                {!editTitle.trim() ? (
                  <p id="book-title-error" className="field-hint is-error" role="alert">Title is required</p>
                ) : null}
                {titleLimit ? <p id="book-title-limit" className="field-hint field-limit tabular-nums">{titleLimit}</p> : null}
              </div>
              <div className="field">
                <label className="field-label" htmlFor="book-author-edit">Author</label>
                <input
                  id="book-author-edit"
                  maxLength={METADATA_LIMITS.author}
                  className="field-input"
                  type="text"
                  value={editAuthor}
                  aria-describedby={authorLimit ? "book-author-limit" : undefined}
                  disabled={detailTransitionPending}
                  onChange={(event) => setEditAuthor(event.target.value)}
                />
                {authorLimit ? <p id="book-author-limit" className="field-hint field-limit tabular-nums">{authorLimit}</p> : null}
              </div>
              <div className="field">
                <label className="field-label" htmlFor="book-tags-edit">Tags</label>
                <input
                  id="book-tags-edit"
                  maxLength={METADATA_LIMITS.tags}
                  className="field-input"
                  type="text"
                  placeholder="Comma separated"
                  value={editTags}
                  aria-describedby={tagsLimit ? "book-tags-limit" : undefined}
                  disabled={detailTransitionPending}
                  onChange={(event) => setEditTags(event.target.value)}
                />
                {tagsLimit ? <p id="book-tags-limit" className="field-hint field-limit tabular-nums">{tagsLimit}</p> : null}
              </div>
              <div className="library-metadata-footer">
                <span id="library-metadata-status" className="field-hint" role="status">
                  {metadataSaved && !metadataDirty ? "Saved" : ""}
                </span>
                <button
                  id="library-save-metadata-btn"
                  className="btn btn-secondary"
                  type="button"
                  disabled={detailTransitionPending || library.savingMetadata || !editTitle.trim() || !metadataDirty}
                  onClick={() => void saveMetadata()}
                >
                  Save
                </button>
              </div>
            </div>
          </section>
        </article>
      ) : (
        <div className="shelf-view view-enter">
          {showContinue && selected ? (
            <section className="continue-card" aria-label="Continue listening">
              <BookCover bookId={selected.id} sourceKey={selected.source_path} title={selected.title} author={selected.author} size="tile" className="continue-cover" />
              <div className="continue-copy">
                <span className="eyebrow">{playing ? "Now playing" : "Continue listening"}</span>
                <button type="button" className="continue-title" onClick={() => onOpenAlbum(library.selectedBookIndex)}>
                  {selected.title || "Untitled"}
                </button>
                <span className="continue-meta">
                  {[selected.author?.trim(), playerLoaded ? currentChapterLabel : ""].filter(Boolean).join(" · ")}
                </span>
                <div className="continue-progress">
                  <ProgressBar pct={continuePercent} tone="neutral" size="hairline" label="Listening progress" />
                  <span className="continue-progress-text tabular-nums">
                    {continuePercent}%{continueRemaining ? ` · ${continueRemaining}` : ""}
                  </span>
                </div>
              </div>
              <button className="btn btn-primary continue-play" type="button" onClick={playSelected} disabled={!onTogglePlay && !onJumpToChapter}>
                <Icon name={playing ? "pause" : "play"} />
                {playing ? "Pause" : "Resume"}
              </button>
            </section>
          ) : null}

          <div className="shelf-header">
            <h2 className="shelf-title">
              All books
              <span className="shelf-count tabular-nums" aria-live="polite">{bookCountLabel}</span>
            </h2>
            <div className="shelf-tools" aria-label="Library filters" role="group">
              <div className="search-field library-search-wrap">
                <Icon name="search" className="search-icon" />
                <label className="sr-only" htmlFor="library-search">Search</label>
                <input
                  id="library-search"
                  className="field-input search-input"
                  type="search"
                  placeholder="Title or author…"
                  value={library.searchQuery}
                  onChange={(event) => library.setSearchQuery(event.target.value)}
                />
              </div>
              <TermSelect
                id="library-mode-filter"
                className="library-mode-filter"
                label="Library type filter"
                value={library.modeFilter}
                options={LIBRARY_MODE_OPTIONS}
                placeholder="All types"
                onChange={(value) => library.setModeFilter(value as typeof library.modeFilter)}
              />
              <button
                className="btn btn-ghost btn-icon library-refresh-btn"
                type="button"
                disabled={library.loadingList}
                aria-label="Refresh library"
                data-tip="Refresh"
                data-tip-pos="bottom"
                onClick={() => void library.refreshLibrary()}
              >
                <Icon name="refresh" />
              </button>
            </div>
          </div>

          {statusMessages}

          <div id="library-list" className="book-grid" aria-busy={library.loadingList} aria-label="Books" role="list">
            {library.loadingList && library.items.length === 0 ? (
              <>
                <span className="sr-only" role="status">Loading library…</span>
                {Array.from({ length: SKELETON_CARDS }, (_, index) => (
                  <span key={index} className="book-card is-skeleton" role="listitem" aria-hidden="true">
                    <span className="skeleton skeleton-cover" />
                    <span className="skeleton skeleton-line" />
                    <span className="skeleton skeleton-line is-short" />
                  </span>
                ))}
              </>
            ) : null}
            {library.items.map((item, index) => {
              const current = item.id === library.selectedBookId && (playing || playerLoaded);
              return (
                <div key={item.id} role="listitem" className="book-grid-item">
                  <BookCard
                    book={item}
                    index={index}
                    current={current}
                    playing={current && playing}
                    onOpenAlbum={onOpenAlbum}
                  />
                </div>
              );
            })}
          </div>

          {showEmpty ? (
            library.indexedCount === 0 ? (
              <div className="empty-state library-empty">
                <span className="empty-state-icon" aria-hidden="true"><Icon name="library" /></span>
                <p className="empty-state-title">No books yet</p>
                <p className="empty-state-hint">Add a book or paste some text and Orpheus will narrate it.</p>
                {onStartConvert ? (
                  <button className="btn btn-primary" type="button" onClick={onStartConvert}>
                    Create an audiobook
                  </button>
                ) : null}
              </div>
            ) : (
              <div className="empty-state library-empty">
                <span className="empty-state-icon" aria-hidden="true"><Icon name="search" /></span>
                <p className="empty-state-title">No matches</p>
                <p className="empty-state-hint">Try a different search or filter.</p>
                {filtered ? (
                  <button
                    className="btn btn-secondary"
                    type="button"
                    onClick={() => {
                      library.setSearchQuery("");
                      library.setModeFilter("all");
                      document.getElementById("library-search")?.focus();
                    }}
                  >
                    Clear filters
                  </button>
                ) : null}
              </div>
            )
          ) : null}

          {library.hasMore ? (
            <button
              className="btn btn-ghost library-load-more"
              type="button"
              disabled={library.loadingMore}
              onClick={() => void library.loadMore()}
            >
              {library.loadingMore ? "Loading…" : `Load more (${library.items.length} loaded)`}
            </button>
          ) : null}
        </div>
      )}
    </section>
  );
}
