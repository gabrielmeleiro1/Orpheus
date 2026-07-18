import { useMemo } from "react";

import type { CleaningPreviewResponse, LibraryChapterDetail } from "../../types/api";
import { formatDurationClock } from "../../format/time";
import { TermSelect } from "../conversion/TermSelect";
import { chapterPlaybackStatusLabel } from "../player/chapterPlaybackLabel";
import type { UseLibraryResult } from "./useLibrary";
import { formatLibraryMeta } from "./useLibrary";
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
  const selected = library.selectedBookDetail;
  const chapters = useMemo(() => selected?.chapters ?? [], [selected?.chapters]);
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

  return (
    <section
      id="tab-library"
      className="panel"
      role="tabpanel"
      aria-label="Library and playback"
      hidden={hidden}
    >
      <div className="panel-header">
        <span>Library</span>
        <span className="panel-header-meta">{library.items.length} {library.items.length === 1 ? "book" : "books"}</span>
      </div>

      <div className="library-workspace">
        <aside className="panel-section library-sidebar" aria-label="Library navigation">
          <div className="library-toolbar library-nav-actions">
            <button
              className="btn btn-compact"
              type="button"
              disabled={library.loadingList}
              data-tip="Reload the library index"
              data-tip-pos="bottom"
              onClick={() => void library.refreshLibrary()}
            >
              Refresh
            </button>
            <button
              className="btn btn-compact"
              type="button"
              disabled={library.selectedBookIndex <= 0}
              onClick={() => void library.selectRelative(-1)}
            >
              Prev
            </button>
            <button
              className="btn btn-compact"
              type="button"
              disabled={library.selectedBookIndex < 0 || library.selectedBookIndex >= library.items.length - 1}
              onClick={() => void library.selectRelative(1)}
            >
              Next
            </button>
          </div>

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
          </div>

          <div id="library-list" className="library-list">
            {library.loadingList ? <div className="log-line">Loading library…</div> : null}
            {showEmpty ? (
              <div className="library-empty">
                <div className="log-line">No converted books yet.</div>
                {onStartConvert ? (
                  <button className="btn btn-compact" type="button" onClick={onStartConvert}>
                    Convert a source
                  </button>
                ) : null}
              </div>
            ) : null}
            {library.items.map((item, index) => {
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
          </div>
        </aside>

        <div className="library-detail-pane">
          <div className="library-detail-header">
            <div>
              <span className="progress-kicker">Selected book</span>
              <div id="book-summary-title" className="book-summary-value library-title-mirror">
                {library.bookSummaryTitle}
              </div>
              <span id="book-summary-meta" className="book-summary-value">
                {library.bookSummaryMeta}
              </span>
            </div>
            <span id="book-summary-extra" className="book-summary-value">
              {library.bookSummaryExtra}
            </span>
          </div>

          <div className="panel-section book-detail-section library-detail-form">
            <div className="form-row">
              <span className="form-row-label">Title</span>
              <div className="form-row-field">
                <input
                  id="book-title-edit"
                  className="field-input"
                  type="text"
                  value={editTitle}
                  disabled={!selected}
                  onChange={(event) => setEditTitle(event.target.value)}
                />
              </div>
            </div>
            <div className="form-row">
              <span className="form-row-label">Author</span>
              <div className="form-row-field">
                <input
                  id="book-author-edit"
                  className="field-input"
                  type="text"
                  value={editAuthor}
                  disabled={!selected}
                  onChange={(event) => setEditAuthor(event.target.value)}
                />
              </div>
            </div>
            <div className="form-row">
              <span className="form-row-label">Tags</span>
              <div className="form-row-field">
                <input
                  id="book-tags-edit"
                  className="field-input"
                  type="text"
                  placeholder="Comma separated"
                  value={editTags}
                  disabled={!selected}
                  onChange={(event) => setEditTags(event.target.value)}
                />
              </div>
            </div>
            <div className="form-row">
              <span
                className="form-row-label"
                data-tip="Whether the original source file is still on disk. Repair needs it."
                data-tip-pos="right"
              >
                Source
              </span>
              <div className="form-row-field">
                <span id="book-source-status" className="book-summary-value">
                  {selected ? (selected.source_available ? "Available" : "Missing") : "—"}
                </span>
              </div>
            </div>
          </div>

          <div className="action-row library-detail-actions">
            <button
              id="library-save-metadata-btn"
              className="btn btn-compact"
              type="button"
              disabled={!selected}
              data-tip="Write title, author, and tags into the audio files"
              onClick={() =>
                void library.updateSelectedBookMetadata({
                  title: editTitle,
                  author: editAuthor,
                  tags: editTags,
                })
              }
            >
              Save metadata
            </button>
            <button
              id="library-open-folder-btn"
              className="btn btn-compact"
              type="button"
              disabled={!library.selectedBookId}
              data-tip="Reveal the exported audio in the file manager"
              onClick={() => void library.openSelectedBookFolder()}
            >
              Open folder
            </button>
            <button
              id="library-repair-btn"
              className="btn btn-compact"
              type="button"
              disabled={!library.selectedBookId}
              data-tip="Re-synthesize chapters whose audio files are missing. Needs the original source."
              onClick={() => void library.repairSelectedBook()}
            >
              Repair
            </button>
            <button
              id="library-delete-btn"
              className="btn btn-compact btn-danger"
              type="button"
              disabled={!library.selectedBookId}
              data-tip="Remove this book and its audio from the library"
              onClick={() => void library.deleteSelectedBook()}
            >
              Delete
            </button>
          </div>

          <div className="panel-section chapter-section">
            <div className="section-title"><span>Chapters</span></div>
            <div id="chapter-list" className="chapter-list">
              {chapters.map((chapter) => {
                const isCurrent = chapter.number === currentChapterNumber;
                const playbackLabel = chapterPlaybackStatusLabel({ isCurrent, playing });
                return (
                  <div key={chapter.id} className={`chapter-row${isCurrent ? " is-current" : ""}`}>
                    <button
                      type="button"
                      className="chapter-main"
                      data-tip="Play from this chapter"
                      onClick={() => onJumpToChapter?.(chapter)}
                      disabled={!onJumpToChapter}
                    >
                      <span className="chapter-number">{String(chapter.number).padStart(2, "0")}</span>
                      <span className="chapter-title">
                        {chapter.title || `Chapter ${chapter.number}`}
                        {playbackLabel ? ` — ${playbackLabel.toLowerCase()}` : ""}
                      </span>
                      <span className="chapter-duration">{formatDurationClock(chapter.duration_ms)}</span>
                    </button>
                    <button
                      className="btn btn-compact"
                      type="button"
                      disabled={cleaningPreviewLoading}
                      data-tip="Compare this chapter's text before and after cleaning"
                      onClick={() => void loadCleaningPreview(chapter.number)}
                    >
                      Cleaned text
                    </button>
                  </div>
                );
              })}
            </div>
          </div>

          <details className="book-detail-toggle cleaning-preview-toggle" open={Boolean(cleaningPreview)}>
            <summary>Cleaning preview</summary>
            <pre id="cleaning-preview">{cleaningPreviewLoading ? "Loading cleaning preview…" : previewText(cleaningPreview)}</pre>
          </details>

          <details className="book-detail-toggle maintenance-toggle">
            <summary>Maintenance</summary>
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
              <button
                className="btn btn-compact"
                type="button"
                disabled={library.maintenanceLoading}
                data-tip="Remove orphaned temp files. Finished audio is never touched."
                onClick={() => void library.cleanupStorage()}
              >
                Clean up storage
              </button>
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
            <span className="field-hint">{library.maintenanceMessage}</span>
            {library.maintenanceReport ? (
              <div className="maintenance-grid">
                <div><span>Database</span><strong>{library.maintenanceReport.database_path}</strong></div>
                <div><span>Output</span><strong>{library.maintenanceReport.output_path}</strong></div>
                <div><span>Temp uploads</span><strong>{library.maintenanceReport.audit.stale_temp_uploads?.length ?? 0}</strong></div>
                <div><span>Orphan folders</span><strong>{library.maintenanceReport.audit.orphan_output_dirs?.length ?? 0}</strong></div>
                <div><span>Stale sources</span><strong>{library.maintenanceReport.audit.stale_book_sources?.length ?? 0}</strong></div>
                <div><span>Missing chapters</span><strong>{library.maintenanceReport.audit.stale_chapter_rows?.length ?? 0}</strong></div>
              </div>
            ) : null}
          </details>

          {library.errorMessage ? <div className="log-line log-error">{library.errorMessage}</div> : null}
          {!library.errorMessage && (library.statusMessage || library.loadingDetail) ? (
            <div className="log-line">{library.loadingDetail ? "Loading selected book…" : library.statusMessage}</div>
          ) : null}

          <details className="book-detail-toggle">
            <summary>Raw metadata</summary>
            <pre id="book-detail">{library.bookDetailText}</pre>
          </details>
        </div>
      </div>
    </section>
  );
}
