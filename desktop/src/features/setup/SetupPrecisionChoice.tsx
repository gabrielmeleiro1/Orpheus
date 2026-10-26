import type { SetupSelection } from "./setupModel";
import { InfoTip } from "./SetupTips";

const PRECISIONS = [
  { value: "4bit", label: "4-bit", hint: "Lowest memory · recommended" },
  { value: "8bit", label: "8-bit", hint: "More precision" },
  { value: "fp16", label: "FP16", hint: "Highest memory use" },
];

/** A three-way segmented control backed by native radios, so keyboard and form semantics stay intact. */
export function SetupPrecisionChoice({ selection, onChange, disabled }: {
  selection: SetupSelection;
  onChange: (value: string) => void;
  disabled: boolean;
}) {
  if (!selection.modelGroups.some((id) => id.startsWith("cleaner-"))) return null;
  if (selection.profile !== "cuda") return null;
  const current = selection.cleanerCudaQuantization ?? "4bit";
  return (
    <fieldset className="setup-section setup-precision" disabled={disabled}>
      <legend className="setup-subheading">Cleaner precision <InfoTip label="Cleaner precision details"><span>Quantization stores model weights with fewer bits to use less GPU memory. Lower precision can affect text quality. All settings share one download; CUDA applies your choice when the cleaner loads. Switch later without downloading again. Leave memory for speech and longer text.</span></InfoTip></legend>
      <div className="seg-control setup-seg">
        {PRECISIONS.map(({ value, label, hint }) => (
          <label key={value} className={`seg-control-btn setup-seg-option${current === value ? " is-active" : ""}`} data-tip={hint} data-tip-pos="bottom">
            <input className="setup-seg-input" type="radio" name="cleaner-precision" value={value} checked={current === value} onChange={() => onChange(value)} />
            {label}<span className="setup-visually-hidden">, {hint}</span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}
