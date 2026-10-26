import { ConversionHelp } from "./ConversionHelp";
import { AsciiLoader } from "../../../ui/ascii/AsciiLoader";
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

const RECOMMEND_TIP = "Suggest a voice suited to this book";

/**
 * One searchable voice picker with a play button beside it. The engine picker
 * appears only when there is more than one engine to choose from.
 */
export function TtsOptionsView({
  ttsOptions,
  capabilities,
  actions,
}: TtsOptionsViewProps) {
  const showEngine = ttsOptions.engineOptions.length > 1;
  const recommendTip = capabilities.recommendationUnavailableReason || RECOMMEND_TIP;
  return (
    <section className="conversion-section form-section voice-options" aria-labelledby="voice-heading">
      <div className="card-header conversion-section-header voice-header">
        <h2 className="eyebrow" id="voice-heading">Voice</h2>
        <button
          id="voice-suggest-btn"
          className="btn btn-ghost btn-sm btn-flush-end voice-action-btn"
          type="button"
          disabled={!capabilities.canRecommendVoice}
          aria-describedby="voice-suggest-tip"
          data-tip={recommendTip}
          data-tip-pos="left"
          onClick={() => void actions.onRecommendVoice()}
        >
          {ttsOptions.voiceSuggestBusy ? "Analyzing…" : "Recommend"}
        </button>
        <span id="voice-suggest-tip" className="sr-only">{recommendTip}</span>
      </div>

      {showEngine ? (
        <div className="field voice-engine-field">
          <div className="conversion-label-row">
            <label className="field-label" htmlFor="tts-engine-select">Engine</label>
            <ConversionHelp label="Engine help" text="Speech engine used to narrate the book" />
          </div>
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
      ) : null}

      <div className="voice-picker" role="group" aria-labelledby="voice-heading">
        <TermSelect
          id="voice-select"
          label="Voice"
          value={ttsOptions.selectedVoice}
          options={ttsOptions.filteredVoiceOptions}
          placeholder="-"
          disabled={ttsOptions.voiceSelectDisabled}
          search={{
            id: "voice-search",
            label: "Search voices",
            placeholder: "Search voices…",
            value: ttsOptions.voiceFilter,
            onChange: actions.setVoiceFilter,
            emptyText: "No matching voices",
          }}
          onChange={actions.setSelectedVoice}
        />
        <button
          id="voice-preview-btn"
          className={`btn btn-secondary btn-icon voice-preview-btn${ttsOptions.previewBusy ? " is-loading" : ""}`}
          type="button"
          disabled={!capabilities.canPreviewVoice}
          aria-label={ttsOptions.previewBusy ? "Loading voice preview" : "Listen to voice preview"}
          data-tip="Listen"
          data-tip-pos="left"
          onClick={() => void actions.onPreviewVoice()}
        >
          {ttsOptions.previewBusy ? <AsciiLoader variant="ring" /> : <Icon name="play" />}
        </button>
      </div>
      {ttsOptions.voiceSuggestStatus ? (
        <p className="voice-suggest-status voice-status-span" role="status">{ttsOptions.voiceSuggestStatus}</p>
      ) : null}
      <span
        id="voice-preview-status"
        role="status"
        aria-atomic="true"
        className={ttsOptions.previewError ? "field-hint is-error voice-status-span" : "sr-only"}
      >
        {ttsOptions.previewStatus}
      </span>

      <details className="voice-sample disclosure">
        <summary><span>Preview text</span></summary>
        <div className="voice-sample-body">
          <input
            id="voice-preview-text"
            maxLength={320}
            aria-label="Voice preview text"
            className="field-input"
            type="text"
            placeholder="Text to speak…"
            value={ttsOptions.previewText}
            onChange={(event) => actions.setPreviewText(event.target.value)}
          />
        </div>
      </details>
      <audio id="voice-preview-audio" ref={ttsOptions.previewAudioRef} className="voice-preview-audio" preload="none" />
    </section>
  );
}
