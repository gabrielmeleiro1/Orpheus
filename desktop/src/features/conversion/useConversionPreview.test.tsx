// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import type { useConversionOptions } from "./useConversionOptions";
import { useConversionPreview } from "./useConversionPreview";

let root: Root;
let current: ReturnType<typeof useConversionPreview>;
let options: ReturnType<typeof useConversionOptions>;
let client: ApiClient;
let sourcePath: string;

function Probe() {
  current = useConversionPreview({ apiClient: client, optionState: options, sourcePath, selectedPreflight: null, sampleText: null });
  return null;
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  root = createRoot(document.createElement("div"));
  sourcePath = "a.txt";
  options = { selectedVoice: "A", selectedEngine: "kokoro", selectedQuantization: "default", speed: 1,
    cleanerBackend: "api", cleanerApiKey: "key", cleanerModel: "model", setSelectedVoice: vi.fn(),
  } as unknown as ReturnType<typeof useConversionOptions>;
});

afterEach(async () => {
  await act(async () => root.unmount());
  vi.restoreAllMocks();
});

it.each(["source", "engine", "voice", "cleaner"])("discards a recommendation after changing %s", async (change) => {
  let resolve!: (value: unknown) => void;
  client = { suggestVoice: vi.fn(() => new Promise((done) => { resolve = done; })) } as unknown as ApiClient;
  await act(async () => root.render(<Probe />));
  const originalText = current.previewText;
  let pending: Promise<void>;
  await act(async () => { pending = current.onRecommendVoice(); });
  expect(current.voiceSuggestBusy).toBe(true);
  if (change === "source") sourcePath = "b.txt";
  if (change === "engine") options = { ...options, selectedEngine: "other" };
  if (change === "voice") options = { ...options, selectedVoice: "B" };
  if (change === "cleaner") options = { ...options, cleanerModel: "new-model" };
  await act(async () => root.render(<Probe />));
  expect(current.voiceSuggestBusy).toBe(false);
  await act(async () => {
    resolve({ voice_suggestion: { recommended_voice_id: "Old recommendation" }, book_sample: { excerpt: "Old excerpt" } });
    await pending;
  });
  expect(options.setSelectedVoice).not.toHaveBeenCalled();
  expect(current.previewText).toBe(originalText);
  expect(current.voiceSuggestStatus).not.toContain("Old recommendation");
});

it.each(["voice", "engine", "speed", "quantization", "text"])("discards a preview after changing %s", async (change) => {
  let resolve!: (value: Blob) => void;
  client = { requestBlob: vi.fn(() => new Promise<Blob>((done) => { resolve = done; })) } as unknown as ApiClient;
  await act(async () => root.render(<Probe />));
  const audio = { pause: vi.fn(), play: vi.fn(async () => {}), src: "", currentTime: 0 } as unknown as HTMLAudioElement;
  current.previewAudioRef.current = audio;
  let pending: Promise<void>;
  await act(async () => { pending = current.onPreviewVoice(); });
  if (change === "voice") options = { ...options, selectedVoice: "B" };
  if (change === "engine") options = { ...options, selectedEngine: "other" };
  if (change === "speed") options = { ...options, speed: 1.2 };
  if (change === "quantization") options = { ...options, selectedQuantization: "4bit" };
  await act(async () => {
    if (change === "text") current.setPreviewText("New text");
    root.render(<Probe />);
  });
  expect(current.previewBusy).toBe(false);
  await act(async () => { resolve(new Blob(["old audio"])); await pending; });
  expect(audio.play).not.toHaveBeenCalled();
  expect(audio.src).toBe("");
  expect(current.previewStatus).not.toContain("Previewing A");
});

it("does not publish an obsolete preview status after play resolves", async () => {
  client = { requestBlob: vi.fn(async () => new Blob(["audio"])) } as unknown as ApiClient;
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:preview");
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
  await act(async () => root.render(<Probe />));
  let finishPlay!: () => void;
  const audio = { pause: vi.fn(), play: vi.fn(() => new Promise<void>((resolve) => { finishPlay = resolve; })), src: "", currentTime: 0 } as unknown as HTMLAudioElement;
  current.previewAudioRef.current = audio;
  let pending: Promise<void>;
  await act(async () => { pending = current.onPreviewVoice(); });
  expect(audio.play).toHaveBeenCalledOnce();
  vi.mocked(audio.pause).mockClear();
  options = { ...options, selectedVoice: "B" };
  await act(async () => root.render(<Probe />));
  expect(audio.pause).toHaveBeenCalled();
  await act(async () => { finishPlay(); await pending; });
  expect(current.previewStatus).not.toContain("Previewing A");
  expect(current.previewBusy).toBe(false);
});
