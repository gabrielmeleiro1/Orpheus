import { useEffect, useMemo, useRef, useState } from "react";

import type { CleaningPreviewResponse, LibraryChapterDetail } from "../../types/api";
import { formatDurationClock } from "../../format/time";
import { TermSelect } from "../conversion/TermSelect";
import { chapterPlaybackStatusLabel } from "../player/chapterPlaybackLabel";
import type { UseLibraryResult } from "./useLibrary";
import { formatLibraryMeta } from "./useLibrary";
import { useBookListWindow } from "./useBookListWindow";
import { useLibraryPanelState } from "./useLibraryPanelState";
import "./library.css";

interface LibraryPanelProps {
  hidden: boolean;
  library: UseLibraryResult;
  currentChapterNumber?: number | null;
  playing?: boolean;
  onJumpToChapter?: (chapter: LibraryChapterDetail) => void;
  onStartConvert?: () => void;
}

function previewText(preview: CleaningPreviewResponse | null): string {
  if (!preview) {
    return "Select a chapter preview.";
  }
  if (!preview.available) {
    return "No cleaning artifacts are available for this chapter yet.";
  }
  const before = preview.before_text?.trim() || "(missing before text)";
  const cleaned = preview.cleaned_text?.trim() || "(missing cleaned text)";
  return [`before:\n${before.slice(0, 1200)}`, `cleaned:\n${cleaned.slice(0, 1200)}`].join("\n\n---\n\n");
}

const LIBRARY_MODE_OPTIONS = [
  { value: "all", label: "All types" },
  { value: "audiobook", label: "Audiobooks" },
  { value: "document_audio", label: "Documents" },
];

