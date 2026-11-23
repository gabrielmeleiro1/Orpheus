import { useState } from "react";

import type {
  ConversionCapabilities,
  ConversionPanelActions,
  QueueItemViewModel,
  SourceQueueViewModel,
} from "../conversionPanelStateTypes";
import { AsciiLoader } from "../../../ui/ascii/AsciiLoader";
import { BookCover } from "../../../ui/BookCover";
import { Icon } from "../../../ui/icons";
import { ProgressBar } from "../../../ui/ProgressBar";
import { unwrapQuotedSourcePath } from "../conversionPanelHelpers";

interface SourceQueueViewProps {
  sourceQueue: SourceQueueViewModel;
  capabilities: ConversionCapabilities;
  actions: ConversionPanelActions;
  hidden?: boolean;
}

const CHAPTER_PREVIEW_LIMIT = 12;

function chapterCountLabel(count: number): string {
  return `${count} ${count === 1 ? "chapter" : "chapters"}`;
}

/** The converting book's own live bar, so progress is watched where the book was queued. */
function QueueRowProgress({ title, pct, label, onOpenDetails }: {
  title: string;
  pct: number;
  label: string;
  onOpenDetails?: () => void;
}) {
  return (
    <div className="queue-row-progress">
      <ProgressBar pct={pct} tone="active" size="regular" variant="ascii" cells="fit" label={`${title} conversion progress`} />
      <span className="queue-row-progress-text tabular-nums">
        <strong>{pct}%</strong>
        {label ? <span> · {label}</span> : null}
      </span>
      {onOpenDetails ? (
        <button className="btn btn-ghost btn-sm btn-flush-end queue-details-btn" type="button" data-tip="Stages and event log" data-tip-pos="left" onClick={onOpenDetails}>
          Details
        </button>
      ) : null}
    </div>
  );
}

/**
 * Status pill. "Ready" is the normal state, so it is only announced, not drawn.
 * One live element per row, so every transition is announced.
 */
function QueueStatus({ item, hidden }: { item: QueueItemViewModel; hidden: boolean }) {
  const statusClassName = item.statusClassName || "queue-item-status";
  // A converting row shows its own progress line, so the badge would repeat it.
  const quiet = statusClassName.includes("status-ready") || item.progressPct !== undefined;
  const tone = statusClassName.includes("status-failed") ? "is-danger"
    : statusClassName.includes("status-warning") ? "is-warning"
      : statusClassName.includes("status-done") ? "is-success"
        : "is-neutral";
  return (
    <span className={quiet ? `${statusClassName} sr-only` : `${statusClassName} status-badge ${tone}`} role="status">
      {item.converting && !quiet ? <AsciiLoader active={!hidden} width={5} className="queue-spinner" /> : null}
      <span className="queue-item-status-label">{item.statusText}</span>
    </span>
  );
}

