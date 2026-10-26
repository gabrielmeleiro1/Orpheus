import type { ConversionPanelViewModel } from "./useConversionPanelState";
import { AdvancedOptionsView } from "./views/AdvancedOptionsView";
import { CleanerOptionsView } from "./views/CleanerOptionsView";
import { ConversionActionsView } from "./views/ConversionActionsView";
import { FormatChoiceView } from "./views/FormatChoiceView";
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
  // With nothing queued, the intake fills the stage instead of an empty queue.
  const isEmpty = sourceQueue.queueItems.length === 0 && !sourceQueue.untrackedJob;

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
      id="tab-convert"
      className="panel page-container conversion-panel"
      role="tabpanel"
      aria-label="Conversion setup"
      aria-labelledby="tab-btn-convert"
      hidden={shell.hidden}
    >
      <div className={`conversion-form${isEmpty ? " is-empty" : ""}`}>
        {/* Not tab stops: every part of each pane is reached through its own
            controls, and in the narrow layout the panes do not scroll at all. */}
        <div className="conversion-scroll" aria-label="Conversion options">
          <SourceQueueView
            sourceQueue={sourceQueue}
            capabilities={capabilities}
            actions={actions}
            hidden={shell.hidden}
          />
        </div>
        <div className="card conversion-narration" aria-label="Voice settings">
          <div className="conversion-options-sections" aria-label="Narration settings">
            <FormatChoiceView isDocumentMode={shell.isDocumentMode} actions={actions} />
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
            sourceQueue={sourceQueue}
            conversionActions={conversionActions}
            capabilities={capabilities}
            actions={actions}
          />
        </div>
      </div>
    </section>
  );
}
