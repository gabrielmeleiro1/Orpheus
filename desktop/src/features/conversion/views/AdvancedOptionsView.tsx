import { TermSelect } from "../TermSelect";
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
                value={advancedOptions.speedInput}
                onChange={(event) => actions.setSpeed(event.target.value)}
              />
            </div>
          </div>

          <div className="form-row">
            <span className="form-row-label">quantization</span>
            <div className="form-row-field">
              <TermSelect
                id="tts-quantization-select"
                label="tts quantization"
                value={advancedOptions.selectedQuantization}
                options={advancedOptions.quantizationOptions}
                placeholder="default"
                onChange={actions.setSelectedQuantization}
              />
            </div>
          </div>

          <div className="form-row">
            <span className="form-row-label">workers</span>
            <div className="form-row-stack">
              <div className="segmented-control" aria-label="parallel worker mode">
                <button
                  id="parallel-worker-mode-auto"
                  className={`segment-btn${advancedOptions.parallelWorkerMode === "auto" ? " is-active" : ""}`}
                  type="button"
                  onClick={() => actions.setParallelWorkerMode("auto")}
                >
                  auto
                </button>
                <button
                  id="parallel-worker-mode-manual"
                  className={`segment-btn${advancedOptions.parallelWorkerMode === "manual" ? " is-active" : ""}`}
                  type="button"
                  onClick={() => actions.setParallelWorkerMode("manual")}
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
                  onClick={() => void actions.onRunParallelWarmup()}
                >
                  {advancedOptions.parallelWarmupLoading ? "warming..." : "run warm-up"}
                </button>
              </div>
              <span
                id="parallel-worker-status"
                className={`field-hint${advancedOptions.parallelWorkerError ? " is-error" : ""}`}
              >
                {advancedOptions.parallelWorkerStatus}
              </span>
            </div>
          </div>

          <span className="field-hint">Cleaner backend and model are configured in the configuration tab.</span>
        </div>
      </div>
    </details>
  );
}
