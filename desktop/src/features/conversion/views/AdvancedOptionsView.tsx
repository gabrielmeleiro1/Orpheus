import { TermSelect } from "../TermSelect";
import { normalizeSpeedInput } from "../conversionOptionsShared";
import type {
  AdvancedOptionsViewModel,
  ConversionPanelActions,
} from "../conversionPanelStateTypes";

interface AdvancedOptionsViewProps {
  advancedOptions: AdvancedOptionsViewModel;
  actions: ConversionPanelActions;
}

function collapsedSummary(advancedOptions: AdvancedOptionsViewModel): string {
  const speed = advancedOptions.speedInput.trim() || "1";
  const workers = advancedOptions.selectedParallelWorkerLimit;
  const workerText = workers > 1 ? `${workers} chapters at once` : "one chapter at a time";
  return `Speed ${speed} · ${workerText}`;
}

export function AdvancedOptionsView({
  advancedOptions,
  actions,
}: AdvancedOptionsViewProps) {
  return (
    <details id="advanced-audio-settings" className="advanced-settings disclosure disclosure-card">
      <summary>
        <span>Advanced</span>
        <span className="disclosure-card-meta">{collapsedSummary(advancedOptions)}</span>
      </summary>
      <div className="advanced-settings-body form-section">
        <div className="form-row">
          <span className="form-row-label" data-tip="Narration speed baked into the audio. 1.0 is natural pace." data-tip-pos="right">Speed</span>
          <div className="form-row-field">
            <input
              id="speed-input"
              aria-label="Narration speed"
              className="field-input short-input"
              type="number"
              min={0.5}
              max={3}
              step={0.05}
              value={advancedOptions.speedInput}
              onChange={(event) => actions.setSpeed(event.target.value)}
              onBlur={(event) => actions.setSpeed(normalizeSpeedInput(event.target.value))}
            />
          </div>
        </div>

        {advancedOptions.showQuantization ? (
          <div className="form-row">
            <span
              className="form-row-label"
              data-tip="Model precision. Lower bits run faster with a slight quality trade-off."
              data-tip-pos="right"
            >
              Quantization
            </span>
            <div className="form-row-field">
              <TermSelect
                id="tts-quantization-select"
                label="TTS quantization"
                value={advancedOptions.selectedQuantization}
                options={advancedOptions.quantizationOptions}
                placeholder="default"
                onChange={actions.setSelectedQuantization}
              />
            </div>
          </div>
        ) : null}

        <div className="form-row">
          <span
            className="form-row-label"
            data-tip="How many chapters synthesize at the same time. Each one loads its own copy of the voice model, so higher values need more memory."
            data-tip-pos="right"
          >
            Parallel chapters
          </span>
          <div className="form-row-stack">
            <div className="form-row-field">
              <input
                id="parallel-worker-max-input"
                aria-label="Chapters converted in parallel"
                className="field-input short-input"
                type="number"
                min={1}
                max={advancedOptions.manualParallelWorkerUpperBound}
                step={1}
                value={advancedOptions.parallelManualMax}
                onChange={(event) => actions.setParallelManualMax(event.target.value)}
              />
            </div>
            {advancedOptions.parallelWorkerStatus ? (
              <span
                id="parallel-worker-status"
                className={`field-hint${advancedOptions.parallelWorkerError ? " is-error" : ""}`}
                {...(advancedOptions.parallelWorkerDetail
                  ? { "data-tip": advancedOptions.parallelWorkerDetail }
                  : {})}
              >
                {advancedOptions.parallelWorkerStatus}
              </span>
            ) : null}
          </div>
        </div>
      </div>
    </details>
  );
}
