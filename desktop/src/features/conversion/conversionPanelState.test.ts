import { describe, expect, it } from "vitest";

import { canRunConversionQueue, resolveConversionBlocker, resolveStartDisabled } from "./useConversionPanelState";

describe("resolveStartDisabled", () => {
  const ready = {
    blockingReason: null,
    queueRunning: false,
    isStarting: false,
    hasActiveJob: false,
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

  it("disables the action when a blocker is present", () => {
    expect(resolveStartDisabled({ ...ready, blockingReason: "Select a source." })).toBe(true);
  });
});

describe("conversion blocker guidance", () => {
  it("explains a missing selected cleaner API key and offers Settings", () => {
    expect(resolveConversionBlocker({
      apiClientAvailable: true,
      sourcePath: "/tmp/book.epub",
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
      selectedEngine: "",
      selectedVoice: "",
      cleanerRuntimeError: false,
      cleanerRequiresApiKey: false,
      cleanerApiKey: "",
      cleanerDisplayName: "",
      cleanerModelOptionsCount: 0,
      cleanerModel: "",
    })).toEqual({
      blockingReason: "Select a source before starting conversion.",
      showSettingsShortcut: false,
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
