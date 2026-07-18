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
      className="panel"
      role="tabpanel"
      aria-label={shell.isDocumentMode ? "Document conversion setup" : "Audiobook conversion setup"}
      hidden={shell.hidden}
    >
      <div className="panel-header">
        <span>{shell.isDocumentMode ? "Document → M4A" : "Audiobook → M4B"}</span>
        {shell.isDocumentMode ? (
          <span className="panel-header-meta">One narrated file from PDF, EPUB, TXT, or MD</span>
        ) : null}
      </div>

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
        capabilities={capabilities}
        actions={actions}
      />
      <ConversionActionsView
        conversionActions={conversionActions}
        capabilities={capabilities}
        actions={actions}
      />
    </section>
  );
}
