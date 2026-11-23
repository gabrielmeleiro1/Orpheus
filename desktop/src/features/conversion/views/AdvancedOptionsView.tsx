import { ConversionHelp } from "./ConversionHelp";
import { TermSelect } from "../TermSelect";
import { normalizeSpeedInput } from "../conversionOptionsShared";
import { useLiveWorkerRecommendation } from "../liveCapabilities";
import { parallelWorkerStatusLine } from "../useParallelWorkers";
import type {
  AdvancedOptionsViewModel,
  ConversionPanelActions,
} from "../conversionPanelStateTypes";

interface AdvancedOptionsViewProps {
  advancedOptions: AdvancedOptionsViewModel;
  actions: ConversionPanelActions;
}

function collapsedSummary(advancedOptions: AdvancedOptionsViewModel): string {
  // The field keeps what was typed ("1.0") until blur; the summary shows the value in use.
  const speed = normalizeSpeedInput(advancedOptions.speedInput);
  const requested = advancedOptions.selectedParallelWorkerLimit;
  // The summary states what will run, which can be fewer than was asked for.
  const workers = Math.min(requested, advancedOptions.usableParallelWorkerLimit ?? requested);
  // Kept short: the header shares one narrow line with the section title.
  if (workers < requested) return `Speed ${speed} · ${workers} of ${requested} chapters`;
  // One chapter at a time is the default and needs no mention unless more was asked for.
  return workers > 1 ? `Speed ${speed} · ${workers} chapters at once` : `Speed ${speed}`;
}

// Free memory changes on every capability poll; only this line follows it.
function ParallelWorkerStatus({ advancedOptions }: { advancedOptions: AdvancedOptionsViewModel }) {
  const live = useLiveWorkerRecommendation(advancedOptions.liveCapabilities);
  const status = live
    ? parallelWorkerStatusLine(live, advancedOptions.selectedParallelWorkerLimit)
    : advancedOptions.parallelWorkerStatus;
  const detail = live ? live.calculation ?? "" : advancedOptions.parallelWorkerDetail;
  return (
    <span
      id="parallel-worker-status"
      className={advancedOptions.parallelWorkerError ? "field-error" : "field-hint"}
      {...(detail ? { "data-tip": detail } : {})}
    >
      {status}
    </span>
  );
}

export function AdvancedOptionsView({
  advancedOptions,
  actions,
}: AdvancedOptionsViewProps) {
  const quantizationLabels: Record<string, string> = {
    "": "Default", "4bit": "4-bit", "8bit": "8-bit", fp16: "FP16", bf16: "BF16", fp32: "FP32",
  };
  const quantizationOptions = advancedOptions.quantizationOptions.map((option) => ({
    ...option,
    label: quantizationLabels[option.value] ?? option.label,
  }));

  return (
    <details id="advanced-audio-settings" className="advanced-settings disclosure">
      <summary>
        <span>Speed &amp; performance</span>
        <span className="disclosure-card-meta">{collapsedSummary(advancedOptions)}</span>
      </summary>
      <div className="advanced-settings-body">
        <div className="form-grid conversion-form-grid">
          <div className="field">
            <div className="conversion-label-row">
              <label className="field-label" htmlFor="speed-input">Speed</label>
              <ConversionHelp label="Speed help" text="Narration speed baked into the audio. 1.0 is natural pace." />
            </div>
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

          {advancedOptions.showQuantization ? (
            <div className="field">
              <div className="conversion-label-row">
                <label className="field-label" htmlFor="tts-quantization-select">Quantization</label>
                <ConversionHelp label="Quantization help" text="Model precision. Lower bits run faster with a slight quality trade-off." />
              </div>
              <TermSelect
                id="tts-quantization-select"
                label="TTS quantization"
                value={advancedOptions.selectedQuantization}
                options={quantizationOptions}
                placeholder="Default"
                onChange={actions.setSelectedQuantization}
              />
            </div>
          ) : null}

          <div className="field">
            <div className="conversion-label-row">
              <label className="field-label" htmlFor="parallel-worker-max-input">Parallel chapters</label>
              <ConversionHelp label="Parallel chapters help" text="Narrates several chapters at once to finish sooner. Each one loads its own copy of the voice model, so it needs more memory." />
            </div>
            <input
              id="parallel-worker-max-input"
              aria-label="Chapters converted in parallel"
              aria-invalid={advancedOptions.parallelWorkerError || undefined}
              aria-describedby={advancedOptions.parallelWorkerStatus ? "parallel-worker-status" : undefined}
              className="field-input short-input"
              type="number"
              min={1}
              max={advancedOptions.manualParallelWorkerUpperBound}
              step={1}
              value={advancedOptions.parallelManualMax}
              onChange={(event) => actions.setParallelManualMax(event.target.value)}
              onBlur={(event) => actions.setParallelManualMax(event.target.value.trim() || "1")}
            />
            {advancedOptions.parallelWorkerStatus ? <ParallelWorkerStatus advancedOptions={advancedOptions} /> : null}
          </div>
        </div>
      </div>
    </details>
  );
}
