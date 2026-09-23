import { useLayoutEffect, useRef } from "react";
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
  const panelRef = useRef<HTMLElement>(null);

  useLayoutEffect(() => {
    const panel = panelRef.current;
    const bar = panel?.querySelector<HTMLElement>(".conversion-actions");
    if (!panel || !bar) return;
    const measure = () => {
      panel.style.setProperty("--conversion-actions-height", `${bar.getBoundingClientRect().height}px`);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(bar);
    return () => observer.disconnect();
  }, [shell.activeTab, shell.hidden]);

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
      ref={panelRef}
      id={shell.isDocumentMode ? "tab-document" : "tab-convert"}
      className="panel conversion-panel"
      role="tabpanel"
      aria-label={shell.isDocumentMode ? "Document conversion setup" : "Audiobook conversion setup"}
      aria-labelledby={`tab-btn-${shell.isDocumentMode ? "document" : "convert"}`}
      hidden={shell.hidden}
    >
      <div className="conversion-scroll" tabIndex={0} aria-label="Conversion options">
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
      </div>
      <ConversionActionsView
        sourceQueue={sourceQueue}
        conversionActions={conversionActions}
        capabilities={capabilities}
        actions={actions}
      />
    </section>
  );
}
