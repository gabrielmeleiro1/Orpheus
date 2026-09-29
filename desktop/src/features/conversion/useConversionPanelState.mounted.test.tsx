// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import type { ConversionJobController } from "./useConversionJob";
import { useConversionPanelState } from "./useConversionPanelState";
import { SourceQueueView } from "./views/SourceQueueView";

vi.mock("./useConversionOptions", () => ({ useConversionOptions: () => ({
  selectedEngine: "kokoro", selectedVoice: "voice", cleanerBackend: "basic",
  cleanerApiKey: "", cleanerModel: "", cleanerModelOptions: [], engineOptions: [],
  filteredVoiceOptions: [], quantizationOptions: [],
}) }));

it.each(["single conversion", "queue conversion", "failed reconciliation"])("Clear finished retains preflight warnings and clears warnings after %s", async (scenario) => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const deleteUpload = vi.fn(async () => ({}));
  const client = { deleteUpload, getActiveConversion: vi.fn(async () => {
    if (scenario === "failed reconciliation") throw new Error("Backend disconnected");
    return { active: false, job: null };
  }), uploadSourceText: vi.fn(async () => ({
    source_handle: "upload:paste.md", original_name: "paste.md",
    preflight: { title: "Paste", author: null, chapter_count: 1, source_type: "md",
      sample: "sample", warnings: ["Short source"], size_bytes: 10, requires_ocr: false, pdf_text_layer: null },
  })) } as unknown as ApiClient;
  const conversionJob = {
    clearFormError: vi.fn(),
    startConversion: vi.fn(async () => ({ ok: true, jobId: "converted-paste" })),
    waitForTerminalState: vi.fn(async (jobId: string) => ({ jobId, status: "unknown", message: "" })),
    refreshActiveConversion: vi.fn(),
  } as unknown as ConversionJobController;
  let current!: ReturnType<typeof useConversionPanelState>;
  function Probe() {
    current = useConversionPanelState({ activeTab: "document", hidden: false, apiClient: client,
      connectionState: "connected", health: null, statusMessage: "",
      conversionJob,
      onOpenSettings: vi.fn(), onOpenRuntimeSetup: vi.fn() });
    return <SourceQueueView sourceQueue={current.sourceQueue} capabilities={current.capabilities} actions={current.actions} />;
  }
  const container = document.createElement("div");
  const root = createRoot(container);
  try {
    await act(async () => root.render(<Probe />));
    await act(async () => current.actions.setPastedText("Pasted source"));
    await act(async () => { await current.actions.onAddPastedText(); });
    expect(current.sourceQueue.pastedText).toBe("");
    expect(current.sourceQueue.queueItems[0].finished).toBe(false);
    const clear = [...container.querySelectorAll("button")].find((button) => button.textContent === "Clear finished");
    await act(async () => clear?.click());
    expect(current.sourceQueue.queueCount).toBe(1);
    expect(deleteUpload).not.toHaveBeenCalled();

    await act(async () => {
      if (scenario === "single conversion") await current.actions.onStartConversion();
      else await current.actions.onRunQueue();
    });
    expect(conversionJob.waitForTerminalState).toHaveBeenCalledWith("converted-paste");
    expect(current.sourceQueue.queueItems[0].statusText).toContain("conversion status unknown");
    expect(current.sourceQueue.queueItems[0].finished).toBe(true);
    expect(clear?.disabled).toBe(false);
    await act(async () => clear?.click());
    expect(current.sourceQueue.queueCount).toBe(0);
    expect(deleteUpload).toHaveBeenCalledExactlyOnceWith("upload:paste.md");
  } finally {
    await act(async () => root.unmount());
  }
});
