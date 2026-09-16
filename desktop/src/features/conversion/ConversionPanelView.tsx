import type { ConversionPanelViewModel } from "./useConversionPanelState";
import { AdvancedOptionsView } from "./views/AdvancedOptionsView";
import { CleanerOptionsView } from "./views/CleanerOptionsView";
import { ConversionActionsView } from "./views/ConversionActionsView";
import { SourceQueueView } from "./views/SourceQueueView";
import { TtsOptionsView } from "./views/TtsOptionsView";

export function ConversionPanelView({
  shell,
  capabilities,
  sourceQueue,
  ttsOptions,
  cleanerOptions,
  advancedOptions,
  conversionActions,
  actions,
}: ConversionPanelViewModel) {
  if (shell.activeTab === "config") {
    return (
      <CleanerOptionsView
        cleanerOptions={cleanerOptions}
        capabilities={capabilities}
        actions={actions}
        hidden={shell.hidden}
      />
    );
  }

  return (
    <section
      id={shell.isDocumentMode ? "tab-document" : "tab-convert"}
      className="panel conversion-panel"
      role="tabpanel"
      aria-label={shell.isDocumentMode ? "Document conversion setup" : "Audiobook conversion setup"}
      aria-labelledby={`tab-btn-${shell.isDocumentMode ? "document" : "convert"}`}
      hidden={shell.hidden}
    >
      <header className="conversion-context">
        <p className="conversion-lede">
          {shell.isDocumentMode
            ? "Turn a document or pasted text into a focused listening copy."
            : "Prepare a source, choose a voice, and export a chaptered audiobook."}
        </p>
        <span className="conversion-header-meta">{shell.isDocumentMode ? "M4A" : "M4B"}</span>
      </header>

      <div className="conversion-form">
        <SourceQueueView
          sourceQueue={sourceQueue}
          capabilities={capabilities}
          actions={actions}
          hidden={shell.hidden}
        />
        <TtsOptionsView
          ttsOptions={ttsOptions}
          capabilities={capabilities}
          actions={actions}
        />
        <AdvancedOptionsView
          advancedOptions={advancedOptions}
          actions={actions}
        />
      </div>
      <ConversionActionsView
        conversionActions={conversionActions}
        capabilities={capabilities}
        actions={actions}
      />
    </section>
  );
}