export function LibraryPanel({
  hidden,
  library,
  currentChapterNumber,
  playing = false,
  onJumpToChapter,
  onStartConvert,
}: LibraryPanelProps) {
  const showEmpty = !library.loadingList && !library.errorMessage && library.items.length === 0;
  // A search/filter may hide the currently playing book. Keep its detail in
  // the library model for the global player, but do not show stale edit fields
  // while the book is outside the visible result set.
  const selected = library.selectedBookIndex >= 0 ? library.selectedBookDetail : null;
  const chapters = useMemo(() => selected?.chapters ?? [], [selected?.chapters]);
  const detailTransitionPending = Boolean(
    selected && (library.loadingDetail || selected.id !== library.selectedBookId),
  );
  const [deleteConfirmationBookId, setDeleteConfirmationBookId] = useState<number | null>(null);
  const [cleanupConfirmationArmed, setCleanupConfirmationArmed] = useState(false);
  const cleanupConfirmationRef = useRef(false);
  const {
    editTitle,
    editAuthor,
    editTags,
    setEditTitle,
    setEditAuthor,
    setEditTags,
    cleaningPreview,
    cleaningPreviewLoading,
    loadCleaningPreview,
  } = useLibraryPanelState({ library });

  useEffect(() => {
    setDeleteConfirmationBookId(null);
    cleanupConfirmationRef.current = false;
    setCleanupConfirmationArmed(false);
  }, [library.selectedBookId, selected?.id]);

  const showPager = library.items.length > 1 || library.hasMore;
  const selectedAtLoadedEnd = library.selectedBookIndex >= library.items.length - 1;
  const { containerRef: listContainerRef, window: listWindow } = useBookListWindow({
    itemCount: library.items.length,
    selectedIndex: library.selectedBookIndex,
  });
  const showCleaningPreview = Boolean(cleaningPreview) || cleaningPreviewLoading;
  // The detail-pane status line stays quiet for the two echoes the form/list
  // already convey: the empty-library note and the "Selected: <title>" message
  // (findings 11 & 14). Real messages (saved, repaired, errors) still show.
  const isRedundantStatus =
    showEmpty || (!library.loadingDetail && library.statusMessage.startsWith("Selected:"));
  const showDetailStatus =
    !library.errorMessage && !isRedundantStatus && (Boolean(library.statusMessage) || library.loadingDetail);

  return (
    <section
      id="tab-library"
      className="panel"
      role="tabpanel"
      aria-label="Library and playback"
      aria-labelledby="tab-btn-library"
      hidden={hidden}
    >
      <div className="panel-header">
        <span>Library</span>
        <span className="panel-header-meta">{library.items.length}{library.hasMore ? "+" : ""} {library.items.length === 1 ? "book" : "books"}</span>
      </div>

      <div className="library-workspace">
        <aside className="panel-section library-sidebar" aria-label="Library navigation">
          <div className="library-search-row">
            <label className="field-label" htmlFor="library-search">
              Search
            </label>
            <input
              id="library-search"
              className="field-input library-search-field"
              type="text"
              placeholder="Title or author…"
              value={library.searchQuery}
              onChange={(event) => library.setSearchQuery(event.target.value)}
            />
          </div>

          <div className="library-filter-row">
            <TermSelect
              id="library-mode-filter"
              label="Library type filter"
              value={library.modeFilter}
              options={LIBRARY_MODE_OPTIONS}
              placeholder="All types"
              onChange={(value) => library.setModeFilter(value as typeof library.modeFilter)}
            />
            <button
              className="btn btn-ghost btn-compact library-refresh-btn"
              type="button"
              disabled={library.loadingList}
              data-tip="Reload the library index"
              data-tip-pos="bottom"
              onClick={() => void library.refreshLibrary()}
            >
              Refresh
            </button>
          </div>

          {showPager ? (
            <div className="library-pager">
              <button
                className="btn btn-ghost btn-compact"
                type="button"
                disabled={library.selectedBookIndex <= 0}
                onClick={() => void library.selectRelative(-1)}
              >
                Previous
              </button>
              <button
                className="btn btn-ghost btn-compact"
                type="button"
                disabled={
                  library.selectedBookIndex < 0
                  || (selectedAtLoadedEnd && (library.loadingMore || !library.hasMore))
                }
                onClick={() => void library.selectRelative(1)}
              >
                Next
              </button>
            </div>
          ) : null}

          <div id="library-list" className="library-list" ref={listContainerRef}>
            {library.loadingList ? <p className="hint">Loading library…</p> : null}
            {showEmpty ? (
              library.indexedCount === 0 ? (
                <div className="empty-state library-empty">
                  <p className="empty-state-title">No audiobooks yet</p>
                  <p className="empty-state-hint">Convert a book or document to build your library.</p>
                  {onStartConvert ? (
                    <button className="btn btn-primary btn-compact" type="button" onClick={onStartConvert}>
                      Convert a source
                    </button>
                  ) : null}
                </div>
              ) : (
                <div className="empty-state library-empty">
                  <p className="empty-state-title">No matches</p>
                  <p className="empty-state-hint">Try a different search or filter.</p>
                </div>
              )
            ) : null}
            {listWindow.paddingTop > 0 ? (
              <div className="library-list-spacer" aria-hidden="true" style={{ height: listWindow.paddingTop }} />
            ) : null}
            {library.items.slice(listWindow.startIndex, listWindow.endIndex).map((item, offset) => {
              const index = listWindow.startIndex + offset;
              const isSelected = item.id === library.selectedBookId;
              return (
                <button
                  key={item.id}
                  type="button"
                  className={`book-row${isSelected ? " is-selected" : ""}`}
                  aria-current={isSelected ? "true" : undefined}
                  onClick={() => void library.selectBook(index)}
                >
                  <span className="book-title">{item.title || "Untitled"}</span>
                  <span className="book-meta">{formatLibraryMeta(item)}</span>
                </button>
              );
            })}
            {listWindow.paddingBottom > 0 ? (
              <div className="library-list-spacer" aria-hidden="true" style={{ height: listWindow.paddingBottom }} />
            ) : null}
            {library.hasMore ? (
              <button
                className="btn btn-ghost btn-compact library-load-more"
                type="button"
                disabled={library.loadingMore}
                data-tip="Fetch the next page of books"
                onClick={() => void library.loadMore()}
              >
                {library.loadingMore ? "Loading…" : `Load more (${library.items.length} loaded)`}
              </button>
            ) : null}
          </div>
        </aside>

        <div
          className={`library-detail-pane${detailTransitionPending ? " is-loading" : ""}`}
          aria-busy={detailTransitionPending}
        >
          {library.errorMessage ? (
            <p className="field-hint is-error status-sentence" role="alert">{library.errorMessage}</p>
          ) : null}
          {showDetailStatus ? (
            <p className="field-hint status-sentence" role="status" aria-live="polite">
              {library.loadingDetail ? "Loading selected book…" : library.statusMessage}
            </p>
          ) : null}

          {selected ? (
            <>
              <div className="library-detail-form">
                <div className="form-grid">
                  <div className="field-row">
                    <div className="field">
                      <label className="field-label" htmlFor="book-title-edit">Title</label>
                      <input
                        id="book-title-edit"
                        className="field-input"
                        type="text"
                        value={editTitle}
                        disabled={detailTransitionPending}
                        onChange={(event) => setEditTitle(event.target.value)}
                      />
                    </div>
                    <div className="field">
                      <label className="field-label" htmlFor="book-author-edit">Author</label>
                      <input
                        id="book-author-edit"
                        className="field-input"
                        type="text"
                        value={editAuthor}
                        disabled={detailTransitionPending}
                        onChange={(event) => setEditAuthor(event.target.value)}
                      />
                    </div>
                  </div>
                  <div className="field">
                    <label className="field-label" htmlFor="book-tags-edit">Tags</label>
                    <input
                      id="book-tags-edit"
                      className="field-input"
                      type="text"
                      placeholder="Comma separated"
                      value={editTags}
                      disabled={detailTransitionPending}
                      onChange={(event) => setEditTags(event.target.value)}
                    />
                  </div>
                  <p
                    id="book-source-status"
                    className="field-hint"
                    data-tip="Whether the original source file is still on disk. Repair needs it."
                    data-tip-pos="right"
                  >
                    {selected.source_available ? "Source file available" : "Source file missing — repair unavailable"}
                  </p>
                </div>
              </div>

              <div className="library-detail-actions">
                <button
                  id="library-save-metadata-btn"
                  className="btn btn-primary btn-compact"
                  type="button"
                  disabled={detailTransitionPending}
                  data-tip="Write title, author, and tags into the audio files"
                  onClick={() =>
                    void library.updateSelectedBookMetadata({
                      title: editTitle,
                      author: editAuthor,
                      tags: editTags,
                    })
                  }
                >
                  Save changes
                </button>
                <button
                  id="library-open-folder-btn"
                  className="btn btn-ghost btn-compact"
                  type="button"
                  disabled={detailTransitionPending || !library.selectedBookId}
                  data-tip="Reveal the exported audio in the file manager"
                  onClick={() => void library.openSelectedBookFolder()}
                >
                  Open folder
                </button>
                <div className="library-detail-actions-danger">
                  <button
                    id="library-repair-btn"
                    className="btn btn-ghost btn-compact"
                    type="button"
                    disabled={detailTransitionPending || !library.selectedBookId}
                    data-tip="Re-synthesize chapters whose audio files are missing. Needs the original source."
                    onClick={() => void library.repairSelectedBook()}
                  >
                    Repair
                  </button>
                  {deleteConfirmationBookId === selected.id ? (
                    <>
                      <span className="field-hint library-delete-confirmation" role="status">
                        Delete {selected.title || "this book"}? This cannot be undone.
                      </span>
                      <button
                        id="library-delete-cancel-btn"
                        className="btn btn-ghost btn-compact"
                        type="button"
                        onClick={() => setDeleteConfirmationBookId(null)}
                      >
                        Cancel
                      </button>
                      <button
                        id="library-delete-confirm-btn"
                        className="btn btn-danger btn-compact"
                        type="button"
                        disabled={detailTransitionPending}
                        onClick={() => {
                          setDeleteConfirmationBookId(null);
                          void library.deleteSelectedBook();
                        }}
                      >
                        Delete book
                      </button>
                    </>
                  ) : (
                    <button
                      id="library-delete-btn"
                      className="btn btn-danger btn-compact"
                      type="button"
                      disabled={detailTransitionPending || !library.selectedBookId}
                      data-tip="Remove this book and its audio from the library"
                      onClick={() => setDeleteConfirmationBookId(selected.id)}
                    >
                      Delete
                    </button>
                  )}
                </div>
              </div>

              <div className="panel-section chapter-section">
                <div className="section-title"><span>Chapters</span></div>
                <div id="chapter-list" className="chapter-list">
                  {chapters.length === 0 ? (
                    <p className="hint chapter-empty">No chapters yet.</p>
                  ) : (
                    chapters.map((chapter) => {
                      const isCurrent = chapter.number === currentChapterNumber;
                      const playbackLabel = chapterPlaybackStatusLabel({ isCurrent, playing });
                      return (
                        <div key={chapter.id} className={`chapter-row${isCurrent ? " is-current" : ""}`}>
                          <button
                            type="button"
                            className="chapter-main"
                            aria-current={isCurrent ? "true" : undefined}
                            aria-label={
                              playbackLabel
                                ? `${chapter.title || `Chapter ${chapter.number}`} — ${playbackLabel.toLowerCase()}`
                                : undefined
                            }
                            data-tip="Play from this chapter"
                            onClick={() => onJumpToChapter?.(chapter)}
                            disabled={detailTransitionPending || !onJumpToChapter}
                          >
                            <span className="chapter-number tabular-nums">{String(chapter.number).padStart(2, "0")}</span>
                            <span className="chapter-title">
                              {chapter.title || `Chapter ${chapter.number}`}
                            </span>
                            <span className="chapter-duration tabular-nums">{formatDurationClock(chapter.duration_ms)}</span>
                          </button>
                          <button
                            className="btn btn-ghost btn-compact chapter-cleaned-btn"
                            type="button"
                            disabled={detailTransitionPending || cleaningPreviewLoading}
                            data-tip="Compare this chapter's text before and after cleaning"
                            onClick={() => void loadCleaningPreview(chapter.number)}
                          >
                            Cleaned text
                          </button>
                        </div>
                      );
                    })
                  )}
                </div>
              </div>

              {showCleaningPreview ? (
                <details className="library-disclosure cleaning-preview-toggle" open>
                  <summary>Cleaning preview</summary>
                  <pre id="cleaning-preview">
                    {cleaningPreviewLoading ? "Loading cleaning preview…" : previewText(cleaningPreview)}
                  </pre>
                </details>
              ) : null}
            </>
          ) : showEmpty ? null : (
            <div className="empty-state">
              <p className="empty-state-title">No book selected</p>
              <p className="empty-state-hint">
                Choose a book from the list to see its chapters, edit its details, and play it.
              </p>
            </div>
          )}

          <details className="library-disclosure maintenance-toggle">
            <summary>Maintenance</summary>
            <div className="maintenance-body">
              <div className="maintenance-actions">
                <button
                  className="btn btn-compact"
                  type="button"
                  disabled={library.maintenanceLoading}
                  data-tip="Audit storage for orphaned files and missing chapters"
                  onClick={() => void library.loadMaintenanceDiagnostics()}
                >
                  Diagnostics
                </button>
                <button
                  className="btn btn-compact"
                  type="button"
                  disabled={library.maintenanceLoading}
                  data-tip="Copy the library database to a timestamped backup"
                  onClick={() => void library.backupDatabase()}
                >
                  Back up database
                </button>
                {cleanupConfirmationArmed ? (
                  <>
                    <span className="field-hint library-cleanup-confirmation" role="status">
                      Clean stale storage records and orphan files? A database backup will be created first.
                    </span>
                    <button
                      id="library-cleanup-cancel-btn"
                      className="btn btn-ghost btn-compact"
                      type="button"
                      onClick={() => {
                        cleanupConfirmationRef.current = false;
                        setCleanupConfirmationArmed(false);
                      }}
                    >
                      Cancel
                    </button>
                    <button
                      id="library-cleanup-confirm-btn"
                      className="btn btn-danger btn-compact"
                      type="button"
                      disabled={library.maintenanceLoading}
                      onClick={() => {
                        if (!cleanupConfirmationRef.current) {
                          return;
                        }
                        cleanupConfirmationRef.current = false;
                        setCleanupConfirmationArmed(false);
                        void library.cleanupStorage();
                      }}
                    >
                      Clean up storage
                    </button>
                  </>
                ) : (
                  <button
                    id="library-cleanup-btn"
                    className="btn btn-compact"
                    type="button"
                    disabled={library.maintenanceLoading}
                    data-tip="Remove orphaned temp files. Finished audio is never touched."
                    onClick={() => {
                      cleanupConfirmationRef.current = true;
                      setCleanupConfirmationArmed(true);
                    }}
                  >
                    Clean up storage
                  </button>
                )}
                <button
                  className="btn btn-compact"
                  type="button"
                  disabled={!library.canOpenAppData}
                  data-tip="Open the application data folder"
                  onClick={() => void library.openAppDataFolder()}
                >
                  App data
                </button>
              </div>
              {library.maintenanceMessage ? (
                <p className="field-hint status-sentence" role="status" aria-live="polite">{library.maintenanceMessage}</p>
              ) : null}
              {library.maintenanceReport ? (
                <div className="maintenance-grid">
                  <div><span>Database</span><strong>{library.maintenanceReport.database_path}</strong></div>
                  <div><span>Output</span><strong>{library.maintenanceReport.output_path}</strong></div>
                  <div><span>Temp uploads</span><strong className="tabular-nums">{library.maintenanceReport.audit.stale_temp_uploads?.length ?? 0}</strong></div>
                  <div><span>Orphan folders</span><strong className="tabular-nums">{library.maintenanceReport.audit.orphan_output_dirs?.length ?? 0}</strong></div>
                  <div><span>Stale sources</span><strong className="tabular-nums">{library.maintenanceReport.audit.stale_book_sources?.length ?? 0}</strong></div>
                  <div><span>Missing chapters</span><strong className="tabular-nums">{library.maintenanceReport.audit.stale_chapter_rows?.length ?? 0}</strong></div>
                </div>
              ) : null}
              {selected ? (
                <details className="library-disclosure raw-metadata-toggle">
                  <summary>Raw metadata</summary>
                  <pre id="book-detail">{library.bookDetailText}</pre>
                </details>
              ) : null}
            </div>
          </details>
        </div>
      </div>
    </section>
  );
}
