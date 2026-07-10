import type {
  ConversionCapabilities,
  ConversionPanelActions,
  SourceQueueViewModel,
} from "../conversionPanelStateTypes";
import { BrailleSpinner } from "../../../ui/ascii/BrailleSpinner";

interface SourceQueueViewProps {
  sourceQueue: SourceQueueViewModel;
  capabilities: ConversionCapabilities;
  actions: ConversionPanelActions;
}

export function SourceQueueView({
  sourceQueue,
  capabilities,
  actions,
}: SourceQueueViewProps) {
  return (
    <>
      <input
        ref={sourceQueue.browserFileInputRef}
        type="file"
        multiple
        accept={sourceQueue.browserFileAccept}
        hidden
        tabIndex={-1}
        onChange={actions.onBrowserFilesSelected}
      />
      <div
        id="drop-zone"
        className={`drop-zone${sourceQueue.dragging ? " drag-over" : ""}`}
        role="button"
        tabIndex={0}
        aria-label="drop file here"
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
          <svg viewBox="0 0 24 24" focusable="false">
            <path d="M12 4v11" />
            <path d="M8 11l4 4 4-4" />
            <path d="M5 19h14" />
          </svg>
        </span>
        <span className="drop-zone-copy">
          <strong>{sourceQueue.isDocumentMode ? "Drop document here" : "Drop source here"}</strong>
          <span>{sourceQueue.isDocumentMode ? ".pdf, .epub, .txt, or .md" : ".pdf or .epub"}</span>
        </span>
        <span className="drop-zone-hint">Click to browse or drag and drop</span>
      </div>

      <span id="source-file-label" className="field-hint source-file-label">
        {sourceQueue.sourceFileLabel}
      </span>

      {sourceQueue.isDocumentMode ? (
        <div className="panel-section paste-panel" aria-label="Paste markdown source">
          <div className="paste-header">
            <span>paste .md</span>
            <button
              id="add-pasted-text-btn"
              className="btn btn-compact"
              type="button"
              disabled={!capabilities.canAddPastedText}
              onClick={() => void actions.onAddPastedText()}
            >
              {sourceQueue.pasteBusy ? "adding..." : "add pasted text"}
            </button>
          </div>
          <div className="paste-grid">
            <span className="form-row-label">title</span>
            <input
              id="paste-title-input"
              className="field-input"
              type="text"
              placeholder="Pasted Document"
              value={sourceQueue.pasteTitle}
              onChange={(event) => actions.setPasteTitle(event.target.value)}
            />
            <span className="form-row-label">text</span>
            <textarea
              id="paste-text-input"
              className="field-input paste-textarea"
              placeholder="paste text..."
              value={sourceQueue.pastedText}
              onChange={(event) => actions.setPastedText(event.target.value)}
            />
          </div>
          <span id="paste-source-status" className="field-hint">{sourceQueue.pasteStatus}</span>
        </div>
      ) : null}

      {sourceQueue.queueItems.length > 0 ? (
        <div className="panel-section queue-panel" aria-label="Conversion queue">
          <div className="queue-header">
            <span>queue: {sourceQueue.queueCount}</span>
            <button
              id="run-queue-btn"
              className="btn btn-compact"
              type="button"
              disabled={!capabilities.canRunQueue}
              onClick={() => void actions.onRunQueue()}
            >
              {sourceQueue.queueRunning ? "running..." : "run queue"}
            </button>
          </div>
          <div className="queue-list">
            {sourceQueue.queueItems.map((item) => (
              <div key={item.id} className={`queue-row${item.selected ? " is-selected" : ""}`}>
                <button className="queue-item-main" type="button" onClick={() => actions.selectQueueItem(item.id)}>
                  <span className="queue-item-title">{item.title}</span>
                  <span className={item.statusClassName}>
                    {item.converting ? <BrailleSpinner active className="queue-spinner" /> : null}
                    {item.statusText}
                  </span>
                </button>
                <div className="queue-item-actions">
                  <button className="btn btn-compact" type="button" disabled={!item.canMoveUp} onClick={() => actions.moveQueueItem(item.id, -1)}>
                    up
                  </button>
                  <button className="btn btn-compact" type="button" disabled={!item.canMoveDown} onClick={() => actions.moveQueueItem(item.id, 1)}>
                    down
                  </button>
                  <button className="btn btn-compact" type="button" disabled={!item.canRemove} onClick={() => actions.removeQueueItem(item.id)}>
                    remove
                  </button>
                </div>
              </div>
            ))}
          </div>
          <span className="field-hint">{sourceQueue.queueStatus}</span>
        </div>
      ) : null}

      <div id="form-error" className="form-error" role="alert" aria-live="assertive">
        {sourceQueue.formError}
      </div>

      <div className="panel-section form-section">
        <div className="form-row">
          <span className="form-row-label">source</span>
          <div className="form-row-field">
            <input
              id="source-path"
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
            <button
              id="preflight-source-btn"
              className="btn btn-compact"
              type="button"
              disabled={!capabilities.canInspectSource}
              onClick={() => actions.onInspectCurrentSource()}
            >
              inspect
            </button>
            <button id="browse-source-btn" className="btn btn-compact" type="button" onClick={() => void actions.onBrowse()}>
              browse
            </button>
          </div>
        </div>

        {sourceQueue.preflightCard ? (
          <div className="panel-section preflight-card">
            <div className="preflight-title">
              <span>{sourceQueue.preflightCard.title}</span>
              <span>{sourceQueue.preflightCard.meta}</span>
            </div>
            <div className="preflight-meta">
              author: {sourceQueue.preflightCard.author} / source: {sourceQueue.preflightCard.sourceType}
            </div>
            {sourceQueue.preflightCard.warnings.length > 0 ? (
              <div className="preflight-warnings">
                {sourceQueue.preflightCard.warnings.map((warning) => (
                  <div key={warning}>warning: {warning}</div>
                ))}
              </div>
            ) : null}
            <p className="preflight-sample">{sourceQueue.preflightCard.sample}</p>
            <button className="btn btn-compact" type="button" disabled={!capabilities.canStartConversion} onClick={() => void actions.onStartConversion()}>
              looks good, convert
            </button>
          </div>
        ) : sourceQueue.preflightStatus ? (
          <div className="field-hint preflight-status">{sourceQueue.preflightStatus}</div>
        ) : null}

        <div className="form-row">
          <span className="form-row-label">title</span>
          <div className="form-row-field">
            <input
              id="title-input"
              className="field-input"
              type="text"
              placeholder="optional"
              value={sourceQueue.titleOverride}
              onChange={(event) => {
                actions.setTitleOverride(event.target.value);
                actions.syncSelectedQueueEdits({ title: event.target.value });
              }}
            />
          </div>
        </div>

        <div className="form-row">
          <span className="form-row-label">author</span>
          <div className="form-row-field">
            <input
              id="author-input"
              className="field-input"
              type="text"
              placeholder="optional"
              value={sourceQueue.authorOverride}
              onChange={(event) => {
                actions.setAuthorOverride(event.target.value);
                actions.syncSelectedQueueEdits({ author: event.target.value });
              }}
            />
          </div>
        </div>
      </div>
    </>
  );
}
