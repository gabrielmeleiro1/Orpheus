import { invoke } from "@tauri-apps/api/core";
import { useEffect, useMemo, useState } from "react";

import type { CleaningPreviewResponse, LibraryChapterDetail } from "../../types/api";
import { TermSelect } from "../conversion/TermSelect";
import { isTauriRuntime } from "../conversion/conversionPanelHelpers";
import type { UseLibraryResult } from "./useLibrary";
import { formatLibraryMeta } from "./useLibrary";
import "./library.css";

interface LibraryPanelProps {
  hidden: boolean;
  library: UseLibraryResult;
  currentChapterNumber?: number | null;
  onJumpToChapter?: (chapter: LibraryChapterDetail) => void;
}

function formatDurationMs(durationMs: number | null): string {
  if (!durationMs || durationMs <= 0) {
    return "--:--";
  }
  const seconds = Math.floor(durationMs / 1000);
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
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
  { value: "all", label: "all types" },
  { value: "audiobook", label: "audiobooks" },
  { value: "document_audio", label: "document audio" },
];

export function LibraryPanel({ hidden, library, currentChapterNumber, onJumpToChapter }: LibraryPanelProps) {
  const showEmpty = !library.loadingList && !library.errorMessage && library.items.length === 0;
  const selected = library.selectedBookDetail;
  const chapters = useMemo(() => selected?.chapters ?? [], [selected?.chapters]);
  const [editTitle, setEditTitle] = useState("");
  const [editAuthor, setEditAuthor] = useState("");
  const [editTags, setEditTags] = useState("");
  const [cleaningPreview, setCleaningPreview] = useState<CleaningPreviewResponse | null>(null);
  const [cleaningPreviewLoading, setCleaningPreviewLoading] = useState(false);

  useEffect(() => {
    setEditTitle(selected?.title ?? "");
    setEditAuthor(selected?.author ?? "");
    setEditTags(selected?.tags ?? "");
    setCleaningPreview(null);
  }, [selected?.id, selected?.title, selected?.author, selected?.tags]);

  const openAppDataFolder = async () => {
    if (isTauriRuntime()) {
      await invoke("open_app_data_folder");
    }
  };

  const loadCleaningPreview = async (chapterNumber: number) => {
    setCleaningPreviewLoading(true);
    try {
      const preview = await library.loadCleaningPreview(chapterNumber);
      setCleaningPreview(preview);
    } finally {
      setCleaningPreviewLoading(false);
    }
  };

  return (
    <section
      id="tab-library"
      className="panel"
      role="tabpanel"
      aria-label="Library and playback"
      hidden={hidden}
    >
      <div className="panel-header">
        <span>library</span>
        <span className="panel-header-meta">{library.items.length} {library.items.length === 1 ? "book" : "books"}</span>
      </div>

      <div className="library-workspace">
        <aside className="panel-section library-sidebar" aria-label="Library navigation">
          <div className="library-toolbar library-nav-actions">
            <button
              className="btn btn-compact"
              type="button"
              disabled={library.loadingList}
              onClick={() => void library.refreshLibrary()}
            >
              refresh
            </button>
            <button
              className="btn btn-compact"
              type="button"
              disabled={library.selectedBookIndex <= 0}
              onClick={() => void library.selectRelative(-1)}
            >
              prev
            </button>
            <button
              className="btn btn-compact"
              type="button"
              disabled={library.selectedBookIndex < 0 || library.selectedBookIndex >= library.items.length - 1}
              onClick={() => void library.selectRelative(1)}
            >
              next
            </button>
          </div>

          <div className="library-search-row">
            <label className="field-label" htmlFor="library-search">
              search
            </label>
            <input
              id="library-search"
              className="field-input library-search-field"
              type="text"
              placeholder="filter by title or author..."
              value={library.searchQuery}
              onChange={(event) => library.setSearchQuery(event.target.value)}
            />
          </div>

          <div className="library-filter-row">
            <TermSelect
              id="library-mode-filter"
              label="library type filter"
              value={library.modeFilter}
              options={LIBRARY_MODE_OPTIONS}
              placeholder="all types"
              onChange={(value) => library.setModeFilter(value as typeof library.modeFilter)}
            />
          </div>

          <div id="library-list" className="library-list">
            {library.loadingList ? <div className="log-line">Loading library...</div> : null}
            {showEmpty ? <div className="log-line">No converted books yet.</div> : null}
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
                  <span className="book-title">{item.title || "untitled"}</span>
                  <span className="book-meta">{formatLibraryMeta(item)}</span>
                </button>
              );
            })}
          </div>
        </aside>

        <div className="library-detail-pane">
          <div className="library-detail-header">
            <div>
              <span className="progress-kicker">selected output</span>
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
              <span className="form-row-label">title</span>
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
              <span className="form-row-label">author</span>
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
              <span className="form-row-label">tags</span>
              <div className="form-row-field">
                <input
                  id="book-tags-edit"
                  className="field-input"
                  type="text"
                  placeholder="comma separated"
                  value={editTags}
                  disabled={!selected}
                  onChange={(event) => setEditTags(event.target.value)}
                />
              </div>
            </div>
            <div className="form-row">
              <span className="form-row-label">source</span>
              <div className="form-row-field">
                <span id="book-source-status" className="book-summary-value">
                  {selected ? (selected.source_available ? "available" : "missing") : "—"}
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
              onClick={() =>
                void library.updateSelectedBookMetadata({
                  title: editTitle,
                  author: editAuthor,
                  tags: editTags,
                })
              }
            >
              save metadata
            </button>
            <button
              id="library-open-folder-btn"
              className="btn btn-compact"
              type="button"
              disabled={!library.selectedBookId}
              onClick={() => void library.openSelectedBookFolder()}
            >
              open folder
            </button>
            <button
              id="library-repair-btn"
              className="btn btn-compact"
              type="button"
              disabled={!library.selectedBookId}
              onClick={() => void library.repairSelectedBook()}
            >
              repair missing chapters
            </button>
            <button
              id="library-delete-btn"
              className="btn btn-compact"
              type="button"
              disabled={!library.selectedBookId}
              onClick={() => void library.deleteSelectedBook()}
            >
              delete
            </button>
          </div>

          <div className="panel-section chapter-section">
            <div className="section-title"><span>chapters</span></div>
            <div id="chapter-list" className="chapter-list">
              {chapters.map((chapter) => {
                const isCurrent = chapter.number === currentChapterNumber;
                return (
                  <div key={chapter.id} className={`chapter-row${isCurrent ? " is-current" : ""}`}>
                    <button
                      type="button"
                      className="chapter-main"
                      onClick={() => onJumpToChapter?.(chapter)}
                      disabled={!onJumpToChapter}
                    >
                      <span className="chapter-number">{String(chapter.number).padStart(2, "0")}</span>
                      <span className="chapter-title">
                        {chapter.title || `Chapter ${chapter.number}`}{isCurrent ? " / playing" : ""}
                      </span>
                      <span className="chapter-duration">{formatDurationMs(chapter.duration_ms)}</span>
                    </button>
                    <button
                      className="btn btn-compact"
                      type="button"
                      disabled={cleaningPreviewLoading}
                      onClick={() => void loadCleaningPreview(chapter.number)}
                    >
                      review cleaned text
                    </button>
                  </div>
                );
              })}
            </div>
          </div>

          <details className="book-detail-toggle cleaning-preview-toggle" open={Boolean(cleaningPreview)}>
            <summary>cleaning preview</summary>
            <pre id="cleaning-preview">{cleaningPreviewLoading ? "Loading cleaning preview..." : previewText(cleaningPreview)}</pre>
          </details>

          <details className="book-detail-toggle maintenance-toggle">
            <summary>maintenance / diagnostics</summary>
            <div className="maintenance-actions">
              <button className="btn btn-compact" type="button" disabled={library.maintenanceLoading} onClick={() => void library.loadMaintenanceDiagnostics()}>
                load diagnostics
              </button>
              <button className="btn btn-compact" type="button" disabled={library.maintenanceLoading} onClick={() => void library.backupDatabase()}>
                backup database
              </button>
              <button className="btn btn-compact" type="button" disabled={library.maintenanceLoading} onClick={() => void library.cleanupStorage()}>
                safe cleanup
              </button>
              <button className="btn btn-compact" type="button" disabled={!isTauriRuntime()} onClick={() => void openAppDataFolder()}>
                open app data
              </button>
            </div>
            <span className="field-hint">{library.maintenanceMessage}</span>
            {library.maintenanceReport ? (
              <div className="maintenance-grid">
                <div><span>database</span><strong>{library.maintenanceReport.database_path}</strong></div>
                <div><span>output</span><strong>{library.maintenanceReport.output_path}</strong></div>
                <div><span>temp uploads</span><strong>{library.maintenanceReport.audit.stale_temp_uploads?.length ?? 0}</strong></div>
                <div><span>orphan folders</span><strong>{library.maintenanceReport.audit.orphan_output_dirs?.length ?? 0}</strong></div>
                <div><span>stale sources</span><strong>{library.maintenanceReport.audit.stale_book_sources?.length ?? 0}</strong></div>
                <div><span>missing chapters</span><strong>{library.maintenanceReport.audit.stale_chapter_rows?.length ?? 0}</strong></div>
              </div>
            ) : null}
          </details>

          {library.errorMessage ? <div className="log-line log-error">{library.errorMessage}</div> : null}
          {!library.errorMessage && (library.statusMessage || library.loadingDetail) ? (
            <div className="log-line">{library.loadingDetail ? "Loading selected book..." : library.statusMessage}</div>
          ) : null}

          <details className="book-detail-toggle">
            <summary>raw metadata</summary>
            <pre id="book-detail">{library.bookDetailText}</pre>
          </details>
        </div>
      </div>
    </section>
  );
}
