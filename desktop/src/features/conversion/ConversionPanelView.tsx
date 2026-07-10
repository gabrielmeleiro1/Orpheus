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
        <span>{shell.isDocumentMode ? "document / m4a" : "audiobook / m4b"}</span>
        {shell.isDocumentMode ? (
          <span className="panel-header-meta">single narrated output from pdf, epub, txt, or md</span>
        ) : null}
      </div>

      <SourceQueueView
        sourceQueue={sourceQueue}
        capabilities={capabilities}
        actions={actions}
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
