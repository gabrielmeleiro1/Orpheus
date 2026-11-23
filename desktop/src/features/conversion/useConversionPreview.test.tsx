// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import { createRequestTimeoutError } from "../../api/http";
import type { useConversionOptions } from "./useConversionOptions";
import { useConversionPreview } from "./useConversionPreview";

let root: Root;
let current: ReturnType<typeof useConversionPreview>;
let options: ReturnType<typeof useConversionOptions>;
let client: ApiClient;
let sourcePath: string;
let hidden: boolean;

function Probe() {
  current = useConversionPreview({ apiClient: client, optionState: options, sourcePath, selectedPreflight: null, sampleText: null, hidden });
  return null;
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  root = createRoot(document.createElement("div"));
  sourcePath = "a.txt";
  hidden = false;
  options = { selectedVoice: "A", selectedEngine: "kokoro", selectedQuantization: "default", speed: 1,
    cleanerBackend: "api", cleanerApiKey: "key", cleanerModel: "model", setSelectedVoice: vi.fn(),
  } as unknown as ReturnType<typeof useConversionOptions>;
});

afterEach(async () => {
  await act(async () => root.unmount());
  vi.restoreAllMocks();
});

it("shows a retryable error when voice preview times out", async () => {
  const error = createRequestTimeoutError();
  client = { requestBlob: vi.fn().mockRejectedValue(error) } as unknown as ApiClient;
  await act(async () => root.render(<Probe />));
  await act(async () => { await current.onPreviewVoice(); });
  expect(current.previewBusy).toBe(false);
  expect(current.previewError).toBe(true);
  expect(current.previewStatus).toBe(error.message);
});

it("replaces the analyzing notice when voice recommendation times out", async () => {
  const error = createRequestTimeoutError();
  client = { suggestVoice: vi.fn().mockRejectedValue(error) } as unknown as ApiClient;
  await act(async () => root.render(<Probe />));
  await act(async () => { await current.onRecommendVoice(); });
  expect(current.voiceSuggestBusy).toBe(false);
  expect(current.voiceSuggestStatus).toBe(error.message);
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

it.each(["voice", "engine", "speed", "quantization", "text", "hidden"])("discards a preview after changing %s", async (change) => {
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
  if (change === "hidden") hidden = true;
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

it("pauses a playing preview when leaving the conversion panel", async () => {
  options = { ...options, filteredVoiceOptions: [{ value: "A", label: "Voice A" }] };
  client = { requestBlob: vi.fn(async () => new Blob(["audio"])) } as unknown as ApiClient;
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:preview");
  const revoke = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
  await act(async () => root.render(<Probe />));
  const audio = { pause: vi.fn(), play: vi.fn(async () => {}), src: "", currentTime: 0 } as unknown as HTMLAudioElement;
  current.previewAudioRef.current = audio;
  await act(async () => current.onPreviewVoice());
  expect(current.previewStatus).toBe("Previewing Voice A");
  vi.mocked(audio.pause).mockClear();
  hidden = true;
  await act(async () => root.render(<Probe />));
  expect(audio.pause).toHaveBeenCalledOnce();
  expect(revoke).toHaveBeenCalledWith("blob:preview");
  expect(current.previewStatus).toBe("");
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

it("clears the Previewing notice when the sample stops playing", async () => {
  client = { requestBlob: vi.fn(async () => new Blob(["wav"], { type: "audio/wav" })) } as unknown as ApiClient;
  options = { ...options, filteredVoiceOptions: [{ value: "A", label: "Alloy (en-US)" }] } as unknown as ReturnType<typeof useConversionOptions>;
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:preview");
  await act(async () => root.render(<Probe />));
  const audio = document.createElement("audio");
  vi.spyOn(audio, "play").mockResolvedValue(undefined);
  current.previewAudioRef.current = audio;
  await act(async () => { await current.onPreviewVoice(); });
  expect(current.previewStatus).toBe("Previewing Alloy (en-US)");
  await act(async () => { audio.dispatchEvent(new Event("ended")); });
  expect(current.previewStatus).toBe("");
});

it("explains truthfully why a local cleaner cannot recommend a voice", async () => {
  client = {} as ApiClient;
  options = { ...options, cleanerBackend: "cuda" } as unknown as ReturnType<typeof useConversionOptions>;
  await act(async () => root.render(<Probe />));
  expect(current.recommendationUnavailableReason).toBe(
    "Voice recommendations need an online (API) text cleaner, and yours runs locally. Pick a voice from the list, or set up an API cleaner in Settings.");
  options = { ...options, cleanerBackend: "api", cleanerApiKey: "" } as unknown as ReturnType<typeof useConversionOptions>;
  await act(async () => root.render(<Probe />));
  expect(current.recommendationUnavailableReason).toBe("Add the API cleaner's model and key in Settings to recommend a voice.");
});

it("bounds the preview request and aborts it when the preview is discarded", async () => {
  let signal: AbortSignal | undefined;
  const requestBlob = vi.fn((_path: string, init?: { signal?: AbortSignal; timeoutMs?: number }) => {
    signal = init?.signal;
    return new Promise<Blob>(() => undefined);
  });
  client = { requestBlob } as unknown as ApiClient;
  await act(async () => root.render(<Probe />));
  void current.onPreviewVoice();
  expect(requestBlob.mock.calls[0][1]).toMatchObject({ timeoutMs: expect.any(Number), signal: expect.any(AbortSignal) });
  expect(signal?.aborted).toBe(false);
  hidden = true;
  await act(async () => root.render(<Probe />));
  expect(signal?.aborted).toBe(true);
});
