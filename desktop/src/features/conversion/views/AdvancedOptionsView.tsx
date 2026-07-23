import { TermSelect } from "../TermSelect";
import { normalizeSpeedInput } from "../conversionOptionsShared";
import type {
  AdvancedOptionsViewModel,
  ConversionCapabilities,
  ConversionPanelActions,
} from "../conversionPanelStateTypes";

interface AdvancedOptionsViewProps {
  advancedOptions: AdvancedOptionsViewModel;
  capabilities: ConversionCapabilities;
  actions: ConversionPanelActions;
}

export function AdvancedOptionsView({
  advancedOptions,
  capabilities,
  actions,
}: AdvancedOptionsViewProps) {
  return (
    <details id="advanced-audio-settings" className="advanced-settings disclosure">
      <summary>Advanced</summary>
      <div className="advanced-settings-body">
        <div className="panel-section form-section">
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

          <div className="form-row">
            <span
              className="form-row-label"
              data-tip="How many chapters synthesize at once. Auto sizes the pool from a warm-up benchmark."
              data-tip-pos="right"
            >
              Workers
            </span>
            <div className="form-row-stack">
              <div className="seg-control" aria-label="Parallel worker mode">
                <button
                  id="parallel-worker-mode-auto"
                  className={`seg-control-btn${advancedOptions.parallelWorkerMode === "auto" ? " is-active" : ""}`}
                  type="button"
                  data-tip="Size the worker pool automatically"
                  onClick={() => actions.setParallelWorkerMode("auto")}
                >
                  Auto
                </button>
                <button
                  id="parallel-worker-mode-manual"
                  className={`seg-control-btn${advancedOptions.parallelWorkerMode === "manual" ? " is-active" : ""}`}
                  type="button"
                  data-tip="Set the worker count yourself"
                  onClick={() => actions.setParallelWorkerMode("manual")}
                >
                  Manual
                </button>
              </div>
              <div className="form-row-field compact-row">
                <input
                  id="parallel-worker-max-input"
                  aria-label="Maximum parallel workers"
                  className="field-input short-input"
                  type="number"
                  min={1}
                  max={advancedOptions.manualParallelWorkerUpperBound}
                  step={1}
                  disabled={advancedOptions.parallelWorkerMode === "auto"}
                  value={
                    advancedOptions.parallelWorkerMode === "auto"
                      ? advancedOptions.selectedParallelWorkerLimit
                      : advancedOptions.parallelManualMax
                  }
                  onChange={(event) => actions.setParallelManualMax(event.target.value)}
                />
                <button
                  id="parallel-worker-warmup-btn"
                  className="btn btn-compact"
                  type="button"
                  disabled={!capabilities.canRunParallelWarmup}
                  data-tip="Benchmark how many parallel workers this machine sustains"
                  onClick={() => void actions.onRunParallelWarmup()}
                >
                  {advancedOptions.parallelWarmupLoading ? "Warming…" : "Warm-up"}
                </button>
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
      </div>
    </details>
  );
}
