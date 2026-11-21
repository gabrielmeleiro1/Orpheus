import type { ConversionMode } from "../../../types/api";
import type { ConversionPanelActions } from "../conversionPanelStateTypes";

const FORMATS: Array<{ mode: ConversionMode; title: string; hint: string }> = [
  { mode: "audiobook", title: "Audiobook", hint: "An .m4b file with chapter markers. Each chapter is announced: “Chapter 1”, “Chapter 2”…" },
  { mode: "document_audio", title: "Document", hint: "A single .m4a file. Each section is announced: “Section 1”, “Section 2”…" },
];

interface FormatChoiceViewProps {
  isDocumentMode: boolean;
  actions: Pick<ConversionPanelActions, "setConversionMode">;
}

/** Two-way segmented radio group with one line saying what the selected format produces. */
export function FormatChoiceView({ isDocumentMode, actions }: FormatChoiceViewProps) {
  const current: ConversionMode = isDocumentMode ? "document_audio" : "audiobook";
  return (
    <section className="conversion-section format-options" aria-labelledby="format-heading">
      <div className="card-header conversion-section-header">
        <h2 className="eyebrow" id="format-heading">Format</h2>
      </div>
      <div className="seg-control format-choices" role="radiogroup" aria-labelledby="format-heading">
        {FORMATS.map(({ mode, title, hint }) => (
          <label key={mode} className={`seg-control-btn format-choice${current === mode ? " is-active" : ""}`}>
            <input
              type="radio"
              name="conversion-format"
              value={mode}
              checked={current === mode}
              aria-describedby={`format-hint-${mode}`}
              onChange={() => actions.setConversionMode(mode)}
            />
            <span className="format-choice-title">{title}</span>
            <span id={`format-hint-${mode}`} className="sr-only">{hint}</span>
          </label>
        ))}
      </div>
      <p className="field-hint format-summary">{FORMATS.find(({ mode }) => mode === current)?.hint}</p>
    </section>
  );
}
