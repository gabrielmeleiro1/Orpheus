import { useEffect, useRef } from "react";

import type {
  ConversionCapabilities,
  ConversionPanelActions,
  SourceQueueViewModel,
} from "../conversionPanelStateTypes";
import { scrollConversionReviewAnchor, nextConversionReviewScrollState } from "../scrollConversionReviewAnchor";
import { BrailleSpinner } from "../../../ui/ascii/BrailleSpinner";
import { Icon } from "../../../ui/icons";

interface SourceQueueViewProps {
  sourceQueue: SourceQueueViewModel;
  capabilities: ConversionCapabilities;
  actions: ConversionPanelActions;
  hidden?: boolean;
}

export function SourceQueueView({
  sourceQueue,
  capabilities,
  actions,
  hidden = false,
}: SourceQueueViewProps) {
  const reviewAnchorRef = useRef<HTMLDivElement | null>(null);
  const previousReadyKeyRef = useRef("");
  const selectedItem = sourceQueue.queueItems.find((item) => item.selected);

  useEffect(() => {
    const readyKey = sourceQueue.preflightCard
      ? `${sourceQueue.sourcePath}|${sourceQueue.preflightCard.title}|${sourceQueue.queueCount}`
      : "";
    const next = nextConversionReviewScrollState({
      readyKey,
      acknowledgedReadyKey: previousReadyKeyRef.current,
      hidden,
    });
    previousReadyKeyRef.current = next.acknowledgedReadyKey;
    if (next.shouldScroll) {
      scrollConversionReviewAnchor(reviewAnchorRef.current);
    }
  }, [hidden, sourceQueue.preflightCard, sourceQueue.queueCount, sourceQueue.sourcePath]);

  const metadataFields = (
    <div className="form-grid conversion-form-grid">
      <div className="field-row">
        <div className="field">
          <label
            className="field-label"
            htmlFor="title-input"
            data-tip="Overrides the title detected from the source"
            data-tip-pos="right"
          >
            Title
          </label>
          <input
            id="title-input"
            aria-label="Title override"
            className="field-input"
            type="text"
            placeholder="Optional"
            value={sourceQueue.titleOverride}
            onChange={(event) => {
              actions.setTitleOverride(event.target.value);
              actions.syncSelectedQueueEdits({ title: event.target.value });
            }}
          />
        </div>
        <div className="field">
          <label
            className="field-label"
            htmlFor="author-input"
            data-tip="Overrides the author detected from the source"
            data-tip-pos="right"
          >
            Author
          </label>
          <input
            id="author-input"
            aria-label="Author override"
            className="field-input"
            type="text"
            placeholder="Optional"
            value={sourceQueue.authorOverride}
            onChange={(event) => {
              actions.setAuthorOverride(event.target.value);
              actions.syncSelectedQueueEdits({ author: event.target.value });
            }}
          />
        </div>
      </div>
    </div>
  );

  return (
    <div className="conversion-source-sections">
      <span className="sr-only" role="status" aria-atomic="true">{[sourceQueue.preflightStatus, sourceQueue.pasteStatus === "Idle" ? "" : sourceQueue.pasteStatus].filter(Boolean).join(". ")}</span>
      <section className="conversion-section conversion-source-section">
        <div className="conversion-section-header">
          <h2 className="conversion-group-heading">Source</h2>
          <p className="conversion-section-hint">
            {sourceQueue.isDocumentMode
              ? "Choose a file, or paste text below."
              : "Add a book file to convert into an audiobook."}
          </p>
        </div>
        <input
          ref={sourceQueue.browserFileInputRef}
          type="file"
          multiple
          accept={sourceQueue.browserFileAccept}
          hidden
          tabIndex={-1}
          onChange={actions.onBrowserFilesSelected}
        />
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
              <Icon name="upload" size={22} />
            </span>
            <span className="drop-zone-copy">
              <strong>Drop a file or browse</strong>
              <span className="drop-zone-formats">.pdf, .epub, .txt, or .md</span>
            </span>
            <span className="btn drop-zone-browse" aria-hidden="true">Browse</span>
          </div>

          <details
            className="manual-path"
            onClick={(event) => event.stopPropagation()}
          >
            <summary className="btn btn-ghost btn-sm manual-path-toggle">Enter path manually</summary>
            <input
              id="source-path"
              aria-label="Source path"
              className="field-input"
              type="text"
              placeholder={sourceQueue.sourcePathPlaceholder}
              value={sourceQueue.sourcePath}
              onChange={(event) => {
                const nextValue = event.target.value;
                actions.setSourcePath(nextValue);
                actions.syncSelectedQueueEdits({ sourcePath: nextValue });
                actions.clearFormError();
              }}
            />
          </details>
        </div>

        {sourceQueue.sourcePath ? (
          <div className="source-chosen">
            <span id="source-file-label" className="source-chosen-file" data-tip={sourceQueue.sourcePath}>
              <span className="source-chosen-name">{sourceQueue.sourceFileName}</span>
              {sourceQueue.sourceFileDir ? (
                <span className="source-chosen-dir">{sourceQueue.sourceFileDir}</span>
              ) : null}
            </span>
            <button
              id="preflight-source-btn"
              className="btn btn-ghost"
              type="button"
              disabled={!capabilities.canInspectSource}
              data-tip="Analyze the source — chapters, length, warnings — without converting"
              onClick={() => actions.onInspectCurrentSource()}
            >
              Inspect
            </button>
          </div>
        ) : null}

        {sourceQueue.isDocumentMode ? (
          <div className="source-or-divider" aria-hidden="true">
            <span>or</span>
          </div>
        ) : null}

        {sourceQueue.isDocumentMode ? (
          <div className="panel-section paste-panel" aria-label="Paste markdown source">
            <div className="paste-header">
              <div className="paste-header-copy">
                <span className="paste-title">Paste text</span>
                <span className="field-hint">Skip the file picker and narrate pasted Markdown or plain text.</span>
              </div>
              <button
                id="add-pasted-text-btn"
                className="btn"
                type="button"
                disabled={!capabilities.canAddPastedText}
                data-tip="Queue the pasted text as a document source"
                onClick={() => void actions.onAddPastedText()}
              >
                {sourceQueue.pasteBusy ? "Adding…" : "Add to queue"}
              </button>
            </div>
            <div className="paste-grid">
              <textarea
                id="paste-text-input"
                aria-label="Pasted document text"
                className="field-input paste-textarea"
                placeholder="Paste Markdown or plain text…"
                value={sourceQueue.pastedText}
                onChange={(event) => actions.setPastedText(event.target.value)}
              />
              <div className="field-row">
                <div className="field">
                  <label className="field-label" htmlFor="paste-title-input">Title for this paste</label>
                  <input
                    id="paste-title-input"
                    aria-label="Paste title"
                    className="field-input"
                    type="text"
                    placeholder="Optional"
                    value={sourceQueue.pastedTitle}
                    onChange={(event) => actions.setPastedTitle(event.target.value)}
                  />
                </div>
                <div className="field">
                  <label className="field-label" htmlFor="paste-author-input">Author for this paste</label>
                  <input
                    id="paste-author-input"
                    aria-label="Paste author"
                    className="field-input"
                    type="text"
                    placeholder="Optional"
                    value={sourceQueue.pastedAuthor}
                    onChange={(event) => actions.setPastedAuthor(event.target.value)}
                  />
                </div>
              </div>
            </div>
            {sourceQueue.pasteStatus && sourceQueue.pasteStatus !== "Idle" ? (
              <p id="paste-source-status" className="field-hint">{sourceQueue.pasteStatus}</p>
            ) : (
              <p className="hint">These fields apply only when you add pasted text — not to a chosen file.</p>
            )}
          </div>
        ) : null}
      </section>

      {sourceQueue.queueItems.length > 0 ? (
        <section className="conversion-section queue-panel" aria-label="Conversion queue">
          <div className="queue-header">
            <span>{`Queue · ${sourceQueue.queueCount}`}</span>
            <button
              className="btn btn-ghost btn-sm"
              type="button"
              disabled={!sourceQueue.queueItems.some((item) => item.finished)}
              onClick={() => sourceQueue.queueItems.filter((item) => item.finished).forEach((item) => actions.removeQueueItem(item.id))}
            >
              Clear finished
            </button>
          </div>
          <div className="queue-list">
            {sourceQueue.queueItems.map((item) => (
              <div key={item.id} className={`queue-row${item.selected ? " is-selected" : ""}`}>
                <button className="queue-item-main" type="button" aria-pressed={item.selected} onClick={() => actions.selectQueueItem(item.id)}>
                  <span className="queue-item-title">{item.title}</span>
                  <span className={item.statusClassName}>
                    {item.converting ? <BrailleSpinner active className="queue-spinner" /> : null}
                    {item.statusText}
                  </span>
                </button>
                <div className="queue-item-actions">
                  <button className="btn btn-sm" type="button" disabled={!item.canMoveUp} onClick={() => actions.moveQueueItem(item.id, -1)}>
                    Up
                  </button>
                  <button className="btn btn-sm" type="button" disabled={!item.canMoveDown} onClick={() => actions.moveQueueItem(item.id, 1)}>
                    Down
                  </button>
                  <button className="btn btn-sm" type="button" disabled={!item.canRemove} onClick={() => actions.removeQueueItem(item.id)}>
                    Remove
                  </button>
                </div>
              </div>
            ))}
          </div>
        </section>
      ) : null}

      <div id="form-error" className="form-error" role="alert" aria-live="assertive">
        {sourceQueue.formError}
      </div>

      <section ref={reviewAnchorRef} id="conversion-review-anchor" className="conversion-section details-section">
        <div className="conversion-section-header">
          <h2 className="conversion-group-heading">
            Details{selectedItem?.title ? ` — ${selectedItem.title}` : ""}
          </h2>
        </div>
        {sourceQueue.preflightCard ? (
          <div className="panel-section preflight-card">
            <div className="preflight-title">
              <span>{sourceQueue.preflightCard.title}</span>
              <span>{sourceQueue.preflightCard.meta}</span>
            </div>
            {[sourceQueue.preflightCard.author, sourceQueue.preflightCard.sourceType].filter(Boolean).length > 0 ? (
              <div className="preflight-meta">
                {[sourceQueue.preflightCard.author, sourceQueue.preflightCard.sourceType].filter(Boolean).join(" · ")}
              </div>
            ) : null}
            {sourceQueue.preflightCard.warnings.length > 0 ? (
              <div className="preflight-warnings">
                {sourceQueue.preflightCard.warnings.map((warning) => (
                  <div key={warning}>Warning: {warning}</div>
                ))}
              </div>
            ) : null}
            <p className="preflight-sample">{sourceQueue.preflightCard.sample}</p>
            <p className="field-hint">
              {!sourceQueue.preflightCard.canConvert
                ? `Source cannot be converted — ${sourceQueue.preflightCard.blockingReason}`
                : capabilities.showStartConversion
                  ? "Source ready — use Start conversion below."
                  : "Source ready — use Run queue below."}
            </p>
          </div>
        ) : sourceQueue.preflightStatus ? (
          <div className="field-hint preflight-status">{sourceQueue.preflightStatus}</div>
        ) : null}

        {sourceQueue.isDocumentMode ? (
          <details className="metadata-overrides disclosure disclosure-card">
            <summary>
              <span>Edit title &amp; author</span>
            </summary>
            <div className="metadata-overrides-body">
              {metadataFields}
            </div>
          </details>
        ) : metadataFields}
      </section>
    </div>
  );
}
