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
        <span className="form-row-label" data-tip="Speech engine used to narrate the book" data-tip-pos="right">Engine</span>
        <div className="form-row-field">
          <TermSelect
            id="tts-engine-select"
            label="TTS engine"
            value={ttsOptions.selectedEngine}
            options={ttsOptions.engineOptions}
            placeholder="-"
            disabled={ttsOptions.engineDisabled}
            onChange={actions.setSelectedEngine}
          />
        </div>
      </div>

      <div className="form-row">
        <span className="form-row-label">Voice</span>
        <div className="form-row-stack">
          <input
            id="voice-search"
            aria-label="Search voices"
            className="field-input"
            type="text"
            placeholder="Search voices…"
            value={ttsOptions.voiceFilter}
            onChange={(event) => actions.setVoiceFilter(event.target.value)}
          />
          <TermSelect
            id="voice-select"
            label="Voice"
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
              data-tip="Suggest a voice suited to this book from a text sample"
              onClick={() => void actions.onRecommendVoice()}
            >
              {ttsOptions.voiceSuggestBusy ? "Analyzing…" : "Recommend"}
            </button>
            <span className="voice-suggest-status">{ttsOptions.voiceSuggestStatus}</span>
          </div>
        </div>
      </div>

      <div className="form-row">
        <span className="form-row-label">Preview</span>
        <div className="form-row-stack">
          <div className="form-row-field compact-row">
            <input
              id="voice-preview-text"
              aria-label="Voice preview text"
              className="field-input"
              type="text"
              placeholder="Text to speak…"
              value={ttsOptions.previewText}
              onChange={(event) => actions.setPreviewText(event.target.value)}
            />
            <button
              id="voice-preview-btn"
              className="btn btn-compact"
              type="button"
              disabled={!capabilities.canPreviewVoice}
              data-tip="Speak the sample with the selected voice"
              onClick={() => void actions.onPreviewVoice()}
            >
              {ttsOptions.previewBusy ? "…" : "Listen"}
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
