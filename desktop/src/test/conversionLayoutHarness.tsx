// Real conversion views and styles, synthetic state: no backend or model needed.
import { useState } from "react";
import { createRoot } from "react-dom/client";
import { ConversionPanelView } from "../features/conversion/ConversionPanelView";
import { SettingsPage } from "../features/setup/SettingsPage";
import { conversionPanelViewModel } from "./conversionPanelViewModel";
import "../styles.css";
import "../features/conversion/conversion.css";

declare global {
  interface Window {
    conversionLayout: {
      addSource: () => void;
      inspect: () => void;
      rename: () => void;
      hide: (hidden: boolean) => void;
      collapse: (collapsed: boolean) => void;
      checking: (checking: boolean) => void;
      fillQueue: () => void;
    };
  }
}

function Harness() {
  const documentMode = new URLSearchParams(location.search).has("document");
  const [count, setCount] = useState(0);
  const [ready, setReady] = useState(false);
  const [longTitle, setLongTitle] = useState(false);
  const [hidden, setHidden] = useState(false);
  const [checking, setChecking] = useState(false);
  const [collapsed, setCollapsed] = useState(() =>
    matchMedia("(max-width: 760px), (max-height: 540px)").matches,
  );
  const model = conversionPanelViewModel();
  const title = longTitle ? "A very long source title ".repeat(12) + "x".repeat(120) : "Prepared source";
  const addSource = () => { setReady(false); setCount((current) => current + 1); };
  window.conversionLayout = {
    addSource, inspect: () => setReady(true), rename: () => setLongTitle(true),
    hide: setHidden, collapse: setCollapsed,
    checking: setChecking, fillQueue: () => setCount(25),
  };
  model.shell = { activeTab: "convert", isDocumentMode: documentMode, hidden };
  model.sourceQueue = {
    ...model.sourceQueue,
    isDocumentMode: documentMode,
    sourceInput: "file",
    sourcePath: count ? `C:/Books/${title}.epub` : "",
    queueCount: count,
    queueStatus: `${count} item${count === 1 ? "" : "s"} added`,
    queueItems: Array.from({ length: count }, (_, index) => ({
      id: String(index), title, meta: "Example author · EPUB · 15 chapters", selected: index === 0,
      statusText: index === 1 ? "Converting…" : "Ready",
      statusClassName: `queue-item-status status-${index === 1 ? "converting" : "ready"}`, converting: index === 1,
      canRemove: true, canMoveUp: index > 0, canMoveDown: index < count - 1,
    })),
    preflightCard: count && ready ? {
      title, meta: "EPUB · 15 chapters · 433 KB", author: "Example author", sourceType: "epub",
      chapterCount: 15, chapterTitles: Array.from({ length: 15 }, (_, index) => `Chapter ${index + 1}: A journey begins`),
      sizeLabel: "433 KB", warnings: ["Low text layer: check the sample before converting."],
      sample: "A prepared source sample. ".repeat(30), canConvert: true, blockingReason: null,
    } : null,
    preflightStatus: count && !ready ? "Inspecting source…" : "",
  };
  model.ttsOptions = {
    ...model.ttsOptions, selectedEngine: "kokoro", engineDisabled: false,
    engineOptions: [{ value: "kokoro", label: "Kokoro" }],
    filteredVoiceOptions: Array.from({ length: 50 }, (_, index) => ({ value: String(index), label: `Voice ${index}` })),
    selectedVoice: "45", voiceSelectDisabled: false,
  };
  model.capabilities = {
    ...model.capabilities, showRunQueue: count > 1, showStartConversion: count <= 1,
    canStartConversion: count === 1 && !checking, canRunQueue: count > 1 && !checking,
  };
  model.conversionActions = {
    ...model.conversionActions, showRefreshJob: checking, hasActiveJob: checking,
    startLabel: checking ? "Checking…" : "Start", cancelLabel: checking ? "Cancelling…" : "Cancel", isStarting: checking,
    blockingReason: checking ? "Checking the active conversion before starting another source." : null,
  };
  model.actions = { ...model.actions, onBrowse: async () => addSource(), onInspectCurrentSource: () => setReady(true) };
  return (
    <main className={`app-frame${collapsed ? " sidebar-collapsed" : ""}`}>
      <aside className="rail" hidden={collapsed}>
        <div className="brand">Orpheus</div>
        <nav className="rail-nav"><button className="rail-tab active">Convert</button></nav>
        <div className="rail-bottom">Connected</div>
      </aside>
      <div className="workspace-shell">
        <header className="studio-status">
          <div className="page-container page-header"><div className="page-title-block">
            <h1>{documentMode ? "Document" : "Convert"}</h1>
          </div></div>
        </header>
        <div className="main-workspace has-conversion">
          <SettingsPage active={false} apiClient={null}>
            <ConversionPanelView {...model} />
          </SettingsPage>
        </div>
      </div>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<Harness />);
