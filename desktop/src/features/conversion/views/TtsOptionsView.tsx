import { Icon } from "../../../ui/icons";
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
    <section className="conversion-section form-section voice-options">
      <div className="conversion-section-header">
        <h2 className="conversion-group-heading">Voice</h2>
        <p className="conversion-section-hint">Choose the narrator and preview a short sample before converting.</p>
      </div>

      <div className="voice-controls">
        <div className="field voice-engine-field">
          <label
            className="field-label"
            htmlFor="tts-engine-select"
            data-tip="Speech engine used to narrate the book"
            data-tip-pos="right"
          >
            Engine
          </label>
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

        <div className="field voice-main-field">
          <span className="field-label" id="voice-field-label">Voice</span>
          <div className="voice-selection" role="group" aria-labelledby="voice-field-label">
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
          </div>
        </div>
        <div className="voice-side">
          <span className="field-label voice-side-spacer" aria-hidden="true">&nbsp;</span>
          <button
            id="voice-suggest-btn"
            className="btn voice-action-btn"
            type="button"
            disabled={!capabilities.canRecommendVoice}
            title={capabilities.recommendationUnavailableReason || "Suggest a voice suited to this book from a text sample"}
            data-tip={capabilities.recommendationUnavailableReason || "Suggest a voice suited to this book from a text sample"}
            onClick={() => void actions.onRecommendVoice()}
          >
            {ttsOptions.voiceSuggestBusy ? "Analyzing…" : "Recommend"}
          </button>
        </div>
        {ttsOptions.voiceSuggestStatus ? (
          <span className="voice-suggest-status voice-status-span" role="status">{ttsOptions.voiceSuggestStatus}</span>
        ) : null}

        <div className="field voice-main-field">
          <label className="field-label" htmlFor="voice-preview-text">Preview</label>
          <input
            id="voice-preview-text"
            aria-label="Voice preview text"
            className="field-input"
            type="text"
            placeholder="Text to speak…"
            value={ttsOptions.previewText}
            onChange={(event) => actions.setPreviewText(event.target.value)}
          />
        </div>
        <div className="voice-side">
          <span className="field-label voice-side-spacer" aria-hidden="true">&nbsp;</span>
          <button
            id="voice-preview-btn"
            className="btn voice-action-btn"
            type="button"
            disabled={!capabilities.canPreviewVoice}
            aria-label={ttsOptions.previewBusy ? "Loading voice preview" : "Listen to voice preview"}
            data-tip="Speak the sample with the selected voice"
            onClick={() => void actions.onPreviewVoice()}
          >
            <Icon name="play" />
            {ttsOptions.previewBusy ? "…" : "Listen"}
          </button>
        </div>
        <span
          id="voice-preview-status"
          role="status"
          aria-atomic="true"
          className={`field-hint voice-status-span${ttsOptions.previewError ? " is-error" : ""}`}
        >
          {ttsOptions.previewStatus}
        </span>
        <audio id="voice-preview-audio" ref={ttsOptions.previewAudioRef} className="voice-preview-audio" preload="none" />
      </div>
    </section>
  );
}
