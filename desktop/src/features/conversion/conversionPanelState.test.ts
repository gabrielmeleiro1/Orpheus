import { describe, expect, it } from "vitest";

import {
  canRunConversionQueue,
  resolveConnectionBlocker,
  resolveConversionBlocker,
  resolveRuntimeBlocker,
  resolveStartDisabled,
} from "./useConversionPanelState";

describe("resolveStartDisabled", () => {
  const ready = {
    blockingReason: null,
    queueRunning: false,
    isStarting: false,
    hasActiveJob: false,
    pasteBusy: false,
    sourcelessPasteReady: false,
  };

  it("enables the primary action when nothing is blocking or in flight", () => {
    expect(resolveStartDisabled(ready)).toBe(false);
  });

  it("disables the action while a job is already active", () => {
    expect(resolveStartDisabled({ ...ready, hasActiveJob: true })).toBe(true);
  });

  it("disables the action while the queue is running or a start is pending", () => {
    expect(resolveStartDisabled({ ...ready, queueRunning: true })).toBe(true);
    expect(resolveStartDisabled({ ...ready, isStarting: true })).toBe(true);
  });

  it("disables the action while pasted text is being queued", () => {
    expect(resolveStartDisabled({ ...ready, pasteBusy: true, sourcelessPasteReady: true })).toBe(true);
  });

  it("keeps an existing source startable while an unrelated paste upload is busy", () => {
    expect(resolveStartDisabled({ ...ready, pasteBusy: true })).toBe(false);
  });

  it("disables the action when a blocker is present", () => {
    expect(resolveStartDisabled({ ...ready, blockingReason: "Select a source." })).toBe(true);
  });
});

describe("conversion blocker guidance", () => {
  it("explains a missing selected cleaner API key and offers Settings", () => {
    expect(resolveConversionBlocker({
      apiClientAvailable: true,
      sourcePath: "/tmp/book.epub",
      hasPastedText: false,
      selectedEngine: "kokoro",
      selectedVoice: "af_heart",
      cleanerRuntimeError: false,
      cleanerRequiresApiKey: true,
      cleanerApiKey: "",
      cleanerDisplayName: "Cloud API",
      cleanerModelOptionsCount: 1,
      cleanerModel: "gpt-4o-mini",
    })).toEqual({
      blockingReason: "Cloud API requires an API key. Add it in Settings.",
      showSettingsShortcut: true,
    });
  });

  it("reports the first missing conversion prerequisite", () => {
    expect(resolveConversionBlocker({
      apiClientAvailable: true,
      sourcePath: "",
      hasPastedText: false,
      selectedEngine: "",
      selectedVoice: "",
      cleanerRuntimeError: false,
      cleanerRequiresApiKey: false,
      cleanerApiKey: "",
      cleanerDisplayName: "",
      cleanerModelOptionsCount: 0,
      cleanerModel: "",
    })).toEqual({
      blockingReason: "Add a book to start.",
      showSettingsShortcut: false,
    });
  });

  it("accepts un-queued pasted text in place of a selected source", () => {
    expect(resolveConversionBlocker({
      apiClientAvailable: true,
      sourcePath: "",
      hasPastedText: true,
      selectedEngine: "kokoro",
      selectedVoice: "af_heart",
      cleanerRuntimeError: false,
      cleanerRequiresApiKey: false,
      cleanerApiKey: "",
      cleanerDisplayName: "",
      cleanerModelOptionsCount: 0,
      cleanerModel: "",
    })).toEqual({ blockingReason: null, showSettingsShortcut: false });
  });

  it("still enforces the remaining prerequisites when only pasted text is present", () => {
    expect(resolveConversionBlocker({
      apiClientAvailable: true,
      sourcePath: "",
      hasPastedText: true,
      selectedEngine: "",
      selectedVoice: "",
      cleanerRuntimeError: false,
      cleanerRequiresApiKey: false,
      cleanerApiKey: "",
      cleanerDisplayName: "",
      cleanerModelOptionsCount: 0,
      cleanerModel: "",
    })).toEqual({
      blockingReason: "Choose a voice engine.",
      showSettingsShortcut: false,
    });
  });

  it("blocks an API-backed cleaner until its key has produced a model list", () => {
    expect(resolveConversionBlocker({
      apiClientAvailable: true,
      sourcePath: "/tmp/book.epub",
      hasPastedText: false,
      selectedEngine: "kokoro",
      selectedVoice: "af_heart",
      cleanerRuntimeError: false,
      cleanerRequiresApiKey: true,
      cleanerApiKey: "sk-live-key",
      cleanerDisplayName: "Cloud API",
      cleanerModelOptionsCount: 0,
      cleanerModel: "",
    })).toEqual({
      blockingReason: "No Cloud API models loaded yet. Check the API key in Settings.",
      showSettingsShortcut: true,
    });
  });

  it("blocks a queued conversion when the shared blocker is active", () => {
    expect(canRunConversionQueue({
      apiClientAvailable: true,
      queueRunning: false,
      hasRunnableItems: true,
      blocker: {
        blockingReason: "Cloud API requires an API key. Add it in Settings.",
        showSettingsShortcut: true,
      },
    })).toBe(false);
  });
});

describe("runtime blocker", () => {
  it("lets a verified runtime through", () => {
    expect(resolveRuntimeBlocker("ready")).toBeNull();
  });

  it("points a library-only setup at Settings", () => {
    expect(resolveRuntimeBlocker("unavailable")).toEqual({
      blockingReason: "Conversion is unavailable. Set up a voice model in Settings.",
      showSettingsShortcut: true,
    });
  });

  it("blocks starting while the backend is not answering", () => {
    expect(resolveConnectionBlocker("connected")).toBeNull();
    for (const state of ["checking", "error"] as const) {
      expect(resolveConnectionBlocker(state)).toEqual({
        blockingReason: "Orpheus is reconnecting. Start is available once it is back.",
        showSettingsShortcut: false,
      });
    }
  });

  it("asks to wait while the runtime is still being checked", () => {
    expect(resolveRuntimeBlocker("checking")).toEqual({
      blockingReason: "Wait for the runtime check to finish.",
      showSettingsShortcut: false,
    });
  });
});
