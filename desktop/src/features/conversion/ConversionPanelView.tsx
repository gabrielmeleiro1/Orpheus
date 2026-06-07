import type { SourcePreflight } from "../../types/api";
import { TermSelect } from "./TermSelect";
import { compactSample, nowStatus, splitPath } from "./conversionPanelHelpers";
import type {
  ConversionPanelActions,
  ConversionPanelFormState,
  ConversionPanelQueueState,
  ConversionPanelShellState,
} from "./useConversionPanelState";

interface ConversionPanelViewProps {
  shell: ConversionPanelShellState;
  form: ConversionPanelFormState;
  queue: ConversionPanelQueueState;
  actions: ConversionPanelActions;
}

function preflightMeta(preflight: SourcePreflight): string {
  const count = Number(preflight.chapter_count || 0);
  const chapterText = count === 1 ? "1 chapter" : `${count} chapters`;
  const sizeText =
    preflight.size_bytes > 1024 * 1024
      ? `${(preflight.size_bytes / (1024 * 1024)).toFixed(1)} MB`
      : `${Math.max(1, Math.round(preflight.size_bytes / 1024))} KB`;
  return `${preflight.source_type || "source"} / ${chapterText} / ${sizeText}`;
}

export function ConversionPanelView({
  shell,
  form,
  queue,
  actions,
}: ConversionPanelViewProps) {
  const {
    activeTab,
    hidden,
    apiClient,
    connectionState,
    health,
    statusMessage,
    conversionJob,
    optionState,
    isDocumentMode,
    sourcePathPlaceholder,
    startDisabled,
    cancelDisabled,
    mergedFormError,
    runtimeStatusClass,
  } = shell;
  const {
    sourcePath,
    sourceFileLabel,
    titleOverride,
    authorOverride,
    selectedPreflight,
    dragging,
    pasteTitle,
    pastedText,
    pasteBusy,
    pasteStatus,
    voiceSuggestBusy,
    voiceSuggestStatus,
    preflightStatus,
    previewText,
    previewBusy,
    previewStatus,
    previewError,
    previewAudioRef,
  } = form;
  const { queueItems, selectedQueueId, queueRunning, queueStatus } = queue;
  const {
    setSourcePath,
    setTitleOverride,
    setAuthorOverride,
    setDragging,
    setPasteTitle,
    setPastedText,
    setPreviewText,
    onBrowse,
    onAddPastedText,
    onDropZoneDrop,
    onInspectCurrentSource,
    onPreviewVoice,
    onRecommendVoice,
    onRunQueue,
    onStartConversion,
    applySelectedQueueItem,
    moveQueueItem,
    removeQueueItem,
    syncSelectedQueueEdits,
  } = actions;
  if (activeTab === "config") {
    return (
      <section id="tab-config" className="panel config-panel" role="tabpanel" aria-label="Configuration" hidden={hidden}>
        <div className="panel-header">
          <span>configuration</span>
        </div>

        <div className="config-grid">
          <div className="panel-section config-panel-section">
            <div className="section-title">
              <span>cleaner runtime</span>
            </div>
            <div className="form-row">
              <span className="form-row-label">backend</span>
              <div className="form-row-field">
                <TermSelect
                  id="cleaner-backend-select"
                  label="cleaner backend"
                  value={optionState.cleanerBackend}
                  options={optionState.cleanerBackendOptions}
                  placeholder="local (mlx)"
                  onChange={optionState.setCleanerBackend}
                />
              </div>
            </div>

            <div className="form-row">
              <span className="form-row-label">status</span>
              <div className="form-row-field">
                <span id="cleaner-runtime-status" className={runtimeStatusClass}>
                  {optionState.cleanerRuntimeStatus}
                </span>
              </div>
            </div>

            <div className="form-row">
              <span className="form-row-label">model</span>
              <div className="form-row-field">
                <TermSelect
                  id="cleaner-model-select"
                  label="cleaner model"
                  value={optionState.cleanerModel}
                  options={optionState.cleanerModelOptions}
                  placeholder="-"
                  disabled={optionState.cleanerModelOptions.length === 0}
                  onChange={optionState.setCleanerModel}
                />
              </div>
            </div>
          </div>

          <div className="panel-section config-panel-section">
            <div className="section-title">
              <span>openai api cleaner</span>
            </div>
            <div className="form-row">
              <span className="form-row-label">api key</span>
              <div className="form-row-field">
                <input
                  id="cleaner-api-key-input"
                  className="field-input"
                  type="password"
                  autoComplete="off"
                  placeholder="sk-..."
                  value={optionState.cleanerApiKey}
                  onChange={(event) => optionState.setCleanerApiKey(event.target.value)}
                />
              </div>
            </div>

            <div className="action-row">
              <button
                id="fetch-openai-models-btn"
                className="btn btn-compact"
                type="button"
                disabled={!apiClient || optionState.openAiModelFetchLoading || !optionState.cleanerApiKey.trim()}
                onClick={() => void optionState.fetchOpenAiModelIds()}
              >
                {optionState.openAiModelFetchLoading ? "fetching..." : "fetch OpenAI models"}
              </button>
            </div>

            <span
              id="openai-model-fetch-status"
              className={`field-hint${optionState.openAiModelFetchError ? " is-error" : ""}`}
            >
              {optionState.openAiModelFetchStatus}
            </span>
          </div>
        </div>
      </section>
    );
  }

  return (
    <section
      id={isDocumentMode ? "tab-document" : "tab-convert"}
      className="panel"
      role="tabpanel"
      aria-label={isDocumentMode ? "Document conversion setup" : "Audiobook conversion setup"}
      hidden={hidden}
    >
      <div className="panel-header">
        <span>{isDocumentMode ? "document / m4a" : "audiobook / m4b"}</span>
        {isDocumentMode ? (
          <span className="panel-header-meta">single narrated output from pdf, epub, txt, or md</span>
        ) : null}
      </div>

      <div
        id="drop-zone"
        className={`drop-zone${dragging ? " drag-over" : ""}`}
        role="button"
        tabIndex={0}
        aria-label="drop file here"
        onClick={() => void onBrowse()}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            void onBrowse();
          }
        }}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={(event) => {
          event.preventDefault();
          setDragging(false);
        }}
        onDrop={onDropZoneDrop}
      >
        <span className="drop-zone-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" focusable="false">
            <path d="M12 4v11" />
            <path d="M8 11l4 4 4-4" />
            <path d="M5 19h14" />
          </svg>
        </span>
        <span className="drop-zone-copy">
          <strong>{isDocumentMode ? "Drop document here" : "Drop source here"}</strong>
          <span>{isDocumentMode ? ".pdf, .epub, .txt, or .md" : ".pdf or .epub"}</span>
        </span>
        <span className="drop-zone-hint">Click to browse or drag and drop</span>
      </div>

      <span id="source-file-label" className="field-hint source-file-label">
        {sourceFileLabel}
      </span>

      {isDocumentMode ? (
        <div className="panel-section paste-panel" aria-label="Paste markdown source">
          <div className="paste-header">
            <span>paste .md</span>
            <button
              id="add-pasted-text-btn"
              className="btn btn-compact"
              type="button"
              disabled={!apiClient || pasteBusy || !pastedText.trim()}
              onClick={() => void onAddPastedText()}
            >
              {pasteBusy ? "adding..." : "add pasted text"}
            </button>
          </div>
          <div className="paste-grid">
            <span className="form-row-label">title</span>
            <input
              id="paste-title-input"
              className="field-input"
              type="text"
              placeholder="Pasted Document"
              value={pasteTitle}
              onChange={(event) => setPasteTitle(event.target.value)}
            />
            <span className="form-row-label">text</span>
            <textarea
              id="paste-text-input"
              className="field-input paste-textarea"
              placeholder="paste text..."
              value={pastedText}
              onChange={(event) => setPastedText(event.target.value)}
            />
          </div>
          <span id="paste-source-status" className="field-hint">{pasteStatus}</span>
        </div>
      ) : null}

      {queueItems.length > 0 ? (
        <div className="panel-section queue-panel" aria-label="Conversion queue">
          <div className="queue-header">
            <span>queue: {queueItems.length}</span>
            <button
              id="run-queue-btn"
              className="btn btn-compact"
              type="button"
              disabled={
                queueRunning ||
                !apiClient ||
                !queueItems.some((item) => item.status === "ready" || item.status === "warning")
              }
              onClick={() => void onRunQueue()}
            >
              {queueRunning ? "running..." : "run queue"}
            </button>
          </div>
          <div className="queue-list">
            {queueItems.map((item, index) => {
              const selected = item.id === selectedQueueId;
              return (
                <div key={item.id} className={`queue-row${selected ? " is-selected" : ""}`}>
                  <button className="queue-item-main" type="button" onClick={() => applySelectedQueueItem(item)}>
                    <span className="queue-item-title">{item.title || item.label}</span>
                    <span className={`queue-item-status status-${item.status}`}>
                      {item.status}{item.error ? ` / ${item.error}` : ""}
                    </span>
                  </button>
                  <div className="queue-item-actions">
                    <button className="btn btn-compact" type="button" disabled={index === 0 || queueRunning} onClick={() => moveQueueItem(item.id, -1)}>
                      up
                    </button>
                    <button className="btn btn-compact" type="button" disabled={index === queueItems.length - 1 || queueRunning} onClick={() => moveQueueItem(item.id, 1)}>
                      down
                    </button>
                    <button className="btn btn-compact" type="button" disabled={queueRunning} onClick={() => removeQueueItem(item.id)}>
                      remove
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
          <span className="field-hint">{queueStatus}</span>
        </div>
      ) : null}

      <div id="form-error" className="form-error" role="alert" aria-live="assertive">
        {mergedFormError}
      </div>

      <div className="panel-section form-section">
        <div className="form-row">
          <span className="form-row-label">source</span>
          <div className="form-row-field">
            <input
              id="source-path"
              className="field-input"
              type="text"
              placeholder={sourcePathPlaceholder}
              value={sourcePath}
              onChange={(event) => {
                const nextValue = event.target.value;
                setSourcePath(nextValue);
                syncSelectedQueueEdits({ sourcePath: nextValue });
                conversionJob.clearFormError();
              }}
            />
            <button
              id="preflight-source-btn"
              className="btn btn-compact"
              type="button"
              disabled={!apiClient || !sourcePath.trim()}
              onClick={() => onInspectCurrentSource()}
            >
              inspect
            </button>
            <button id="browse-source-btn" className="btn btn-compact" type="button" onClick={() => void onBrowse()}>
              browse
            </button>
          </div>
        </div>

        {selectedPreflight ? (
          <div className="panel-section preflight-card">
            <div className="preflight-title">
              <span>{selectedPreflight.title || splitPath(sourcePath) || "untitled source"}</span>
              <span>{preflightMeta(selectedPreflight)}</span>
            </div>
            <div className="preflight-meta">
              author: {selectedPreflight.author || "unknown"} / source: {selectedPreflight.source_type || "unknown"}
            </div>
            {selectedPreflight.warnings.length > 0 ? (
              <div className="preflight-warnings">
                {selectedPreflight.warnings.map((warning) => (
                  <div key={warning}>warning: {warning}</div>
                ))}
              </div>
            ) : null}
            <p className="preflight-sample">
              {selectedPreflight.sample ? compactSample(selectedPreflight.sample, 360) : "No readable sample found."}
            </p>
            <button className="btn btn-compact" type="button" disabled={startDisabled} onClick={() => void onStartConversion()}>
              looks good, convert
            </button>
          </div>
        ) : preflightStatus ? (
          <div className="field-hint preflight-status">{preflightStatus}</div>
        ) : null}

        <div className="form-row">
          <span className="form-row-label">title</span>
          <div className="form-row-field">
            <input
              id="title-input"
              className="field-input"
              type="text"
              placeholder="optional"
              value={titleOverride}
              onChange={(event) => {
                setTitleOverride(event.target.value);
                syncSelectedQueueEdits({ title: event.target.value });
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
              value={authorOverride}
              onChange={(event) => {
                setAuthorOverride(event.target.value);
                syncSelectedQueueEdits({ author: event.target.value });
              }}
            />
          </div>
        </div>

        <div className="form-row">
          <span className="form-row-label">engine</span>
          <div className="form-row-field">
            <TermSelect
              id="tts-engine-select"
              label="tts engine"
              value={optionState.selectedEngine}
              options={optionState.engineOptions}
              placeholder="-"
              disabled={optionState.loading || optionState.engineOptions.length === 0}
              onChange={optionState.setSelectedEngine}
            />
          </div>
        </div>

        <div className="form-row">
          <span className="form-row-label">voice</span>
          <div className="form-row-stack">
            <input
              id="voice-search"
              className="field-input"
              type="text"
              placeholder="search voices..."
              value={optionState.voiceFilter}
              onChange={(event) => optionState.setVoiceFilter(event.target.value)}
            />
            <TermSelect
              id="voice-select"
              label="voice"
              value={optionState.selectedVoice}
              options={optionState.filteredVoiceOptions}
              placeholder="-"
              disabled={optionState.loading || optionState.filteredVoiceOptions.length === 0}
              onChange={optionState.setSelectedVoice}
            />
            <div className="voice-suggest-actions">
              <button
                id="voice-suggest-btn"
                className="btn btn-compact"
                type="button"
                disabled={!apiClient || voiceSuggestBusy || !sourcePath.trim() || !optionState.selectedEngine}
                onClick={() => void onRecommendVoice()}
              >
                {voiceSuggestBusy ? "analyzing..." : "recommend voice"}
              </button>
              <span className="voice-suggest-status">{voiceSuggestStatus}</span>
            </div>
          </div>
        </div>

        <div className="form-row">
          <span className="form-row-label">preview</span>
          <div className="form-row-stack">
            <div className="form-row-field compact-row">
              <input
                id="voice-preview-text"
                className="field-input"
                type="text"
                placeholder="type preview text..."
                value={previewText}
                onChange={(event) => setPreviewText(event.target.value)}
              />
              <button
                id="voice-preview-btn"
                className="btn btn-compact"
                type="button"
                disabled={previewBusy || !optionState.selectedVoice || !apiClient}
                onClick={() => void onPreviewVoice()}
              >
                {previewBusy ? "..." : "preview"}
              </button>
            </div>
            <span id="voice-preview-status" className={`field-hint${previewError ? " is-error" : ""}`}>
              {previewStatus}
            </span>
            <audio id="voice-preview-audio" ref={previewAudioRef} className="voice-preview-audio" preload="none" />
          </div>
        </div>
      </div>

      <details id="advanced-audio-settings" className="advanced-settings">
        <summary>advanced settings</summary>
        <div className="advanced-settings-body">
          <div className="panel-section form-section">
            <div className="form-row">
              <span className="form-row-label">speed</span>
              <div className="form-row-field">
                <input
                  id="speed-input"
                  className="field-input short-input"
                  type="number"
                  min={0.5}
                  max={3}
                  step={0.05}
                  value={optionState.speedInput}
                  onChange={(event) => optionState.setSpeed(event.target.value)}
                />
              </div>
            </div>

            <div className="form-row">
              <span className="form-row-label">quantization</span>
              <div className="form-row-field">
                <TermSelect
                  id="tts-quantization-select"
                  label="tts quantization"
                  value={optionState.selectedQuantization}
                  options={optionState.quantizationOptions}
                  placeholder="default"
                  onChange={optionState.setSelectedQuantization}
                />
              </div>
            </div>

            <div className="form-row">
              <span className="form-row-label">workers</span>
              <div className="form-row-stack">
                <div className="segmented-control" aria-label="parallel worker mode">
                  <button
                    id="parallel-worker-mode-auto"
                    className={`segment-btn${optionState.parallelWorkerMode === "auto" ? " is-active" : ""}`}
                    type="button"
                    onClick={() => optionState.setParallelWorkerMode("auto")}
                  >
                    auto
                  </button>
                  <button
                    id="parallel-worker-mode-manual"
                    className={`segment-btn${optionState.parallelWorkerMode === "manual" ? " is-active" : ""}`}
                    type="button"
                    onClick={() => optionState.setParallelWorkerMode("manual")}
                  >
                    manual
                  </button>
                </div>
                <div className="form-row-field compact-row">
                  <input
                    id="parallel-worker-max-input"
                    className="field-input short-input"
                    type="number"
                    min={1}
                    max={optionState.manualParallelWorkerUpperBound}
                    step={1}
                    disabled={optionState.parallelWorkerMode === "auto"}
                    value={
                      optionState.parallelWorkerMode === "auto"
                        ? optionState.selectedParallelWorkerLimit
                        : optionState.parallelManualMax
                    }
                    onChange={(event) => optionState.setParallelManualMax(event.target.value)}
                  />
                  <button
                    id="parallel-worker-warmup-btn"
                    className="btn btn-compact"
                    type="button"
                    disabled={!apiClient || optionState.parallelWarmupLoading}
                    onClick={() => void optionState.runParallelWarmup()}
                  >
                    {optionState.parallelWarmupLoading ? "warming..." : "run warm-up"}
                  </button>
                </div>
                <span
                  id="parallel-worker-status"
                  className={`field-hint${optionState.parallelWorkerError ? " is-error" : ""}`}
                >
                  {optionState.parallelWorkerStatus}
                </span>
              </div>
            </div>

            <span className="field-hint">Cleaner backend and model are configured in the configuration tab.</span>
          </div>
        </div>
      </details>

      <div className="panel-section form-section">
        <div className="form-row">
          <span className="form-row-label">backend</span>
          <div className="form-row-field">
            <span id="desktop-backend-state" className="field-hint">
              {nowStatus(connectionState)}
            </span>
          </div>
        </div>
        <div className="form-row">
          <span className="form-row-label">health</span>
          <div className="form-row-field">
            <span id="desktop-health-result" className="field-hint">
              {health ? `status=${health.status}` : "unavailable"}
            </span>
          </div>
        </div>
        <div className="form-row">
          <span className="form-row-label">job</span>
          <div className="form-row-field">
            <span className="field-hint">
              {conversionJob.activeJobId
                ? `${conversionJob.activeJobStatus} (${conversionJob.activeJobId})`
                : "no active conversion"}
            </span>
          </div>
        </div>
        <div className="form-row">
          <span className="form-row-label">detail</span>
          <div className="form-row-field">
            <span className="field-hint">{conversionJob.statusText || statusMessage}</span>
          </div>
        </div>
      </div>

      <button id="start-btn" className="btn btn-primary" type="button" disabled={startDisabled} onClick={() => void onStartConversion()}>
        {conversionJob.isStarting ? "starting..." : queueItems.length > 1 ? "start selected conversion" : "start conversion"}
      </button>

      <div className="action-row" style={{ marginTop: "12px", marginBottom: "10px" }}>
        <button
          id="refresh-btn"
          className="btn btn-compact"
          type="button"
          disabled={!apiClient}
          onClick={() => void conversionJob.refreshActiveConversion()}
        >
          refresh
        </button>
        <button
          id="cancel-btn"
          className="btn btn-compact"
          type="button"
          disabled={cancelDisabled}
          onClick={() => void conversionJob.cancelActiveConversion()}
        >
          {conversionJob.isCancelling ? "cancelling..." : "cancel"}
        </button>
      </div>
    </section>
  );
}
