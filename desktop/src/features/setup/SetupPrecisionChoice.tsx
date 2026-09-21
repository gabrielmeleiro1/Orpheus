import type { SetupSelection } from "./setupModel";

export function SetupPrecisionChoice({ selection, onChange, disabled }: {
  selection: SetupSelection;
  onChange: (value: string) => void;
  disabled: boolean;
}) {
  if (!selection.modelGroups.some((id) => id.startsWith("cleaner-"))) return null;
  if (selection.profile === "mlx") return <p className="setup-model-explanation">This MLX cleaner is already stored in 4-bit precision. Only the selected model is downloaded.</p>;
  if (selection.profile !== "cuda") return null;
  return (
    <fieldset className="setup-section setup-precision" disabled={disabled}>
      <legend className="section-label">Cleaner precision</legend>
      <p className="setup-model-explanation">Quantization stores model weights with fewer bits to use less GPU memory. Lower precision can affect text quality. It changes how your selected cleaner runs; it does not install three models.</p>
      <div className="setup-precision-grid">
        {[
          ["4bit", "4-bit", "Recommended · lowest memory use"],
          ["8bit", "8-bit", "More precision · more memory"],
          ["fp16", "FP16", "16-bit · highest memory use"],
        ].map(([value, label, description]) => (
          <label key={value} className={`setup-precision-card${(selection.cleanerCudaQuantization ?? "4bit") === value ? " is-selected" : ""}`}>
            <input type="radio" name="cleaner-precision" value={value} checked={(selection.cleanerCudaQuantization ?? "4bit") === value} onChange={() => onChange(value)} />
            <span><strong>{label}</strong><small>{description}</small></span>
          </label>
        ))}
      </div>
      <p className="setup-model-explanation">One shared download for all three settings. Cuda applies your choice when the cleaner loads. Switch later without downloading it again. Leave GPU memory for speech generation and longer text, too.</p>
    </fieldset>
  );
}
