import { TermSelect } from "../TermSelect";
import type {
  ConversionCapabilities,
  ConversionPanelActions,
  TtsOptionsViewModel,
} from "../conversionPanelStateTypes";

interface TtsOptionsViewProps {
  ttsOptions: TtsOptionsViewModel;
  capabilities: ConversionCapabilities;
  actions: ConversionPanelActions;
}

export function TtsOptionsView({
  ttsOptions,
  capabilities,
  actions,
}: TtsOptionsViewProps) {
  return (
    <div className="panel-section form-section">
      <div className="form-row">
        <span className="form-row-label">engine</span>
        <div className="form-row-field">
          <TermSelect
            id="tts-engine-select"
            label="tts engine"
            value={ttsOptions.selectedEngine}
            options={ttsOptions.engineOptions}
            placeholder="-"
            disabled={ttsOptions.engineDisabled}
            onChange={actions.setSelectedEngine}
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
            value={ttsOptions.voiceFilter}
            onChange={(event) => actions.setVoiceFilter(event.target.value)}
          />
          <TermSelect
            id="voice-select"
            label="voice"
            value={ttsOptions.selectedVoice}
            options={ttsOptions.filteredVoiceOptions}
            placeholder="-"
            disabled={ttsOptions.voiceSelectDisabled}
            onChange={actions.setSelectedVoice}
          />
          <div className="voice-suggest-actions">
            <button
              id="voice-suggest-btn"
              className="btn btn-compact"
              type="button"
              disabled={!capabilities.canRecommendVoice}
              onClick={() => void actions.onRecommendVoice()}
            >
              {ttsOptions.voiceSuggestBusy ? "analyzing..." : "recommend voice"}
            </button>
            <span className="voice-suggest-status">{ttsOptions.voiceSuggestStatus}</span>
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
              value={ttsOptions.previewText}
              onChange={(event) => actions.setPreviewText(event.target.value)}
            />
            <button
              id="voice-preview-btn"
              className="btn btn-compact"
              type="button"
              disabled={!capabilities.canPreviewVoice}
              onClick={() => void actions.onPreviewVoice()}
            >
              {ttsOptions.previewBusy ? "..." : "preview"}
            </button>
          </div>
          <span id="voice-preview-status" className={`field-hint${ttsOptions.previewError ? " is-error" : ""}`}>
            {ttsOptions.previewStatus}
          </span>
          <audio id="voice-preview-audio" ref={ttsOptions.previewAudioRef} className="voice-preview-audio" preload="none" />
        </div>
      </div>
    </div>
  );
}