/** Title, author, chapters and a sample for the selected row, shown inside that row. */
function QueueItemDetail({ sourceQueue, capabilities, actions, inspecting, failed }: SourceQueueViewProps & {
  inspecting: boolean;
  failed: boolean;
}) {
  const pendingText = inspecting ? "Reading chapters…" : failed ? "" : "Not inspected yet.";
  const card = sourceQueue.preflightCard;
  const titles = card?.chapterTitles ?? [];
  const chapterTotal = Math.max(titles.length, card?.chapterCount ?? 0);
  return (
    <div id="conversion-review-anchor" className="queue-item-detail" role="group" aria-label="Selected book">
      <div className="field-row">
        <div className="field">
          <label className="field-label" htmlFor="title-input">Title</label>
          <input
            id="title-input"
            aria-label="Title override"
            className="field-input"
            type="text"
            placeholder={card?.title || "Title"}
            value={sourceQueue.titleOverride}
            onChange={(event) => {
              actions.setTitleOverride(event.target.value);
              actions.syncSelectedQueueEdits({ title: event.target.value });
            }}
          />
        </div>
        <div className="field">
          <label className="field-label" htmlFor="author-input">Author</label>
          <input
            id="author-input"
            aria-label="Author override"
            className="field-input"
            type="text"
            placeholder={card?.author || "Optional"}
            value={sourceQueue.authorOverride}
            onChange={(event) => {
              actions.setAuthorOverride(event.target.value);
              actions.syncSelectedQueueEdits({ author: event.target.value });
            }}
          />
        </div>
      </div>
      {card ? (
        <>
          {card.warnings.length > 0 ? (
            <ul className="preflight-warnings">
              {card.warnings.map((warning) => <li key={warning}>{warning}</li>)}
            </ul>
          ) : null}
          {!card.canConvert ? <p className="field-error">Can’t convert this source: {card.blockingReason}</p> : null}
          <details className="book-preview disclosure">
            <summary><span>Chapters and sample</span></summary>
            <div className="book-preview-columns">
              <section className="book-chapters" aria-label="Chapters">
                <h3 className="eyebrow">Chapters</h3>
                {titles.length > 0 ? (
                  <>
                    <ol>{titles.slice(0, CHAPTER_PREVIEW_LIMIT).map((title, index) => <li key={index}><span>{title}</span></li>)}</ol>
                    {chapterTotal > CHAPTER_PREVIEW_LIMIT ? (
                      <p className="field-hint">{chapterTotal - CHAPTER_PREVIEW_LIMIT} more</p>
                    ) : null}
                  </>
                ) : <p className="field-hint">{card.chapterCount !== undefined ? chapterCountLabel(card.chapterCount) : card.meta}</p>}
              </section>
              <section aria-label="Sample">
                <h3 className="eyebrow">Sample</h3>
                <p className="preflight-sample">{card.sample}</p>
              </section>
            </div>
          </details>
        </>
      ) : (
        <div className="queue-item-detail-pending">
          {pendingText ? <span className="field-hint">{pendingText}</span> : null}
          {inspecting ? null : (
            <button
              id="preflight-source-btn"
              className="btn btn-ghost btn-sm"
              type="button"
              disabled={!capabilities.canInspectSource}
              data-tip="Read chapters and a sample without converting"
              data-tip-pos="left"
              onClick={() => actions.onInspectCurrentSource()}
            >
              Inspect
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function FileIntake({ sourceQueue, actions }: Pick<SourceQueueViewProps, "sourceQueue" | "actions">) {
  const [pathDraft, setPathDraft] = useState("");
  const addPath = () => {
    const path = pathDraft.trim();
    if (!path) return;
    actions.onAddSourcePath(path);
    setPathDraft("");
  };
  return (
    <div className="drop-zone-stack">
      <div
        id="drop-zone"
        className={`drop-zone${sourceQueue.dragging ? " drag-over" : ""}`}
        role="button"
        tabIndex={0}
        aria-label="Drop file here"
        onClick={() => void actions.onBrowse()}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            void actions.onBrowse();
          }
        }}
        onDragOver={(event) => {
          event.preventDefault();
          actions.setDragging(true);
        }}
        onDragLeave={(event) => {
          event.preventDefault();
          actions.setDragging(false);
        }}
        onDrop={actions.onDropZoneDrop}
      >
        <span className="drop-zone-icon" aria-hidden="true">
          <Icon name="upload" />
        </span>
        <span className="drop-zone-copy">
          <strong>Drop books here</strong>
          <span className="drop-zone-formats">PDF, EPUB, TXT or Markdown</span>
        </span>
        <span className="btn btn-secondary drop-zone-browse" aria-hidden="true">Browse</span>
      </div>

      {/* Typing a path is a power-user fallback for the picker, so it stays folded away. */}
      <details className="manual-path disclosure">
        <summary className="manual-path-toggle"><span>Enter a file path</span></summary>
        <form
          className="manual-path-row"
          onSubmit={(event) => {
            event.preventDefault();
            addPath();
          }}
        >
          <input
            id="source-path"
            aria-label="Source path"
            className="field-input"
            type="text"
            spellCheck={false}
            autoComplete="off"
            placeholder={sourceQueue.sourcePathPlaceholder}
            value={pathDraft}
            onChange={(event) => setPathDraft(unwrapQuotedSourcePath(event.target.value))}
          />
          <button id="add-source-path-btn" className="btn btn-secondary" type="submit" disabled={!pathDraft.trim()}>
            Add
          </button>
        </form>
      </details>
    </div>
  );
}

function PasteIntake({ sourceQueue, capabilities, actions }: SourceQueueViewProps) {
  const pasteMessage = sourceQueue.pasteStatus === "Idle" ? "" : sourceQueue.pasteStatus;
  return (
    <div className="paste-panel" aria-label="Paste text source" role="group">
      <label className="sr-only" htmlFor="paste-text-input">Text</label>
      <textarea
        id="paste-text-input"
        aria-label="Pasted document text"
        className="field-textarea"
        placeholder="Paste Markdown or plain text…"
        value={sourceQueue.pastedText}
        onChange={(event) => actions.setPastedText(event.target.value)}
      />
      <div className="field-row">
        <div className="field">
          <label className="sr-only" htmlFor="paste-title-input">Title</label>
          <input
            id="paste-title-input"
            aria-label="Paste title"
            className="field-input"
            type="text"
            placeholder="Title"
            value={sourceQueue.pastedTitle}
            onChange={(event) => actions.setPastedTitle(event.target.value)}
          />
        </div>
        <div className="field">
          <label className="sr-only" htmlFor="paste-author-input">Author</label>
          <input
            id="paste-author-input"
            aria-label="Paste author"
            className="field-input"
            type="text"
            placeholder="Author (optional)"
            value={sourceQueue.pastedAuthor}
            onChange={(event) => actions.setPastedAuthor(event.target.value)}
          />
        </div>
      </div>
      <div className="paste-footer">
        <p className="field-hint">
          {sourceQueue.isDocumentMode ? "Headings become sections." : "Headings become chapters."}
        </p>
        <button
          id="add-pasted-text-btn"
          className="btn btn-secondary"
          type="button"
          disabled={!capabilities.canAddPastedText}
          onClick={() => void actions.onAddPastedText()}
        >
          {sourceQueue.pasteBusy ? "Adding…" : "Add to queue"}
        </button>
      </div>
      <p id="paste-source-status" className="field-hint" role="status" aria-atomic="true">
        {pasteMessage}
      </p>
    </div>
  );
}

export function SourceQueueView({
  sourceQueue,
  capabilities,
  actions,
  hidden = false,
}: SourceQueueViewProps) {
  const hasFinished = sourceQueue.queueItems.some((item) => item.finished);
  return (
    <div className="conversion-source-sections">
      <section className="card conversion-section conversion-source-section" aria-label="Sources">
        <input
          ref={sourceQueue.browserFileInputRef}
          type="file"
          multiple
          accept={sourceQueue.browserFileAccept}
          hidden
          tabIndex={-1}
          onChange={actions.onBrowserFilesSelected}
        />
        <div className="seg-control source-input-switch" role="group" aria-label="Source type">
          <button
            id="source-input-file"
            type="button"
            className={`seg-control-btn${sourceQueue.sourceInput === "file" ? " is-active" : ""}`}
            aria-pressed={sourceQueue.sourceInput === "file"}
            onClick={() => actions.setSourceInput("file")}
          >
            <Icon name="upload" />
            Add a file
          </button>
          <button
            id="source-input-paste"
            type="button"
            className={`seg-control-btn${sourceQueue.sourceInput === "paste" ? " is-active" : ""}`}
            aria-pressed={sourceQueue.sourceInput === "paste"}
            onClick={() => actions.setSourceInput("paste")}
          >
            <Icon name="document" />
            Paste text
          </button>
        </div>
        {sourceQueue.sourceInput === "file"
          ? <FileIntake sourceQueue={sourceQueue} actions={actions} />
          : <PasteIntake sourceQueue={sourceQueue} capabilities={capabilities} actions={actions} />}
        {sourceQueue.preflightStatus ? (
          <p id="source-intake-status" className="field-hint source-intake-status" role="status" aria-atomic="true">
            {sourceQueue.preflightStatus}
          </p>
        ) : null}
      </section>

      <section className="card conversion-section queue-panel" aria-label="Conversion queue">
        <div className="card-header queue-header">
          <div className="queue-heading">
            <h2 className="eyebrow">Queue</h2>
            <span className="queue-count tabular-nums">{sourceQueue.queueCount}</span>
          </div>
          {hasFinished ? (
            <button
              className="btn btn-ghost btn-sm btn-flush-end"
              type="button"
              onClick={() => actions.removeQueueItems(sourceQueue.queueItems.filter((item) => item.finished).map((item) => item.id))}
            >
              Clear finished
            </button>
          ) : null}
        </div>
        <div className="queue-list">
          {sourceQueue.untrackedJob ? (
            <div className="queue-row is-converting">
              <div className="queue-item-main is-static">
                <BookCover title={sourceQueue.untrackedJob.title} size="sm" />
                <span className="queue-item-title">{sourceQueue.untrackedJob.title}</span>
              </div>
              <QueueRowProgress
                title={sourceQueue.untrackedJob.title}
                pct={sourceQueue.untrackedJob.progressPct}
                label={sourceQueue.untrackedJob.progressLabel}
                onOpenDetails={actions.onOpenConversionDetails}
              />
            </div>
          ) : null}
          {sourceQueue.queueItems.length === 0 && !sourceQueue.untrackedJob ? <p className="queue-empty">Books you add wait here</p> : null}
          {sourceQueue.queueItems.map((item) => {
            const converting = item.progressPct !== undefined;
            const expanded = item.selected && !converting && !item.converting && !item.finished;
            const metaText = item.error || item.meta;
            return (
              <div key={item.id} className={`queue-row${item.selected ? " is-selected" : ""}${converting ? " is-converting" : ""}${expanded ? " is-expanded" : ""}`}>
                <button className="queue-item-main" type="button" aria-pressed={item.selected} onClick={() => actions.selectQueueItem(item.id)}>
                  <BookCover title={item.title} size="sm" />
                  <span className="queue-item-title">{item.title}</span>
                  {metaText ? (
                    <span className={`queue-item-meta${item.error ? " is-error" : ""}`}>{metaText}</span>
                  ) : null}
                  <QueueStatus item={item} hidden={hidden} />
                </button>
                <div className="queue-item-actions">
                  <button className="btn btn-ghost btn-sm btn-icon" type="button" aria-label="Up" data-tip="Move up" disabled={!item.canMoveUp} onClick={() => actions.moveQueueItem(item.id, -1)}>
                    <Icon name="chevron-right" className="queue-move-up" />
                  </button>
                  <button className="btn btn-ghost btn-sm btn-icon" type="button" aria-label="Down" data-tip="Move down" disabled={!item.canMoveDown} onClick={() => actions.moveQueueItem(item.id, 1)}>
                    <Icon name="chevron-right" className="queue-move-down" />
                  </button>
                  <button className="btn btn-ghost btn-sm btn-icon" type="button" aria-label="Remove" data-tip="Remove" disabled={!item.canRemove} onClick={() => actions.removeQueueItem(item.id)}>
                    <Icon name="x" />
                  </button>
                </div>
                {converting ? (
                  <QueueRowProgress
                    title={item.title}
                    pct={item.progressPct ?? 0}
                    label={item.progressLabel ?? ""}
                    onOpenDetails={actions.onOpenConversionDetails}
                  />
                ) : null}
                {expanded ? (
                  <QueueItemDetail
                    sourceQueue={sourceQueue}
                    capabilities={capabilities}
                    actions={actions}
                    inspecting={Boolean(item.inspecting)}
                    failed={Boolean(item.error)}
                  />
                ) : null}
              </div>
            );
          })}
        </div>
      </section>
    </div>
  );
}
